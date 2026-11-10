"""注册 / 登录 / 当前用户 / 偏好设置 / 找回密码。"""

from __future__ import annotations

import base64
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import captcha as cap
from .. import email_code
from .. import profile_service as ps
from .. import dav_auth as da
from .. import settings_store, trash_guard
from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import User, utcnow
from ..schemas import (
    EmailBindIn,
    EmailCodeIn,
    EmailVerifyIn,
    ForgotCheckIn,
    ForgotStartIn,
    LoginIn,
    PasswordIn,
    PasswordOnlyIn,
    ProfileIn,
    RegisterIn,
    ResetPasswordIn,
    SettingsIn,
    TrashSwitchIn,
)
from ..security import (
    check_origin,
    client_ip,
    create_token,
    email_problem,
    hash_password,
    normalize_email,
    password_problem,
    sign_reset,
    username_problem,
    verify_password,
    verify_reset,
)
from ..user_service import ensure_settings, log_action, user_payload

router = APIRouter(prefix="/api", tags=["auth"])

_FAIL_WINDOW = 15 * 60
_FAIL_LIMIT = 5
# 连续失败到这个次数就必须过验证码（需求：输错两次开始弹）
CAPTCHA_AFTER_FAILURES = 2

_FAIL_MESSAGE = "用户名或密码错误"

_failures: dict[str, deque[float]] = defaultdict(deque)


def _rate_key(request: Request, account: str) -> str:
    return f"{client_ip(request)}|{account.strip().lower()}"


def _recent_failures(key: str) -> int:
    """清掉窗口外的记录后，返回窗口内的失败次数。"""
    now = time.time()
    q = _failures[key]
    while q and now - q[0] > _FAIL_WINDOW:
        q.popleft()
    return len(q)


def _guard_rate(key: str) -> None:
    if _recent_failures(key) >= _FAIL_LIMIT:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "失败次数过多，请 15 分钟后再试"
        )


def _record_failure(key: str) -> None:
    _failures[key].append(time.time())


def _auth_error(message: str, need_captcha: bool, failures: int = 0) -> JSONResponse:
    """登录失败统一出口：把「要不要验证码」明确告诉前端。"""
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": message, "needCaptcha": need_captcha, "failures": failures},
    )


def _set_session(response: Response, user: User) -> None:
    token = create_token(user.id, user.token_version)
    response.set_cookie(
        key=settings.COOKIE_NAME,
        value=token,
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


@router.get("/meta")
def meta(db: Session = Depends(get_db)) -> dict:
    from .. import db_failover

    return {
        "appName": settings_store.get_value(db, "site_name"),
        "version": settings.VERSION,
        "allowRegister": settings_store.get_flag(db, "allow_register"),
        "announcement": settings_store.get_value(db, "site_announcement"),
        "chunkSize": settings_store.get_int(db, "chunk_size_bytes") or settings.chunk_size_bytes,
        "maxFileSize": settings_store.get_int(db, "max_file_size_bytes"),
        "trashRetentionDays": settings_store.get_int(db, "trash_retention_days"),
        "siteUrl": settings.SITE_URL,
        # primary = 主库（MySQL）；fallback = 主库不可用，本地 SQLite 接管
        "dbMode": db_failover.mode_label(),
        # 认证相关：前端据此决定要不要显示验证码、找回密码走哪条路
        "mailReady": settings.mail_ready,
        "contactEmail": settings_store.get_value(db, "contact_email"),
        "contactQq": settings_store.get_value(db, "contact_qq"),
        # 管理员开启后，前端登录时自动查一次新版本（只提示）
        "autoUpdateCheck": settings_store.get_flag(db, "auto_update_check"),
        "preview": {
            "chromecast": settings.PREVIEW_CHROMECAST,
            "vastTag": settings.PREVIEW_VAST_TAG,
        },
    }


@router.get("/auth/captcha")
def get_captcha(request: Request) -> dict:
    """签发一张图形验证码。码只存在服务端，前端拿到的是渲染好的 SVG。"""
    try:
        cid, svg = cap.issue(client_ip(request))
    except cap.TooManyRequests:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "验证码获取过于频繁，请稍后再试"
        ) from None
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return {
        "captchaId": cid,
        "image": f"data:image/svg+xml;base64,{encoded}",
        "expiresIn": cap.CODE_TTL_SECONDS,
    }


