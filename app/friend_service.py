"""好友：搜索、申请、同意/拒绝、列表、删除。

关系存 ``friendships`` 一行一对用户（requester → addressee）：
- pending：等待 addressee 处理
- accepted：已是好友（不看方向）

拒绝即删行，以后还能再加；删除好友同理。
「拉黑」不在这里：它要长期记住（好友关系已断），存在 ``chat_blocks`` 表，
由 ``chat_service`` 维护；这里只在发好友申请时查一下对方有没有拉黑我。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from . import notice_service as ns
from .models import Friendship, User, utcnow
from .user_service import iso

MAX_FRIENDS = 500
MAX_PENDING_OUT = 30
SEARCH_LIMIT = 20


def card(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name or user.username,
        "avatarUrl": f"/api/avatar/{user.avatar}" if user.avatar else None,
        "bio": user.bio or "",
        "role": user.role,
    }


def _pair(a: int, b: int):
    return or_(
        and_(Friendship.requester_id == a, Friendship.addressee_id == b),
        and_(Friendship.requester_id == b, Friendship.addressee_id == a),
    )


def _users_by_ids(db: Session, ids: list[int]) -> dict[int, User]:
    if not ids:
        return {}
    rows = db.scalars(select(User).where(User.id.in_(ids))).all()
    return {u.id: u for u in rows}


def search(db: Session, me: User, query: str) -> list[dict[str, Any]]:
    text = (query or "").strip()
    if not text:
        return []
    # 用户输入里的 % 和 _ 是 LIKE 通配符，不转义的话搜「a_b」会命中一堆无关账号
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    rows = db.scalars(
        select(User)
        .where(
            User.id != me.id,
            User.banned.is_(False),
            or_(
                User.username.ilike(pattern, escape="\\"),
                User.display_name.ilike(pattern, escape="\\"),
            ),
        )
        .order_by(User.id)
        .limit(SEARCH_LIMIT)
    ).all()
    # 关系一次性查出来，避免每个结果都打一次库
    rel_rows = db.scalars(
        select(Friendship).where(
            or_(Friendship.requester_id == me.id, Friendship.addressee_id == me.id)
        )
    ).all()
    rel_map: dict[int, str] = {}
    for row in rel_rows:
        other = row.addressee_id if row.requester_id == me.id else row.requester_id
        if row.status == "accepted":
            rel_map[other] = "friends"
        else:
            rel_map[other] = "pending_out" if row.requester_id == me.id else "pending_in"
    # 我拉黑的人还给「添加」按钮就尴尬了；对方拉黑了我则不显示，免得暴露屏蔽关系
    from .models import ChatBlock

    blocked = {
        int(x)
        for x in db.scalars(
            select(ChatBlock.blocked_id).where(ChatBlock.blocker_id == me.id)
        ).all()
    }
    out = []
    for user in rows:
        item = card(user)
        item["relation"] = "blocked" if user.id in blocked else rel_map.get(user.id, "none")
        out.append(item)
    return out


def list_all(db: Session, me: User) -> dict[str, Any]:
    rows = db.scalars(
        select(Friendship)
        .where(or_(Friendship.requester_id == me.id, Friendship.addressee_id == me.id))
        .order_by(Friendship.id.desc())
    ).all()
    other_ids = [
        row.addressee_id if row.requester_id == me.id else row.requester_id for row in rows
    ]
    users = _users_by_ids(db, other_ids)
    friends: list[dict[str, Any]] = []
    incoming: list[dict[str, Any]] = []
    outgoing: list[dict[str, Any]] = []
    for row in rows:
        other_id = row.addressee_id if row.requester_id == me.id else row.requester_id
        user = users.get(other_id)
        if not user:
            continue  # 对方已注销：列表里不显示
        item = card(user)
        item["since"] = iso(row.updated_at or row.created_at)
        item["requestId"] = row.id
        item["message"] = row.message or ""
        if row.status == "accepted":
            friends.append(item)
        elif row.addressee_id == me.id:
            incoming.append(item)
        else:
            outgoing.append(item)
    return {"friends": friends, "incoming": incoming, "outgoing": outgoing}


def request(db: Session, me: User, target_id: int, message: str = "") -> dict[str, Any]:
    if int(target_id) == me.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能加自己为好友")
    target = db.get(User, int(target_id))
    if not target or target.banned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if is_blocked(db, me.id, target.id):
        # 不说是「被拉黑」：否则换个号试探一下就知道自己被谁屏蔽了
        raise HTTPException(status.HTTP_409_CONFLICT, "对方暂时无法接收好友申请")
    existing = db.scalar(select(Friendship).where(_pair(me.id, target.id)).limit(1))
    if existing:
        if existing.status == "accepted":
            raise HTTPException(status.HTTP_409_CONFLICT, "你们已经是好友了")
        if existing.requester_id == me.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "已经发过申请，等对方处理就好")
        # 对方先发的申请 → 直接当作同意，省一次来回
        existing.status = "accepted"
        existing.updated_at = utcnow()
        ns.notify(
            db,
            existing.requester_id,
            "friend_accept",
            f"{me.display_name or me.username} 通过了你的好友申请",
            "",
            "#/friends",
        )
        return {"relation": "friends", "autoAccepted": True}
    if len(list_all(db, me)["friends"]) >= MAX_FRIENDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"好友数量已达上限（{MAX_FRIENDS}）")
    # 防止拿好友申请刷别人的消息中心
    pending_out = int(
        db.scalar(
            select(func.count())
            .select_from(Friendship)
            .where(Friendship.requester_id == me.id, Friendship.status == "pending")
        )
        or 0
    )
    if pending_out >= MAX_PENDING_OUT:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "待对方处理的申请太多了，先等等吧")
    row = Friendship(
        requester_id=me.id,
        addressee_id=target.id,
        status="pending",
        message=(message or "").strip()[:120],
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(row)
    db.flush()
    ns.notify(
        db,
        target.id,
        "friend_request",
        f"{me.display_name or me.username} 想加你为好友",
        row.message,
        "#/friends",
    )
    return {"relation": "pending_out", "requestId": row.id}


def accept(db: Session, me: User, request_id: int) -> dict[str, Any]:
    row = db.get(Friendship, int(request_id))
    if not row or row.addressee_id != me.id or row.status != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "申请不存在或已处理")
    row.status = "accepted"
    row.updated_at = utcnow()
    ns.notify(
        db,
        row.requester_id,
        "friend_accept",
        f"{me.display_name or me.username} 通过了你的好友申请",
        "",
        "#/friends",
    )
    return {"ok": True}


def reject(db: Session, me: User, request_id: int) -> dict[str, Any]:
    row = db.get(Friendship, int(request_id))
    if not row or row.addressee_id != me.id or row.status != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "申请不存在或已处理")
    db.delete(row)
    return {"ok": True}


def remove(db: Session, me: User, other_id: int) -> dict[str, Any]:
    row = db.scalar(select(Friendship).where(_pair(me.id, int(other_id))).limit(1))
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "你们还不是好友")
    db.delete(row)
    return {"ok": True}


def remove_between(db: Session, a: int, b: int) -> bool:
    """无声地删掉两人的好友关系（拉黑用）。没有关系也当成功。"""
    row = db.scalar(select(Friendship).where(_pair(int(a), int(b))).limit(1))
    if not row:
        return False
    db.delete(row)
    return True


def is_blocked(db: Session, a: int, b: int) -> bool:
    """b 是否拉黑了 a（申请好友时用来静默拒绝）。"""
    from .models import ChatBlock

    return (
        db.get(ChatBlock, {"blocker_id": int(b), "blocked_id": int(a)}) is not None
    )
