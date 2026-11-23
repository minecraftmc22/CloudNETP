"""消息中心 + 好友。

消息中心（/api/notices）：每个用户只看自己的通知；管理员额外拿到
「待处理」计数（举报/申诉/反馈），前端据此在铃铛上打角标。
好友（/api/friends）：搜索、申请、同意/拒绝、删除。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import delete
from sqlalchemy.orm import Session

from .. import chat_service as chat_svc
from .. import friend_service as fsv
from .. import notice_service as ns
from ..db import get_db
from ..deps import get_current_user
from ..models import Notice, User
from ..schemas import FriendRequestIn, NoticeReadIn
from ..security import check_origin
from ..user_service import log_action

router = APIRouter(prefix="/api", tags=["social"])

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


# --------------------------------------------------------------------------- #
# 消息中心
# --------------------------------------------------------------------------- #
@router.get("/notices")
def list_notices(
    db: DbSession,
    user: CurrentUser,
    unread_only: bool = Query(default=False, alias="unreadOnly"),
    limit: int = Query(default=60, ge=1, le=200),
) -> dict:
    return {
        "items": ns.list_for(db, user.id, limit=limit, unread_only=unread_only),
        "unread": ns.unread_count(db, user.id),
        "pending": ns.pending_for(db, user),
    }


@router.get("/notices/unread")
def notice_badge(db: DbSession, user: CurrentUser) -> dict:
    """角标轮询用：只回数字，尽量轻。"""
    return {"unread": ns.unread_count(db, user.id), "pending": ns.pending_for(db, user)}


@router.post("/notices/read")
def read_notices(payload: NoticeReadIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    changed = ns.mark_read(db, user.id, payload.ids)
    db.commit()
    return {"changed": changed, "unread": ns.unread_count(db, user.id)}


@router.delete("/notices/read")
def clear_read_notices(request: Request, db: DbSession, user: CurrentUser) -> dict:
    """清空已读通知（未读的不动）。"""
    check_origin(request)
    db.execute(
        delete(Notice).where(Notice.user_id == user.id, Notice.read_at.is_not(None))
    )
    db.commit()
    return {"unread": ns.unread_count(db, user.id)}


# --------------------------------------------------------------------------- #
# 好友
# --------------------------------------------------------------------------- #
@router.get("/friends")
def list_friends(db: DbSession, user: CurrentUser) -> dict:
    data = fsv.list_all(db, user)
    data["pending"] = ns.pending_for(db, user)
    # 已拉黑的人：好友关系已断，得单独给个入口才能解除
    data["blocked"] = chat_svc.list_blocks(db, user)
    return data


@router.get("/friends/search")
def search_friends(
    db: DbSession,
    user: CurrentUser,
    q: str = Query(default="", max_length=32),
) -> dict:
    return {"items": fsv.search(db, user, q)}


@router.post("/friends/requests", status_code=status.HTTP_201_CREATED)
def add_friend(
    payload: FriendRequestIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    result = fsv.request(db, user, payload.user_id, payload.message)
    db.commit()
    log_action(db, "friend_request", user.id, {"to": payload.user_id}, request)
    db.commit()
    return result


@router.post("/friends/requests/{request_id}/accept")
def accept_friend(request_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    result = fsv.accept(db, user, request_id)
    db.commit()
    log_action(db, "friend_accept", user.id, {"request": request_id}, request)
    db.commit()
    return result


@router.post("/friends/requests/{request_id}/reject")
def reject_friend(request_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    result = fsv.reject(db, user, request_id)
    db.commit()
    return result


@router.delete("/friends/{other_id}")
def remove_friend(other_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    result = fsv.remove(db, user, other_id)
    db.commit()
    log_action(db, "friend_remove", user.id, {"with": other_id}, request)
    db.commit()
    return result
