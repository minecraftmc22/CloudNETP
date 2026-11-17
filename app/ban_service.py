"""封禁状态与申诉：算到期、懒解封、查最近一条申诉。

要点：

- 到期时间是「库里的 naive UTC」，与 `utcnow()` 同一套口径；
- 不跑定时任务，`lift_expired()` 在登录 / 每个需要身份的请求里顺手检查和解除，
  这样长时间没人登录的账号也会在下一次访问时自动恢复；
- 封禁详情统一由 `info()` 输出成 camelCase，前端与接口共用。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import BanAppeal, User, utcnow
from .user_service import iso

# 永久封禁：ban_until 为空
PERMANENT = "permanent"
# 被ban时下发的申诉票据 cookie（auth 与 ban 路由共用这一个名字）
BAN_COOKIE = "cloudnetp_ban"


def stamp_of(user: User) -> str:
    """票据指纹：用会话版本号，改密会失效，解封后仍能回来看申诉结果。"""
    return str(int(user.token_version))


def is_expired(user: User) -> bool:
    """临时封禁是否已到期（永久封禁永远 False）。"""
    if not user.banned:
        return False
    if not user.ban_until:
        return False
    return user.ban_until <= utcnow()


def lift_expired(db: Session, user: User) -> bool:
    """到期就自动解封，返回是否真的解了。调用方负责 commit。"""
    if not is_expired(user):
        return False
    clear_ban(db, user)
    return True


def apply_ban(
    db: Session,
    user: User,
    *,
    reason: str,
    until=None,
    by_id: int | None = None,
) -> None:
    user.banned = True
    user.ban_reason = (reason or "").strip()[:200] or None
    user.banned_at = utcnow()
    user.ban_until = until
    user.banned_by = by_id
    db.add(user)


def clear_ban(db: Session, user: User) -> None:
    user.banned = False
    user.ban_reason = None
    user.banned_at = None
    user.ban_until = None
    user.banned_by = None
    db.add(user)


def info(user: User) -> dict[str, Any]:
    """给前端/接口用的封禁信息。"""
    until = user.ban_until
    return {
        "active": bool(user.banned),
        "reason": user.ban_reason or "",
        "bannedAt": iso(user.banned_at),
        "banUntil": iso(until),
        "permanent": bool(user.banned and not until),
        # 剩余秒数：临时封禁才有意义，前端可以就地显示倒计时
        "remainingSeconds": (
            max(0, int((until - utcnow()).total_seconds())) if user.banned and until else None
        ),
    }


def latest_appeal(db: Session, user_id: int) -> BanAppeal | None:
    return db.scalars(
        select(BanAppeal)
        .where(BanAppeal.user_id == user_id)
        .order_by(BanAppeal.created_at.desc(), BanAppeal.id.desc())
        .limit(1)
    ).first()


def pending_appeal(db: Session, user_id: int) -> BanAppeal | None:
    return db.scalars(
        select(BanAppeal)
        .where(BanAppeal.user_id == user_id, BanAppeal.status == "pending")
        .order_by(BanAppeal.id.desc())
        .limit(1)
    ).first()


def appeal_dict(row: BanAppeal | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "message": row.message,
        "status": row.status,
        "reply": row.reply or "",
        "banReason": row.ban_reason or "",
        "banUntil": iso(row.ban_until),
        "createdAt": iso(row.created_at),
        "handledAt": iso(row.handled_at),
    }


def ticket_payload(user: User, ticket: str) -> dict[str, Any]:
    """被ban用户能看到的全部信息（登录被拒 / 在线弹窗共用）。"""
    return {
        "code": "banned",
        "message": "你已被管理员封禁",
        "username": user.username,
        "ban": info(user),
        "ticket": ticket,
    }