@router.get("/auth/check-username")
def check_username(
    username: str = Query(min_length=1, max_length=32), db: Session = Depends(get_db)
) -> dict:
    """注册时实时查重。只回答「能不能用」，不返回任何账号信息。"""
    name = username.strip()
    problem = username_problem(name)
    if problem:
        return {"available": False, "reason": problem}
    taken = db.scalar(select(User.id).where(func.lower(User.username) == name.lower()))
    if taken:
        return {"available": False, "reason": "该用户名已被占用"}
    return {"available": True, "reason": ""}


@router.post("/auth/register", status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)
) -> dict:
    check_origin(request)
    if not settings_store.get_flag(db, "allow_register"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "站点已关闭注册")
    # 勾选是注册的前置条件：不经过前端、直接调接口也要挡住
    if not payload.agree:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "请先阅读并同意《用户协议》和《隐私政策》"
        )

    # 先查格式，再验验证码：格式错误不该白烧一张验证码
    username = payload.username.strip()
    problem = (
        username_problem(username)
        or password_problem(payload.password)
        or email_problem(payload.email or "")
    )
    if problem:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)

    passed, reason = cap.verify(payload.captcha_id, payload.captcha)
    if not passed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, reason)

    exists = db.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "该用户名已被占用")

    email = normalize_email(payload.email or "") or None
    if email:
        if db.scalar(select(User.id).where(func.lower(User.email) == email)):
            raise HTTPException(status.HTTP_409_CONFLICT, "该邮箱已被其它账号绑定")

    is_first = db.scalar(select(func.count()).select_from(User)) == 0
    user = User(
        username=username,
        password_hash=hash_password(payload.password),
        email=email,
        email_verified=False,
        dav_ha1=da.ha1(username, payload.password),
        role=1 if is_first else 0,
        quota_bytes=settings_store.get_int(db, "default_quota_bytes") or settings.default_quota_bytes,
        token_version=1,
        created_at=utcnow(),
        last_login_at=utcnow(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    ensure_settings(db, user.id)
    log_action(db, "register", user.id, {"firstAdmin": is_first}, request)
    db.commit()

    _set_session(response, user)
    return {"user": user_payload(db, user), "firstAdmin": is_first}


@router.post("/auth/login")
def login(
    payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)
):
    check_origin(request)
    key = _rate_key(request, payload.username)
    _guard_rate(key)

    failures = _recent_failures(key)
    need_captcha = failures >= CAPTCHA_AFTER_FAILURES
    if need_captcha:
        passed, reason = cap.verify(payload.captcha_id, payload.captcha)
        if not passed:
            # 验证码错不算密码失败，但照样要换一张新的
            return _auth_error(reason, True, failures)

    account = payload.username.strip()
    ident = account.lower()
    user = db.scalar(
        select(User).where(
            or_(func.lower(User.username) == ident, func.lower(User.email) == ident)
        )
    )
    if user is None or not verify_password(payload.password, user.password_hash):
        _record_failure(key)
        count = _recent_failures(key)
        log_action(db, "login_failed", user.id if user else None, {"account": account}, request)
        db.commit()
        return _auth_error(_FAIL_MESSAGE, count >= CAPTCHA_AFTER_FAILURES, count)

    if user.banned:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该账号已被封禁")

    _failures.pop(key, None)
    if not user.dav_ha1:
        # 网页登录拿得到明文，正好给老账号补上 WebDAV Digest 需要的 HA1
        user.dav_ha1 = da.ha1(user.username, payload.password)
    user.last_login_at = utcnow()
    log_action(db, "login", user.id, None, request)
    db.commit()

    _set_session(response, user)
    return {"user": user_payload(db, user)}


# --------------------------------------------------------------------------- #
# 邮箱验证码（接口已就绪，邮件服务未接入时返回 503）
# --------------------------------------------------------------------------- #
def _mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    if not domain:
        return "***"
    head = name[:2] if len(name) > 2 else name[:1]
    return f"{head}***@{domain}"


@router.post("/auth/email/code")
def send_email_code(
    payload: EmailCodeIn, request: Request, db: Session = Depends(get_db)
) -> dict:
    """发送邮箱验证码。

    限流全部在服务端：同目标 30 秒一次、同设备发满 10 次冷却 1 小时、
    新码覆盖旧码。邮件服务没配好之前直接 503，不消耗任何配额。
    """
    check_origin(request)
    if not settings.mail_ready:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "邮件服务尚未配置，暂时无法发送验证码"
        )

    target = payload.target.strip()
    problem = email_problem(target)
    if problem:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)
    target = normalize_email(target)

    if payload.scene in ("register", "bind"):
        if db.scalar(select(User.id).where(func.lower(User.email) == target)):
            raise HTTPException(status.HTTP_409_CONFLICT, "该邮箱已被其它账号绑定")

    try:
        retry_after = email_code.issue(payload.scene, target, payload.device or client_ip(request))
    except email_code.SendError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, exc.message) from None

    log_action(db, "email_code_send", None, {"scene": payload.scene}, request)
    db.commit()
    return {"sent": True, "retryAfter": retry_after}


