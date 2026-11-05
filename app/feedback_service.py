"""意见反馈：提交、个人记录、管理员列表与状态流转。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Feedback, User, utcnow

KIND_LABELS = {"suggestion": "建议", "bug": "问题", "other": "其他"}
STATUS_LABELS = {0: "待处理", 1: "已处理"}


def _iso(value) -> str | None:
    return value.isoformat() + "Z" if value else None


def payload(row: Feedback, user: User | None = None) -> dict:
    data = {
        "id": row.id,
        "userId": row.user_id,
        "kind": row.kind,
        "kindLabel": KIND_LABELS.get(row.kind, row.kind),
        "title": row.title,
        "content": row.content,
        "contact": row.contact,
        "status": row.status,
        "statusLabel": STATUS_LABELS.get(row.status, "未知"),
        "createdAt": _iso(row.created_at),
        "handledAt": _iso(row.handled_at),
    }
    if user is not None:
        data["username"] = user.username
        data["displayName"] = user.display_name or user.username
    return data


def create(
    db: Session,
    user: User,
    *,
    kind: str,
    title: str,
    content: str,
    contact: str,
) -> Feedback:
    row = Feedback(
        user_id=user.id,
        kind=kind,
        title=title.strip(),
        content=content.strip(),
        contact=contact.strip(),
    )
    db.add(row)
    db.flush()
    return row


def latest_for_user(db: Session, user_id: int) -> Feedback | None:
    return db.scalars(
        select(Feedback)
        .where(Feedback.user_id == user_id)
        .order_by(Feedback.created_at.desc())
        .limit(1)
    ).first()


def list_mine(db: Session, user_id: int, limit: int = 30) -> list[dict]:
    rows = db.scalars(
        select(Feedback)
        .where(Feedback.user_id == user_id)
        .order_by(Feedback.created_at.desc())
        .limit(limit)
    ).all()
    return [payload(row) for row in rows]


def list_all(db: Session, *, status: str, page: int, page_size: int) -> dict:
    stmt = select(Feedback, User).join(User, User.id == Feedback.user_id, isouter=True)
    count_stmt = select(func.count()).select_from(Feedback)
    if status == "pending":
        stmt = stmt.where(Feedback.status == 0)
        count_stmt = count_stmt.where(Feedback.status == 0)
    elif status == "resolved":
        stmt = stmt.where(Feedback.status == 1)
        count_stmt = count_stmt.where(Feedback.status == 1)

    total = int(db.scalar(count_stmt) or 0)
    rows = db.execute(
        stmt.order_by(Feedback.status.asc(), Feedback.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [payload(row, owner) for row, owner in rows],
        "total": total,
        "page": page,
        "pageSize": page_size,
    }


def get(db: Session, feedback_id: int) -> Feedback | None:
    return db.get(Feedback, feedback_id)


def set_status(db: Session, row: Feedback, status: int, admin_id: int) -> None:
    row.status = status
    row.handled_by = admin_id if status else None
    row.handled_at = utcnow() if status else None
