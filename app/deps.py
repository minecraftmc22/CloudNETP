"""FastAPI 依赖：会话用户、管理员校验。"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User
from .security import decode_token, sign_ban_ticket
from .user_service import log_action


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
    if int(payload.get("v", 0)) != user.token_version:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录状态已失效，请重新登录")
    if user.banned:
        from . import ban_service

        # 临时封禁到期了就顺手放行，不用等管理员手动解封
        if ban_service.lift_expired(db, user):
            log_action(db, "ban_expired", user.id, {"reason": "到期自动解封"}, request)
            db.commit()
        else:
            # 403 + 结构化 detail：前端据此弹「你已被管理员封禁」而不是当普通错误
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "banned",
                    "message": "你已被管理员封禁",
                    "ban": ban_service.info(user),
                    "ticket": sign_ban_ticket(user.id, ban_service.stamp_of(user)),
                },
            )
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
