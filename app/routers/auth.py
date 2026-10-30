"""注册 / 登录 / 当前用户 / 偏好设置。"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import profile_service as ps
from .. import dav_auth as da
from .. import settings_store, trash_guard
from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import User, utcnow
from ..schemas import (
    LoginIn,
    PasswordIn,
    PasswordOnlyIn,
    ProfileIn,
    RegisterIn,
    SettingsIn,
    TrashSwitchIn,
)
from ..security import (
    check_origin,
    client_ip,
    create_token,
    hash_password,
    password_problem,
    username_problem,
    verify_password,
)
from ..user_service import ensure_settings, log_action, user_payload

router = APIRouter(prefix="/api", tags=["auth"])

_FAIL_WINDOW = 15 * 60
_FAIL_LIMIT = 5
_failures: dict[str, deque[float]] = defaultdict(deque)


def _rate_key(request: Request, username: str) -> str:
    return f"{client_ip(request)}|{username.strip().lower()}"


def _guard_rate(key: str) -> None:
    now = time.time()
    q = _failures[key]
    while q and now - q[0] > _FAIL_WINDOW:
        q.popleft()
    if len(q) >= _FAIL_LIMIT:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "失败次数过多，请 15 分钟后再试"
        )


def _record_failure(key: str) -> None:
    _failures[key].append(time.time())


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
        "preview": {
            "chromecast": settings.PREVIEW_CHROMECAST,
            "vastTag": settings.PREVIEW_VAST_TAG,
        },
    }


@router.post("/auth/register", status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)
) -> dict:
    check_origin(request)
    if not settings_store.get_flag(db, "allow_register"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "站点已关闭注册")

    username = payload.username.strip()
    problem = username_problem(username) or password_problem(payload.password)
    if problem:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)

    exists = db.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "该用户名已被占用")

    is_first = db.scalar(select(func.count()).select_from(User)) == 0
    user = User(
        username=username,
        password_hash=hash_password(payload.password),
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
) -> dict:
    check_origin(request)
    key = _rate_key(request, payload.username)
    _guard_rate(key)

    username = payload.username.strip()
    user = db.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if user is None or not verify_password(payload.password, user.password_hash):
        _record_failure(key)
        log_action(db, "login_failed", user.id if user else None, {"username": username}, request)
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
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


@router.get("/avatar/{name}")
def get_avatar(
    name: str,
    user: User = Depends(get_current_user),
):
    """只允许读自己的头像：文件名前缀就是所有者 id。"""
    if not name.startswith(f"{user.id}-"):
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
        "nav_collapsed",
        "share_show_name",
    }
    for field, value in payload.model_dump(exclude_none=True).items():
        if field in allowed:
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
