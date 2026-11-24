"""第三方登录：Google / GitHub 的授权跳转、回调、绑定与解绑。

流程（前端只是两个链接，没有任何 SDK）::

    GET /api/auth/oauth/google/start?next=/some/path
      → 302 到 Google 授权页（state 里签了 provider / mode / uid / next）
    GET /api/auth/oauth/google/callback?code=...&state=...
      → 校验 state → 换 token → 取身份 → 找/建账号 → 发会话 → 302 回 next

约定：
- **邮箱不参与主键**，绑定只认 provider 侧的稳定 ID（`oauth_google` / `oauth_github`）。
- 第三方邮箱已被本地账号占用时**不自动合并**，宁可让用户先用密码登录再来绑定——
  自动合并等于「谁控制了这个邮箱就能接管账号」，而第三方邮箱的验证状态我们无法完全信任。
- 失败一律 302 到 `/?oauth_error=<code>`，文案由前端按 `ERRORS` 表显示，
  不透传上游返回的英文原文。
"""

from __future__ import annotations

import logging
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import oauth_config, oauth_service, settings_store, twofa
from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import User, utcnow
from ..security import (
    check_origin,
    decode_token,
    hash_password,
    normalize_email,
    sign_oauth_state,
    username_problem,
    verify_oauth_state,
)
from ..user_service import ensure_settings, log_action
from .auth import _finish_login, _set_session

log = logging.getLogger("uvicorn.error").getChild("cloudnetp.oauth")

router = APIRouter(prefix="/api", tags=["oauth"])

# 「绑定成功」回到设置页的账号安全区
SETTINGS_PATH = "/#/settings"


def _fail(code: str, detail: str = "") -> RedirectResponse:
    if detail:
        log.warning("第三方登录失败（%s）：%s", code, detail)
    safe = code if code in oauth_service.ERRORS else "server"
    return RedirectResponse(f"/?oauth_error={safe}", status_code=302)


def _ok(path: str) -> RedirectResponse:
    return RedirectResponse(path or "/", status_code=302)


def _optional_user(request: Request, db: Session) -> User | None:
    """读 cookie 里的会话（不抛 401）：回调里要判断「谁在绑定」。"""
    token = request.cookies.get(settings.COOKIE_NAME)
    payload = decode_token(token) if token else None
    if not payload:
        return None
    user = db.get(User, int(payload.get("u", 0)))
    if not user or int(payload.get("v", 0)) != user.token_version:
        return None
    return user


def _column(provider: str) -> str:
    return f"oauth_{provider}"


# --------------------------------------------------------------------------- #
# 跳转
# --------------------------------------------------------------------------- #
@router.get("/auth/oauth/{provider}/start")
def oauth_start(
    provider: str,
    request: Request,
    next: str = "/",
    mode: str = "login",
    db: Session = Depends(get_db),
) -> RedirectResponse:
    cfg = oauth_config.resolved()
    if not oauth_config.provider_ready(cfg, provider):
        return _fail("disabled")

    bind = mode == "bind"
    uid = 0
    if bind:
        # 绑定必须已登录：否则回调时无从知道要绑到谁
        me = _optional_user(request, db)
        if me is None:
            return _fail("not_linked", "未登录却请求绑定")
        uid = me.id

    state = sign_oauth_state(provider, "bind" if bind else "login", uid, next)
    try:
        url = oauth_service.authorize_url(provider, cfg, state)
    except oauth_service.OAuthError as exc:
        return _fail(exc.code, exc.detail)
    return RedirectResponse(url, status_code=302)


# --------------------------------------------------------------------------- #
# 回调
# --------------------------------------------------------------------------- #
@router.get("/auth/oauth/{provider}/callback")
def oauth_callback(
    provider: str,
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
) -> RedirectResponse:
    data = verify_oauth_state(state)
    if not data or data["provider"] != provider:
        return _fail("state")
    if error or not code:
        # error=access_denied 是用户自己点了「取消」
        return _fail("denied" if error else "state", f"上游返回 error={error}")

    cfg = oauth_config.resolved()
    if not oauth_config.provider_ready(cfg, provider):
        return _fail("disabled")

    try:
        token = oauth_service.exchange_code(provider, cfg, code)
        ident = oauth_service.fetch_identity(provider, token)
    except oauth_service.OAuthError as exc:
        return _fail(exc.code, exc.detail)

    if data["mode"] == "bind":
        return _bind(provider, ident, data, request, db)
    return _login(provider, ident, data["next"], request, db)


def _bind(
    provider: str,
    ident: dict,
    data: dict,
    request: Request,
    db: Session,
) -> RedirectResponse:
    me = _optional_user(request, db)
    if me is None or me.id != data["uid"]:
        # 发起绑定的人和回来的人不是同一个：当作状态失效，绝不绑到别人号上
        return _fail("state", "绑定发起人与回调会话不一致")

    column = _column(provider)
    owner = db.scalar(select(User.id).where(getattr(User, column) == ident["subject"]))
    if owner and owner != me.id:
        return _fail("already_linked")
    current = getattr(me, column)
    if current and current != ident["subject"]:
        return _fail("already_linked", "该账号已绑定另一个第三方账号，需先解绑")
    if current == ident["subject"]:
        return _ok(SETTINGS_PATH)  # 重复绑定当成功，别报错吓人

    setattr(me, column, ident["subject"])
    # 顺手补一个已验证邮箱：第三方证明了邮箱归属，本地就不用再走一遍验证码
    if ident["emailVerified"] and ident["email"] and not me.email:
        taken = db.scalar(
            select(User.id).where(
                func.lower(User.email) == ident["email"], User.id != me.id
            )
        )
        if not taken:
            me.email = ident["email"]
            me.email_verified = True
    db.commit()
    log_action(db, "oauth_bind", me.id, {"provider": provider}, request)
    db.commit()
    return _ok(SETTINGS_PATH)


