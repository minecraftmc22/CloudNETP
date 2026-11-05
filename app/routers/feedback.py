"""意见反馈：登录用户提交 / 查看自己的反馈；管理员查看与处理。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from .. import feedback_service as fs
from ..db import get_db
from ..deps import get_current_user, require_admin
from ..models import Feedback, User, utcnow
from ..schemas import FeedbackIn, FeedbackStatusIn
from ..security import check_origin
from ..user_service import log_action

router = APIRouter(prefix="/api", tags=["feedback"])

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]


@router.post("/feedback", status_code=status.HTTP_201_CREATED)
def submit_feedback(
    payload: FeedbackIn,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    check_origin(request)
    last = fs.latest_for_user(db, user.id)
    if last and (utcnow() - last.created_at).total_seconds() < 45:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "刚刚已经提交过一条了，稍等片刻再发")
    row = fs.create(
        db,
        user,
        kind=payload.kind,
        title=payload.title,
        content=payload.content,
        contact=payload.contact,
    )
    db.commit()
    db.refresh(row)
    log_action(db, "feedback_create", user.id, {"id": row.id, "kind": row.kind}, request)
    db.commit()
    return {"item": fs.payload(row)}


@router.get("/feedback")
def my_feedback(db: DbSession, user: CurrentUser) -> dict:
    return {"items": fs.list_mine(db, user.id)}


@router.get("/admin/feedback")
def list_feedback(
    db: DbSession,
    _: AdminUser,
    feedback_status: str = Query(default="pending", pattern="^(pending|resolved|all)$", alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="pageSize"),
) -> dict:
    return fs.list_all(db, status=feedback_status, page=page, page_size=page_size)


@router.patch("/admin/feedback/{feedback_id}")
def update_feedback(
    feedback_id: int,
    payload: FeedbackStatusIn,
    request: Request,
    db: DbSession,
    me: AdminUser,
) -> dict:
    check_origin(request)
    row: Feedback | None = fs.get(db, feedback_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "反馈不存在")
    fs.set_status(db, row, payload.status, me.id)
    db.commit()
    db.refresh(row)
    log_action(db, "feedback_status", me.id, {"id": row.id, "status": payload.status}, request)
    db.commit()
    owner = db.get(User, row.user_id)
    return {"item": fs.payload(row, owner)}


@router.delete("/admin/feedback/{feedback_id}")
def delete_feedback(
    feedback_id: int,
    request: Request,
    db: DbSession,
    me: AdminUser,
) -> dict:
    check_origin(request)
    row: Feedback | None = fs.get(db, feedback_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "反馈不存在")
    db.delete(row)
    db.commit()
    log_action(db, "feedback_delete", me.id, {"id": feedback_id}, request)
    db.commit()
    return {"deleted": True}