@router.post("/auth/email/verify")
def verify_email_code(payload: EmailVerifyIn, request: Request) -> dict:
    """核验邮箱验证码。通过即销毁，前端无法伪造。"""
    check_origin(request)
    passed, reason = email_code.verify(payload.scene, payload.target, payload.code)
    if not passed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, reason)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 忘记密码：用户名 + 绑定邮箱（邮箱链接流程预留）
# --------------------------------------------------------------------------- #
def _contact(db: Session) -> dict:
    return {
        "contactEmail": settings_store.get_value(db, "contact_email"),
        "contactQq": settings_store.get_value(db, "contact_qq"),
    }


@router.post("/auth/forgot/start")
def forgot_start(
    payload: ForgotStartIn, request: Request, db: Session = Depends(get_db)
) -> dict:
    """看这个账号能走哪条路。

    - 填的是邮箱 → 走邮件链接（未开通，先返回 mailReady=false）
    - 填的是用户名 → 需要再填一次绑定的邮箱来证明身份
    """
    check_origin(request)
    account = payload.account.strip()
    base = {"mailReady": settings.mail_ready, **_contact(db)}

    if "@" in account:
        email = normalize_email(account)
        user = db.scalar(select(User.id).where(func.lower(User.email) == email))
        return {"mode": "email", "found": bool(user), **base}

    user = db.scalar(
        select(User).where(func.lower(User.username) == account.lower())
    )
    if user is None:
        return {"mode": "username", "found": False, "hasEmail": False, **base}
    return {
        "mode": "username",
        "found": True,
        "hasEmail": bool(user.email),
        "maskedEmail": _mask_email(user.email) if user.email else "",
        **base,
    }


@router.post("/auth/forgot/email")
def forgot_by_email(
    payload: ForgotCheckIn, request: Request, db: Session = Depends(get_db)
) -> dict:
    """用用户名重置：填对绑定邮箱才发重置令牌。

    邮箱验证码通道已经预留（/auth/email/code?scene=reset），
    等邮件服务上线后在这里多校验一步即可。
    """
    check_origin(request)
    key = _rate_key(request, payload.account)
    _guard_rate(key)

    user = db.scalar(
        select(User).where(func.lower(User.username) == payload.account.strip().lower())
    )
    if user is None or not user.email:
        _record_failure(key)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该账号不存在或还没有绑定邮箱")
    if normalize_email(payload.email) != normalize_email(user.email):
        _record_failure(key)
        log_action(db, "password_reset_failed", user.id, None, request)
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "邮箱与绑定的邮箱不一致")

    token = sign_reset(user.id, user.password_hash)
    log_action(db, "password_reset_ticket", user.id, None, request)
    db.commit()
    return {"token": token, "expiresIn": 900}


@router.post("/auth/reset")
def reset_password(
    payload: ResetPasswordIn, request: Request, db: Session = Depends(get_db)
) -> dict:
    """凭令牌重置密码。令牌一次性，改完密码立刻失效。"""
    check_origin(request)
    problem = password_problem(payload.password)
    if problem:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)

    # 先按 uid 取人，再用当前 password_hash 验签 —— 密码一变签名就对不上
    try:
        uid = int(payload.token.split(".")[0])
    except (ValueError, IndexError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "重置链接无效或已过期") from None
    user = db.get(User, uid)
    if user is None or verify_reset(payload.token, user.password_hash) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "重置链接无效或已过期")

    user.password_hash = hash_password(payload.password)
    user.dav_ha1 = da.ha1(user.username, payload.password)
    user.token_version += 1  # 旧会话全部失效
    log_action(db, "password_reset", user.id, None, request)
    db.commit()
    return {"ok": True, "username": user.username}


