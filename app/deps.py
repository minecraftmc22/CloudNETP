"""FastAPI 依赖：会话用户、管理员校验。"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User
from .security import decode_token


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(settings.COOKIE_NAME)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已过期，请重新登录")
    user = db.get(User, int(payload.get("u", 0)))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号不存在")
    if user.banned:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该账号已被封禁")
    if int(payload.get("v", 0)) != user.token_version:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录状态已失效，请重新登录")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    # is_super 也算管理员：数据库里把 is_super 打开就能用，不必同时手改 role
    if user.role != 1 and not user.is_super:
        # 用 404 而非 403，不暴露管理端点是否存在
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    return user


def require_super(user: User = Depends(require_admin)) -> User:
    """超级管理员：能调整成员角色。普通管理员走到这里一律 403。"""
    if not user.is_super:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有超级管理员能调整成员角色")
    return user