def _login(provider: str, ident: dict, next_path: str, request: Request, db: Session) -> RedirectResponse:
    column = _column(provider)
    user = db.scalar(select(User).where(getattr(User, column) == ident["subject"]))

    if user is None:
        # 邮箱已被本地账号占用：不自动合并，提示走「密码登录 + 绑定」
        if ident["emailVerified"] and ident["email"]:
            taken = db.scalar(
                select(User).where(func.lower(User.email) == ident["email"])
            )
            if taken is not None:
                return _fail("email_taken")
        user = _create_user(provider, ident, request, db)
        if user is None:
            return _fail("closed")

    if user.banned:
        return _fail("banned")
    if user.two_factor_enabled and twofa.login_methods(db, user):
        # OAuth 已经把「证明你是你」做完了，但开了 2FA 的人期望第二步不能少。
        # 这里不静默放行，也不做半套的待验证票据，直接请他走密码登录。
        return _fail("need_2fa")

    _finish_login(db, user, request, f"oauth_{provider}")
    response = _ok(next_path)
    _set_session(db, response, user, request)
    return response


# --------------------------------------------------------------------------- #
# 建号
# --------------------------------------------------------------------------- #
_USERNAME_STRIP = re.compile(r"[^\w.\u4e00-\u9fa5-]", re.UNICODE)


def _pick_username(ident: dict, db: Session) -> str | None:
    """从第三方用户名派一个本地用户名，冲突就加数字后缀。"""
    raw = _USERNAME_STRIP.sub("", ident.get("login") or "")
    base = raw[:20].strip(".-")
    if len(base) < 4:
        suffix = secrets.token_hex(2)
        base = f"{base}{suffix}" if base else f"user{suffix}"

    for attempt in range(20):
        candidate = base if attempt == 0 else f"{base}{attempt + 1}"
        if len(candidate) > 32:
            candidate = candidate[:28] + secrets.token_hex(2)
        if username_problem(candidate):
            continue
        exists = db.scalar(
            select(User.id).where(func.lower(User.username) == candidate.lower())
        )
        if not exists:
            return candidate
    return None


def _create_user(
    provider: str, ident: dict, request: Request, db: Session
) -> User | None:
    """首次第三方登录：按站点注册开关建号。返回 None 表示站点不允许注册。"""
    is_first = db.scalar(select(func.count()).select_from(User)) == 0
    if not is_first and not settings_store.get_flag(db, "allow_register"):
        return None

    username = _pick_username(ident, db)
    if username is None:
        log.warning("第三方登录建号失败：用户名生成不出来 provider=%s", provider)
        return None

    email = normalize_email(ident["email"] or "") or None
    # 随机密码：账号确实「有」密码，但没人知道。忘记密码可走邮箱重置。
    # 空串会踩 password_hash NOT NULL，且 sa 层不该出现 NULL 语义
    password = secrets.token_urlsafe(24)
    user = User(
        username=username,
        password_hash=hash_password(password),
        email=email,
        email_verified=bool(email and ident["emailVerified"]),
        display_name=(ident.get("name") or username)[:32],
        role=1 if is_first else 0,
        is_super=is_first,
        quota_bytes=settings_store.get_int(db, "default_quota_bytes")
        or settings.default_quota_bytes,
        token_version=1,
        created_at=utcnow(),
        last_login_at=utcnow(),
    )
    setattr(user, _column(provider), ident["subject"])
    db.add(user)
    db.commit()
    db.refresh(user)
    ensure_settings(db, user.id)
    log_action(
        db,
        "oauth_register",
        user.id,
        {"provider": provider, "firstAdmin": is_first},
        request,
    )
    db.commit()
    return user


# --------------------------------------------------------------------------- #
# 绑定状态 / 解绑（登录态）
# --------------------------------------------------------------------------- #
@router.get("/me/oauth")
def my_oauth(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    cfg = oauth_config.resolved()
    providers = {}
    for provider in oauth_config.PROVIDERS:
        providers[provider] = {
            "linked": bool(getattr(user, _column(provider), None)),
            # 管理员没配凭据时不显示「绑定」入口，否则点了只会跳到一个错误页
            "ready": oauth_config.provider_ready(cfg, provider),
        }
    return {
        "providers": providers,
        # 账号有没有可用的邮箱：解绑最后一个登录方式前要提醒这个
        "hasEmail": bool(user.email and user.email_verified),
    }


@router.delete("/auth/oauth/{provider}")
def unlink_oauth(
    provider: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    check_origin(request)
    if provider not in oauth_config.PROVIDERS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "未知的登录方式")
    column = _column(provider)
    if not getattr(user, column, None):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该登录方式还没有绑定")

    remaining = [
        p
        for p in oauth_config.PROVIDERS
        if p != provider and getattr(user, _column(p), None)
    ]
    # 解绑最后一个第三方账号、又没有已验证邮箱时，账号就再也登不上了
    if not remaining and not (user.email and user.email_verified):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "解绑后账号将没有可用的登录方式，请先绑定邮箱再解绑",
        )

    setattr(user, column, None)
    log_action(db, "oauth_unlink", user.id, {"provider": provider}, request)
    db.commit()
    return {"ok": True}