@router.post("/auth/logout")
def logout(
    request: Request, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    check_origin(request)
    log_action(db, "logout", user.id, None, request)
    db.commit()
    response.delete_cookie(settings.COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/me/dav/activate")
def activate_dav(
    payload: PasswordOnlyIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """为已登录的老账号补生成 WebDAV 的 Digest 凭据。

    只校验密码、存 HA1（MD5(user:realm:password)），不保存明文。
    """
    check_origin(request)
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "密码不正确")
    user.dav_ha1 = da.ha1(user.username, payload.password)
    log_action(db, "dav_activate", user.id, None, request)
    db.commit()
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return {"user": user_payload(db, user)}


@router.patch("/me/email")
def update_email(
    payload: EmailBindIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """绑定 / 换绑邮箱。

    邮件服务还没通，所以现在只能存下来、标记为未验证；
    等 /auth/email/code 可用后，这里接一步验证码即可转成已验证。
    """
    check_origin(request)
    problem = email_problem(payload.email)
    if problem:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)

    email = normalize_email(payload.email)
    other = db.scalar(
        select(User.id).where(func.lower(User.email) == email, User.id != user.id)
    )
    if other:
        raise HTTPException(status.HTTP_409_CONFLICT, "该邮箱已被其它账号绑定")

    user.email = email or None
    user.email_verified = False
    log_action(db, "email_bind", user.id, {"email": email}, request)
    db.commit()
    return {"user": user_payload(db, user)}


# --------------------------------------------------------------------------- #
# 个人资料：昵称 / 简介 / 头像
# --------------------------------------------------------------------------- #
def _clean_display_name(raw: str) -> str | None:
    """昵称只做长度与控制字符校验；空串表示清空（回落到用户名）。"""
    name = " ".join(raw.split())  # 折叠连续空白 + 去首尾
    if not name:
        return None
    if len(name) > ps.DISPLAY_NAME_MAX:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"昵称最多 {ps.DISPLAY_NAME_MAX} 个字符"
        )
    if any(ord(ch) < 32 for ch in name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "昵称不能包含控制字符")
    return name


@router.patch("/me/profile")
def update_profile(
    payload: ProfileIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    check_origin(request)
    changed: list[str] = []

    if payload.display_name is not None:
        user.display_name = _clean_display_name(payload.display_name)
        changed.append("displayName")

    if payload.bio is not None:
        bio = payload.bio.strip()
        if len(bio) > ps.BIO_MAX:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"简介最多 {ps.BIO_MAX} 个字符")
        user.bio = bio or None
        changed.append("bio")

    if changed:
        log_action(db, "profile_update", user.id, {"fields": changed}, request)
        db.commit()
    return {"user": user_payload(db, user)}


@router.put("/me/avatar")
async def upload_avatar(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """头像走原始字节上传（不走 multipart，省一个依赖）；服务端裁方图 + 缩到 256px。"""
    check_origin(request)

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > ps.AVATAR_MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "头像不能超过 4 MB")

    raw = await request.body()
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有收到图片数据")

    try:
        name = ps.save_avatar(user.id, raw)
    except ValueError as exc:
        if str(exc) == "too-large":
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "头像不能超过 4 MB") from None
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "图片读取失败，请换一张试试") from None
    except Exception:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "无法识别这张图片，请用 png / jpg / webp"
        ) from None

    old = user.avatar
    user.avatar = name
    log_action(db, "avatar_update", user.id, None, request)
    db.commit()
    ps.drop_avatar(old)  # 落库成功再删旧文件，避免中途失败丢头像
    return {"user": user_payload(db, user)}


@router.delete("/me/avatar")
def remove_avatar(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    check_origin(request)
    old = user.avatar
    if old:
        user.avatar = None
        log_action(db, "avatar_remove", user.id, None, request)
        db.commit()
        ps.drop_avatar(old)
    return {"user": user_payload(db, user)}


@router.put("/me/background")
async def upload_background(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """自定义背景：原始字节上传，服务端等比缩到长边 2560 并转 webp。

    上传成功后自动把 wallpaper 切成 custom，前端再从图片取主色。
    """
    check_origin(request)

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > ps.BACKGROUND_MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "背景图片不能超过 16 MB")

    raw = await request.body()
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有收到图片数据")

    try:
        name = ps.save_background(user.id, raw)
    except ValueError as exc:
        if str(exc) == "too-large":
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "背景图片不能超过 16 MB"
            ) from None
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "图片读取失败，请换一张试试") from None
    except Exception:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "无法识别这张图片，请用 png / jpg / webp"
        ) from None

    row = ensure_settings(db, user.id)
    old = row.background_image
    row.background_image = name
    row.wallpaper = "custom"
    log_action(db, "background_update", user.id, None, request)
    db.commit()
    ps.drop_background(old)  # 落库成功再删旧图，避免中途失败丢背景
    return {"user": user_payload(db, user)}


