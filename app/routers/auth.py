"""注册 / 登录 / 当前用户 / 偏好设置。"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import settings_store
from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import User, utcnow
from ..schemas import LoginIn, PasswordIn, RegisterIn, SettingsIn
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
        secure=settings.COOKIE_SECURE,
        path="/",
    )


@router.get("/meta")
def meta(db: Session = Depends(get_db)) -> dict:
    return {
        "appName": settings_store.get_value(db, "site_name"),
        "version": settings.VERSION,
        "allowRegister": settings_store.get_flag(db, "allow_register"),
        "announcement": settings_store.get_value(db, "site_announcement"),
        "chunkSize": settings_store.get_int(db, "chunk_size_bytes") or settings.chunk_size_bytes,
        "maxFileSize": settings_store.get_int(db, "max_file_size_bytes"),
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


@router.get("/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return {"user": user_payload(db, user)}


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
    }
    for field, value in payload.model_dump(exclude_none=True).items():
        if field in allowed:
            setattr(row, field, value)
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
    user.token_version += 1  # 踢掉其它设备
    log_action(db, "change_password", user.id, None, request)
    db.commit()

    _set_session(response, user)
    return {"ok": True}
