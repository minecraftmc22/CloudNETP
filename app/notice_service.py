"""消息中心：通知落库、未读统计、管理端「待处理」汇总。

三条约定：
- 通知按收件人一行一条（``Notice.user_id``）。管理类事件发生时按管理员名单展开，
  量很小（举报/申诉/反馈都是低频），换来「已读」语义简单、列表查询不用 join。
- 收件人被删时行随 FK CASCADE 一起走；``admin_service.purge_user`` 里也显式删一次，
  因为 SQLite 建表顺序不同时外键不一定带得动级联。
- 「待处理」数字不落库，直接数源表（pending 状态），保证后台处理完立刻归零。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from .models import (
    BanAppeal,
    ChatReport,
    Feedback,
    Friendship,
    Notice,
    ShareReport,
    User,
    utcnow,
)
from .user_service import iso

# 只给管理员看的通知类型
ADMIN_KINDS = ("report", "appeal", "feedback")
LIST_LIMIT = 60


def admin_ids(db: Session) -> list[int]:
    return list(
        db.scalars(
            select(User.id).where(
                or_(User.role == 1, User.is_super.is_(True)),
                User.banned.is_(False),
            )
        ).all()
    )


def notify(
    db: Session,
    user_id: int,
    kind: str,
    title: str,
    body: str = "",
    link: str = "",
) -> Notice:
    row = Notice(
        user_id=int(user_id),
        kind=kind[:24],
        title=title[:120],
        body=(body or "")[:500],
        link=(link or "")[:160],
        created_at=utcnow(),
    )
    db.add(row)
    return row


def notify_admins(db: Session, kind: str, title: str, body: str = "", link: str = "") -> int:
    """给所有管理员各发一条；返回实际写入条数。"""
    n = 0
    for uid in admin_ids(db):
        notify(db, uid, kind, title, body, link)
        n += 1
    return n


def payload(row: Notice) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "title": row.title,
        "body": row.body or "",
        "link": row.link or "",
        "read": row.read_at is not None,
        "createdAt": iso(row.created_at),
    }


def list_for(
    db: Session, user_id: int, *, limit: int = LIST_LIMIT, unread_only: bool = False
) -> list[dict[str, Any]]:
    stmt = select(Notice).where(Notice.user_id == user_id)
    if unread_only:
        stmt = stmt.where(Notice.read_at.is_(None))
    stmt = stmt.order_by(Notice.id.desc()).limit(max(1, min(limit, 200)))
    return [payload(row) for row in db.scalars(stmt).all()]


def unread_count(db: Session, user_id: int) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Notice)
            .where(Notice.user_id == user_id, Notice.read_at.is_(None))
        )
        or 0
    )


def mark_read(db: Session, user_id: int, ids: list[int] | None = None) -> int:
    """ids 为空 = 全部标记已读；返回本次影响行数。"""
    stmt = update(Notice).where(Notice.user_id == user_id, Notice.read_at.is_(None))
    if ids:
        stmt = stmt.where(Notice.id.in_([int(i) for i in ids]))
    result = db.execute(stmt.values(read_at=utcnow()))
    return int(result.rowcount or 0)


def pending_for(db: Session, user: User) -> dict[str, int]:
    """未处理事项计数：管理员看审核队列，所有人看好友申请。"""
    incoming = int(
        db.scalar(
            select(func.count())
            .select_from(Friendship)
            .where(Friendship.addressee_id == user.id, Friendship.status == "pending")
        )
        or 0
    )
    data = {"friendRequests": incoming}
    if user.role != 1 and not user.is_super:
        return data
    data["reports"] = int(
        db.scalar(
            select(func.count()).select_from(ShareReport).where(ShareReport.status == "pending")
        )
        or 0
    )
    data["appeals"] = int(
        db.scalar(
            select(func.count()).select_from(BanAppeal).where(BanAppeal.status == "pending")
        )
        or 0
    )
    data["feedback"] = int(
        db.scalar(select(func.count()).select_from(Feedback).where(Feedback.status == 0)) or 0
    )
    data["chatReports"] = int(
        db.scalar(
            select(func.count()).select_from(ChatReport).where(ChatReport.status == "pending")
        )
        or 0
    )
    return data