@router.delete("/me/background")
def remove_background(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    check_origin(request)
    row = ensure_settings(db, user.id)
    old = row.background_image
    if old or row.wallpaper == "custom":
        row.background_image = None
        if row.wallpaper == "custom":
            row.wallpaper = "sakura"  # 回到默认壁纸，别留一个没有图的 custom
        log_action(db, "background_remove", user.id, None, request)
        db.commit()
        ps.drop_background(old)
    return {"user": user_payload(db, user)}


@router.get("/background/{name}")
def get_background(
    name: str,
    user: User = Depends(get_current_user),
):
    """背景图只有本人能读：文件名前缀是所有者 id。"""
    if not name.startswith(f"{user.id}-"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "背景不存在")
    path = ps.background_path(name)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "背景不存在")
    return FileResponse(
        path,
        media_type=ps.background_media_type(name),
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.get("/avatar/{name}")
def get_avatar(
    name: str,
    user: User = Depends(get_current_user),
):
    """只允许读自己的头像：文件名前缀就是所有者 id。

    管理员例外——后台用户列表要显示每个账号的头像。
    """
    if not name.startswith(f"{user.id}-") and user.role != 1:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "头像不存在")
    path = ps.avatar_path(name)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "头像不存在")
    return FileResponse(
        path,
        media_type=ps.avatar_media_type(name),
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.patch("/me/settings")
def update_settings(
    payload: SettingsIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    check_origin(request)
    row = ensure_settings(db, user.id)
    allowed = {
        "theme_mode",
        "accent_color",
        "wallpaper",
        "show_mascot",
        "show_petals",
        "view_mode",
        "language",
        "nav_collapsed",
        "share_show_name",
        "webdav_enabled",
        "lyrics_style",
        "surface_opacity",
    }
    for field, value in payload.model_dump(exclude_none=True).items():
        if field in allowed:
            if field == "lyrics_style":
                # 整体覆盖，且只收白名单键，避免塞进任意垃圾
                value = {
                    k: value[k]
                    for k in ("fontSize", "hotColor", "showTrans")
                    if k in value
                }
            elif field == "surface_opacity":
                # 只收四个模块键，值夹到 0-100 的整数
                value = {
                    k: max(0, min(100, int(round(v))))
                    for k in ("nav", "titlebar", "panel", "dialog")
                    if isinstance((v := value.get(k)), (int, float)) and not isinstance(v, bool)
                }
            setattr(row, field, value)
    db.commit()
    return {"settings": user_payload(db, user)["settings"]}


@router.post("/me/trash/challenge")
def trash_challenge(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """签发"关闭回收站"用的验证码。

    码由服务端随机生成并保存，这里明文返回只是给用户照着输入；
    是否通过完全由 /me/trash/switch 在服务端核验。
    """
    check_origin(request)
    try:
        item = trash_guard.issue(user.id)
    except trash_guard.TooManyRequests:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "获取验证码过于频繁，请稍后再试"
        ) from None
    return {"code": item.code, "expiresIn": trash_guard.CODE_TTL_SECONDS}


@router.post("/me/trash/switch")
def switch_trash(
    payload: TrashSwitchIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """开关回收站。关闭属于破坏性变更，必须带服务端签发的验证码。"""
    check_origin(request)
    row = ensure_settings(db, user.id)

    if payload.enabled:
        row.trash_enabled = True
        log_action(db, "trash_enable", user.id, None, request)
        db.commit()
        return {"settings": user_payload(db, user)["settings"]}

    passed, reason = trash_guard.verify(user.id, payload.code)
    if not passed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, reason)

    row.trash_enabled = False
    log_action(db, "trash_disable", user.id, None, request)
    db.commit()
    return {"settings": user_payload(db, user)["settings"]}


@router.put("/me/password")
def change_password(
    payload: PasswordIn,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    check_origin(request)
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "原密码不正确")
    problem = password_problem(payload.new_password)
    if problem:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)
    if payload.old_password == payload.new_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新密码不能与原密码相同")

    user.password_hash = hash_password(payload.new_password)
    user.dav_ha1 = da.ha1(user.username, payload.new_password)
    user.token_version += 1  # 踢掉其它设备
    log_action(db, "change_password", user.id, None, request)
    db.commit()

    _set_session(response, user)
    return {"ok": True}
