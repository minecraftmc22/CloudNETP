"""站内聊天：单聊 / 群聊、图片、表情包、发网盘文件、撤回、转发、举报。

设计要点
--------
1. **消息只存引用，不复制文件**。发文件时把发送方节点快照（名字/大小/mime）写进
   payload，接收方点「下载」时按消息 id + 序号回源读取，点「保存」才真正复制进自己
   的网盘（复用 blob，只增引用计数）。
2. **撤回不删行**。置 ``recalled_at`` 并清空正文/payload，前端显示「已撤回」；
   举报时把当时内容抄进 ``chat_reports.snapshot``，撤回洗不掉证据。
3. **未读用 last_read_id 算**。不做 per-message 已读回执（群里 N 人 × M 条会炸），
   只记「读到哪一条」，未读数按它之后的消息数统计，一条 GROUP BY 查完全部会话。
4. **看图/表情包不进 blob 表**。它们不占用户配额，落 ``storage/chat/``，
   文件名随机，`/api/chat/media/<name>` 需要登录才能取。
"""

from __future__ import annotations

import io
import json
import re
import secrets
from typing import Any

from fastapi import HTTPException, status
from PIL import Image, ImageOps
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from . import friend_service as fsv
from . import node_service as ns
from . import notice_service as notice
from .config import settings
from .models import (
    ChatConversation,
    ChatMember,
    ChatMessage,
    ChatReport,
    Node,
    Sticker,
    User,
    utcnow,
)
from .settings_store import get_int
from .user_service import iso

MAX_TEXT = 2000
MAX_FILES = 20
HISTORY_LIMIT = 30
HISTORY_MAX = 100
MAX_GROUPS = 60
SEARCH_LIMIT = 20

IMAGE_MAX_BYTES = 8 * 1024 * 1024
STICKER_MAX_BYTES = 2 * 1024 * 1024
IMAGE_MAX_SIDE = 1600
STICKER_MAX_SIDE = 512

MEDIA_RE = re.compile(r"^(img|stk)-[0-9a-f]{16}\.(webp|png)$")

DEFAULT_STICKER_MAX = 100
DEFAULT_GROUP_MAX = 200
DEFAULT_RECALL_MINUTES = 10


# --------------------------------------------------------------------------- #
# 配置项（后台可改，读不到就用默认值）
# --------------------------------------------------------------------------- #
def sticker_max(db: Session) -> int:
    return max(1, min(2000, get_int(db, "sticker_max_per_user") or DEFAULT_STICKER_MAX))


def group_max(db: Session) -> int:
    return max(2, min(2000, get_int(db, "chat_group_max_members") or DEFAULT_GROUP_MAX))


def recall_minutes(db: Session) -> int:
    """0 = 不限制撤回时间。"""
    value = get_int(db, "chat_recall_minutes")
    if value <= 0:
        return 0
    return min(1440, value)


# --------------------------------------------------------------------------- #
# 基础查询
# --------------------------------------------------------------------------- #
def member_of(db: Session, conversation_id: int, user_id: int) -> ChatMember | None:
    return db.get(ChatMember, {"conversation_id": int(conversation_id), "user_id": int(user_id)})


def require_member(db: Session, conversation_id: int, user: User) -> tuple[ChatConversation, ChatMember]:
    conv = db.get(ChatConversation, int(conversation_id))
    if not conv:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    member = member_of(db, conv.id, user.id)
    if not member:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "你不在这个会话里")
    return conv, member


def user_card(user: User | None) -> dict[str, Any]:
    if user is None:
        return {"id": 0, "username": "", "displayName": "已注销用户", "avatarUrl": None}
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name or user.username,
        "avatarUrl": f"/api/avatar/{user.avatar}" if user.avatar else None,
    }


def _name_of(user: User | None) -> str:
    if user is None:
        return "已注销用户"
    return (user.display_name or "").strip() or user.username


def _users(db: Session, ids: list[int]) -> dict[int, User]:
    ids = [int(i) for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    return {u.id: u for u in db.scalars(select(User).where(User.id.in_(ids))).all()}


def members_of(db: Session, conv_id: int) -> list[ChatMember]:
    return list(
        db.scalars(
            select(ChatMember)
            .where(ChatMember.conversation_id == conv_id)
            .order_by(ChatMember.joined_at, ChatMember.user_id)
        ).all()
    )


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
def _conv_payload(
    db: Session,
    conv: ChatConversation,
    me: User,
    *,
    unread: int = 0,
    members: list[ChatMember] | None = None,
    last: ChatMessage | None = None,
) -> dict[str, Any]:
    rows = members if members is not None else members_of(db, conv.id)
    users = _users(db, [row.user_id for row in rows])
    people = [user_card(users.get(row.user_id)) for row in rows]
    mine = next((p for p in people if p["id"] == me.id), None)
    if conv.kind == "direct":
        peer = next((p for p in people if p["id"] != me.id), None)
        title = peer["displayName"] if peer else "已注销用户"
        avatar_url = peer["avatarUrl"] if peer else None
    else:
        title = conv.title or "群聊"
        avatar_url = None

    preview = ""
    if last is not None:
        preview = _preview(last)
    return {
        "id": conv.id,
        "kind": conv.kind,
        "title": title,
        "avatarUrl": avatar_url,
        "ownerId": conv.owner_id,
        "isOwner": conv.owner_id == me.id,
        "members": people,
        "memberCount": len(people),
        "me": mine,
        "lastMessage": preview,
        "lastMessageAt": iso(conv.last_message_at or conv.created_at),
        "unread": int(unread or 0),
        "createdAt": iso(conv.created_at),
    }


def _preview(msg: ChatMessage) -> str:
    if msg.recalled_at:
        return "消息已撤回"
    if msg.kind == "image":
        return "[图片]"
    if msg.kind == "sticker":
        return "[表情]"
    if msg.kind == "file":
        try:
            data = json.loads(msg.payload or "{}")
            count = int(data.get("count") or len(data.get("nodes") or []) or 1)
            name = (data.get("nodes") or [{}])[0].get("name") or "文件"
            return f"[文件] {name}" if count <= 1 else f"[文件] {name} 等 {count} 项"
        except Exception:
            return "[文件]"
    return (msg.body or "").replace("\n", " ")[:80]


def unread_map(db: Session, me: User) -> dict[int, int]:
    rows = db.execute(
        select(ChatMessage.conversation_id, func.count())
        .join(
            ChatMember,
            and_(
                ChatMember.conversation_id == ChatMessage.conversation_id,
                ChatMember.user_id == me.id,
            ),
        )
        .where(
            ChatMessage.id > ChatMember.last_read_id,
            ChatMessage.sender_id != me.id,
            # 系统消息（建群、退群）不该把人叫起来：不进未读
            ChatMessage.kind != "system",
        )
        .group_by(ChatMessage.conversation_id)
    ).all()
    return {int(cid): int(n) for cid, n in rows}


def list_conversations(db: Session, me: User) -> dict[str, Any]:
    conv_ids = list(
        db.scalars(
            select(ChatMember.conversation_id).where(ChatMember.user_id == me.id)
        ).all()
    )
    if not conv_ids:
        return {"items": [], "unread": 0}
    convs = list(
        db.scalars(
            select(ChatConversation)
            .where(ChatConversation.id.in_(conv_ids))
            .order_by(
                func.coalesce(ChatConversation.last_message_at, ChatConversation.created_at).desc()
            )
            .limit(200)
        ).all()
    )
    unread = unread_map(db, me)
    conv_ids = [c.id for c in convs]
    rows = list(
        db.scalars(select(ChatMember).where(ChatMember.conversation_id.in_(conv_ids))).all()
    )
    by_conv: dict[int, list[ChatMember]] = {}
    for row in rows:
        by_conv.setdefault(row.conversation_id, []).append(row)

    last_ids = [c.last_message_id for c in convs if c.last_message_id]
    lasts: dict[int, ChatMessage] = {}
    if last_ids:
        for msg in db.scalars(select(ChatMessage).where(ChatMessage.id.in_(last_ids))).all():
            lasts[msg.id] = msg

    items = [
        _conv_payload(
            db,
            conv,
            me,
            unread=unread.get(conv.id, 0),
            members=by_conv.get(conv.id, []),
            last=lasts.get(conv.last_message_id) if conv.last_message_id else None,
        )
        for conv in convs
    ]
    return {"items": items, "unread": sum(unread.values())}


def friends_for_picker(db: Session, me: User) -> list[dict[str, Any]]:
    """新建单聊 / 群聊 / 快传选人用的好友简表。"""
    data = fsv.list_all(db, me)
    return data.get("friends", [])


def get_or_create_direct(db: Session, me: User, other_id: int) -> ChatConversation:
    other_id = int(other_id)
    if other_id == me.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能和自己聊天")
    other = db.get(User, other_id)
    if not other or other.banned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if not _are_friends(db, me.id, other_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "先加好友才能聊天")
    key = f"{min(me.id, other_id)}-{max(me.id, other_id)}"
    conv = db.scalar(select(ChatConversation).where(ChatConversation.direct_key == key))
    if conv:
        return conv
    conv = ChatConversation(
        kind="direct",
        title="",
        owner_id=me.id,
        direct_key=key,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(conv)
    db.flush()
    db.add_all(
        [
            ChatMember(conversation_id=conv.id, user_id=me.id, role="member"),
            ChatMember(conversation_id=conv.id, user_id=other_id, role="member"),
        ]
    )
    db.flush()
    return conv


def _are_friends(db: Session, a: int, b: int) -> bool:
    from .models import Friendship

    row = db.scalar(
        select(Friendship)
        .where(
            Friendship.status == "accepted",
            or_(
                and_(Friendship.requester_id == a, Friendship.addressee_id == b),
                and_(Friendship.requester_id == b, Friendship.addressee_id == a),
            ),
        )
        .limit(1)
    )
    return row is not None


def create_group(
    db: Session, me: User, title: str, member_ids: list[int]
) -> ChatConversation:
    limit = group_max(db)
    ids = [int(i) for i in dict.fromkeys(member_ids or []) if int(i) != me.id]
    if not ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "至少选一位好友")
    if len(ids) + 1 > limit:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"群成员上限是 {limit} 人")
    owned = int(
        db.scalar(
            select(func.count())
            .select_from(ChatMember)
            .join(ChatConversation, ChatConversation.id == ChatMember.conversation_id)
            .where(ChatMember.user_id == me.id, ChatConversation.kind == "group")
        )
        or 0
    )
    if owned >= MAX_GROUPS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"最多加入 {MAX_GROUPS} 个群聊")

    users = _users(db, ids)
    if len(users) != len(ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "有成员不存在")
    for uid, user in users.items():
        if user.banned:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"「{_name_of(user)}」已被封禁")
        if not _are_friends(db, me.id, uid):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"「{_name_of(user)}」还不是你的好友"
            )

    name = (title or "").strip()[:60]
    if not name:
        name = "、".join([_name_of(me)] + [_name_of(u) for u in users.values()])[:60]
    conv = ChatConversation(
        kind="group", title=name, owner_id=me.id, created_at=utcnow(), updated_at=utcnow()
    )
    db.add(conv)
    db.flush()
    db.add(ChatMember(conversation_id=conv.id, user_id=me.id, role="owner"))
    for uid in ids:
        db.add(ChatMember(conversation_id=conv.id, user_id=uid, role="member"))
    db.flush()
    _system_message(db, conv, f"{_name_of(me)} 创建了群聊")
    return conv


def rename_group(db: Session, me: User, conversation_id: int, title: str) -> ChatConversation:
    conv, member = require_member(db, conversation_id, me)
    if conv.kind != "group":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "单聊没有群名")
    if conv.owner_id != me.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有群主能改群名")
    name = (title or "").strip()[:60]
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "群名不能为空")
    conv.title = name
    conv.updated_at = utcnow()
    _system_message(db, conv, f"群名改为「{name}」")
    return conv


def add_members(db: Session, me: User, conversation_id: int, ids: list[int]) -> ChatConversation:
    conv, _ = require_member(db, conversation_id, me)
    if conv.kind != "group":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "单聊不能加人")
    if conv.owner_id != me.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有群主能邀请成员")
    current = members_of(db, conv.id)
    have = {row.user_id for row in current}
    limit = group_max(db)
    fresh = [int(i) for i in dict.fromkeys(ids or []) if int(i) not in have]
    if not fresh:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "这些人都已经在群里了")
    if len(have) + len(fresh) > limit:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"群成员上限是 {limit} 人")
    users = _users(db, fresh)
    if len(users) != len(fresh):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "有用户不存在")
    for uid, user in users.items():
        if user.banned:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"「{_name_of(user)}」已被封禁")
        if not _are_friends(db, me.id, uid):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"「{_name_of(user)}」还不是你的好友")
        db.add(ChatMember(conversation_id=conv.id, user_id=uid, role="member"))
    db.flush()
    _system_message(db, conv, f"{_name_of(me)} 邀请了 " + "、".join(_name_of(u) for u in users.values()))
    for uid in fresh:
        notice.notify(
            db, uid, "chat", f"{_name_of(me)} 邀请你加入群聊「{conv.title}」", "", f"#/chat/{conv.id}"
        )
    return conv


def remove_member(db: Session, me: User, conversation_id: int, user_id: int) -> dict[str, Any]:
    conv, _ = require_member(db, conversation_id, me)
    if conv.kind != "group":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "单聊不能踢人")
    user_id = int(user_id)
    if user_id == me.id:
        return leave_group(db, me, conversation_id)
    if conv.owner_id != me.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有群主能移除成员")
    row = member_of(db, conv.id, user_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "他不在这个群里")
    db.delete(row)
    db.flush()
    target = db.get(User, user_id)
    _system_message(db, conv, f"{_name_of(target)} 被移出了群聊")
    notice.notify(
        db, user_id, "chat", f"你已被移出群聊「{conv.title}」", "", "#/chat"
    )
    return {"ok": True, "left": True}


def leave_group(db: Session, me: User, conversation_id: int) -> dict[str, Any]:
    conv, row = require_member(db, conversation_id, me)
    if conv.kind != "group":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "单聊不能退出，删好友即可")
    db.delete(row)
    db.flush()
    rows = members_of(db, conv.id)
    _system_message(db, conv, f"{_name_of(me)} 退出了群聊")
    if not rows:
        db.delete(conv)
        return {"ok": True, "closed": True}
    # 群主退群 → 顺位交给最早加入的人，否则没人能邀请/移除
    if conv.owner_id == me.id:
        heir = rows[0]
        heir.role = "owner"
        conv.owner_id = heir.user_id
        _system_message(db, conv, f"{_name_of(db.get(User, heir.user_id))} 成为新群主")
    return {"ok": True, "left": True}


# --------------------------------------------------------------------------- #
# 消息
# --------------------------------------------------------------------------- #
def _system_message(db: Session, conv: ChatConversation, text: str) -> ChatMessage:
    msg = ChatMessage(
        conversation_id=conv.id,
        sender_id=0,
        kind="system",
        body=text,
        created_at=utcnow(),
    )
    db.add(msg)
    db.flush()
    conv.last_message_id = msg.id
    conv.last_message_at = msg.created_at
    conv.updated_at = msg.created_at
    return msg


def message_payload(
    db: Session, msg: ChatMessage, senders: dict[int, User] | None = None
) -> dict[str, Any]:
    if senders is not None and msg.sender_id in senders:
        sender = senders[msg.sender_id]
    elif msg.sender_id:
        sender = db.get(User, msg.sender_id)
    else:
        sender = None
    recalled = msg.recalled_at is not None
    payload = None
    if not recalled and msg.payload:
        try:
            payload = json.loads(msg.payload)
        except Exception:
            payload = None
    return {
        "id": msg.id,
        "conversationId": msg.conversation_id,
        "kind": msg.kind,
        "body": "" if recalled else (msg.body or ""),
        "payload": payload,
        "sender": user_card(sender) if msg.sender_id else None,
        "recalled": recalled,
        "forwardedFrom": msg.forwarded_from,
        "createdAt": iso(msg.created_at),
    }


def list_messages(
    db: Session,
    me: User,
    conversation_id: int,
    *,
    before_id: int | None = None,
    after_id: int | None = None,
    limit: int = HISTORY_LIMIT,
) -> dict[str, Any]:
    conv, _ = require_member(db, conversation_id, me)
    limit = max(1, min(HISTORY_MAX, int(limit)))
    stmt = select(ChatMessage).where(ChatMessage.conversation_id == conv.id)
    if after_id:
        stmt = stmt.where(ChatMessage.id > int(after_id)).order_by(ChatMessage.id).limit(limit)
    elif before_id:
        stmt = stmt.where(ChatMessage.id < int(before_id)).order_by(ChatMessage.id.desc()).limit(limit)
    else:
        stmt = stmt.order_by(ChatMessage.id.desc()).limit(limit)
    rows = list(db.scalars(stmt).all())
    if not after_id:
        rows.reverse()
    senders = _users(db, [m.sender_id for m in rows if m.sender_id])
    return {
        "items": [message_payload(db, m, senders) for m in rows],
        "hasMore": len(rows) == limit and not after_id,
    }


def _check_text(body: str) -> str:
    text = (body or "").strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "消息不能为空")
    if len(text) > MAX_TEXT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"消息最多 {MAX_TEXT} 字")
    return text


def _file_snapshot(db: Session, me: User, node_ids: list[int]) -> dict[str, Any]:
    ids = [int(i) for i in dict.fromkeys(node_ids or [])]
    if not ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有选择文件")
    if len(ids) > MAX_FILES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"一次最多发 {MAX_FILES} 项")
    nodes = list(db.scalars(select(Node).where(Node.id.in_(ids))).all())
    by_id = {n.id: n for n in nodes}
    items: list[dict[str, Any]] = []
    total = 0
    for node_id in ids:
        node = by_id.get(node_id)
        if not node or node.user_id != me.id or node.deleted_at or node.quick_owned:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "有文件不存在或已删除")
        if node.pending_send_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "待接收的快传文件不能转发")
        if node.type == "folder":
            size = int(
                sum(
                    int(n.size or 0)
                    for n in db.scalars(
                        select(Node).where(
                            Node.id.in_(ns.subtree_ids(db, me.id, node.id)), Node.type == "file"
                        )
                    ).all()
                )
            )
        else:
            size = int(node.size or 0)
        total += size
        items.append(
            {
                "id": node.id,
                "name": node.name,
                "size": size,
                "mime": node.mime or "",
                "folder": node.type == "folder",
                "icon": ns.icon_for(node),
            }
        )
    return {"nodes": items, "count": len(items), "size": total}


def send_message(
    db: Session,
    me: User,
    conversation_id: int,
    *,
    kind: str = "text",
    body: str = "",
    payload: dict[str, Any] | None = None,
) -> ChatMessage:
    conv, _ = require_member(db, conversation_id, me)
    if conv.kind == "direct":
        rows = members_of(db, conv.id)
        peer_id = next((row.user_id for row in rows if row.user_id != me.id), None)
        if peer_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "对方已注销，会话不可用")
        if not _are_friends(db, me.id, peer_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "你们已经不是好友，无法继续聊天")

    kind = (kind or "text").strip()
    text = ""
    data: dict[str, Any] | None = None
    if kind == "text":
        text = _check_text(body)
    elif kind == "image":
        name = str((payload or {}).get("name") or "")
        if not MEDIA_RE.fullmatch(name) or not name.startswith("img-"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "图片参数不对")
        path = settings.chat_dir / name
        if not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "图片已失效，请重新发送")
        data = {
            "name": name,
            "width": int((payload or {}).get("width") or 0),
            "height": int((payload or {}).get("height") or 0),
            "size": path.stat().st_size,
        }
    elif kind == "sticker":
        sticker_id = (payload or {}).get("stickerId")
        sticker = db.get(Sticker, int(sticker_id)) if sticker_id else None
        if not sticker:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "表情不存在")
        # 群里别人用过的表情也能显示，所以不校验归属
        data = {"name": sticker.file, "stickerId": sticker.id}
    elif kind == "file":
        data = _file_snapshot(db, me, list((payload or {}).get("nodeIds") or []))
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不支持的消息类型")

    msg = ChatMessage(
        conversation_id=conv.id,
        sender_id=me.id,
        kind=kind,
        body=text,
        payload=json.dumps(data, ensure_ascii=False) if data else None,
        created_at=utcnow(),
    )
    db.add(msg)
    db.flush()
    was_read = _peer_all_read(db, conv, me)
    conv.last_message_id = msg.id
    conv.last_message_at = msg.created_at
    conv.updated_at = msg.created_at
    if conv.kind == "direct" and was_read:
        rows = members_of(db, conv.id)
        for row in rows:
            if row.user_id == me.id:
                continue
            notice.notify(
                db,
                row.user_id,
                "chat",
                f"{_name_of(me)} 给你发了消息",
                _preview(msg),
                f"#/chat/{conv.id}",
            )
    db.flush()
    return msg


def _peer_all_read(db: Session, conv: ChatConversation, me: User) -> bool:
    """对方是否已经把上一条读完了 —— 用来把「新消息」通知压成一次，而不是每条都响。"""
    if not conv.last_message_id:
        return True
    rows = members_of(db, conv.id)
    for row in rows:
        if row.user_id == me.id:
            continue
        if int(row.last_read_id or 0) < int(conv.last_message_id):
            return False
    return True


def mark_read(db: Session, me: User, conversation_id: int, message_id: int | None) -> dict[str, Any]:
    conv, row = require_member(db, conversation_id, me)
    target = int(message_id or 0)
    if target <= 0:
        target = int(conv.last_message_id or 0)
    if target > int(row.last_read_id or 0):
        row.last_read_id = target
    return {"unread": unread_map(db, me)}


def recall(db: Session, me: User, message_id: int) -> ChatMessage:
    msg = db.get(ChatMessage, int(message_id))
    if not msg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "消息不存在")
    require_member(db, msg.conversation_id, me)
    if msg.sender_id != me.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能撤回自己发的消息")
    if msg.kind == "system":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "系统消息不能撤回")
    if msg.recalled_at:
        return msg
    minutes = recall_minutes(db)
    if minutes:
        age = (utcnow() - msg.created_at).total_seconds()
        if age > minutes * 60:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"超过 {minutes} 分钟就不能撤回了"
            )
    msg.recalled_at = utcnow()
    msg.body = ""
    msg.payload = None
    return msg


def forward(db: Session, me: User, message_id: int, conversation_ids: list[int]) -> dict[str, Any]:
    src = db.get(ChatMessage, int(message_id))
    if not src:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "消息不存在")
    require_member(db, src.conversation_id, me)
    if src.recalled_at:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "消息已撤回，转发不了")
    if src.kind == "system":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "系统消息不能转发")
    targets = [int(i) for i in dict.fromkeys(conversation_ids or [])]
    if not targets:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "选一个要转发到的会话")
    if len(targets) > 10:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "一次最多转发到 10 个会话")

    # 文件消息转发时重新快照：原发送者的节点对转发者不可见，
    # 所以只把「名字/大小」带过去，接收方点下载会走转发者这边？
    # 不行——文件只有原发送者能读。故文件消息转发时直接把文件复制进转发者网盘再发。
    payload: dict[str, Any] | None = None
    if src.payload:
        try:
            payload = json.loads(src.payload)
        except Exception:
            payload = None
    files: list[Node] = []
    if src.kind == "file" and payload:
        ids = [int(n.get("id") or 0) for n in payload.get("nodes") or []]
        files = [
            n
            for n in db.scalars(select(Node).where(Node.id.in_(ids))).all()
            if n.user_id == me.id and not n.deleted_at
        ]
        # 别人的文件先落到自己网盘（复用 blob），否则转发出去对方也下载不了
        if len(files) != len(ids):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "这是他人分享的文件，请先「保存到我的文件」再转发"
            )

    created: list[int] = []
    for conv_id in targets:
        conv, _ = require_member(db, conv_id, me)
        if conv.kind == "direct":
            rows = members_of(db, conv.id)
            peer_id = next((r.user_id for r in rows if r.user_id != me.id), None)
            if peer_id is None or not _are_friends(db, me.id, peer_id):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "有会话已不是好友关系")
        msg = ChatMessage(
            conversation_id=conv.id,
            sender_id=me.id,
            kind=src.kind if src.kind in ("text", "image", "file", "sticker") else "text",
            body=src.body or "",
            payload=json.dumps(payload, ensure_ascii=False) if payload else None,
            forwarded_from=src.id,
            created_at=utcnow(),
        )
        db.add(msg)
        db.flush()
        conv.last_message_id = msg.id
        conv.last_message_at = msg.created_at
        conv.updated_at = msg.created_at
        created.append(msg.id)
    return {"created": created, "count": len(created)}


def save_files(db: Session, me: User, message_id: int) -> dict[str, Any]:
    """把聊天里的文件存进自己的网盘（复用 blob，只增引用）。"""
    msg = db.get(ChatMessage, int(message_id))
    if not msg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "消息不存在")
    require_member(db, msg.conversation_id, me)
    if msg.kind != "file" or not msg.payload:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "这条消息里没有文件")
    try:
        payload = json.loads(msg.payload)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件信息已损坏") from None
    ids = [int(n.get("id") or 0) for n in payload.get("nodes") or []]
    nodes = [n for n in db.scalars(select(Node).where(Node.id.in_(ids))).all() if not n.deleted_at]
    if not nodes:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件已被对方删除")

    folder = _inbox_folder(db, me)
    added = 0
    saved = 0
    for node in nodes:
        if node.user_id == me.id:
            continue
        _, size = ns.copy_subtree(db, me, node, folder.id)
        added += int(size)
        saved += 1
    if me.used_bytes + added > me.quota_bytes:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "空间不足，先清理一下再保存")
    me.used_bytes = int(me.used_bytes or 0) + added
    return {"saved": saved, "size": added, "folder": folder.name}


def _inbox_folder(db: Session, me: User) -> Node:
    name = "聊天文件"
    row = db.scalar(
        select(Node)
        .where(
            Node.user_id == me.id,
            Node.parent_id.is_(None),
            Node.type == "folder",
            Node.name == name,
            Node.deleted_at.is_(None),
        )
        .limit(1)
    )
    if row:
        return row
    return ns.create_folder(db, me, None, name)


# --------------------------------------------------------------------------- #
# 举报
# --------------------------------------------------------------------------- #
def report(db: Session, me: User, message_id: int, reason: str) -> ChatReport:
    msg = db.get(ChatMessage, int(message_id))
    if not msg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "消息不存在")
    require_member(db, msg.conversation_id, me)
    if msg.sender_id == me.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能举报自己")
    text = (reason or "").strip()[:200]
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "写一下举报理由")
    existing = db.scalar(
        select(ChatReport)
        .where(
            ChatReport.message_id == msg.id,
            ChatReport.reporter_id == me.id,
            ChatReport.status == "pending",
        )
        .limit(1)
    )
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "这条消息你已经在举报了，等管理员处理吧")

    body = msg.body or ""
    names = ""
    if msg.payload:
        try:
            data = json.loads(msg.payload)
            names = "、".join(str(n.get("name") or "") for n in data.get("nodes") or [])
        except Exception:
            names = ""
    parts = [f"[{msg.kind}]", body[:500]]
    if names:
        parts.append(names)
    row = ChatReport(
        message_id=msg.id,
        conversation_id=msg.conversation_id,
        reporter_id=me.id,
        target_id=msg.sender_id,
        reason=text,
        snapshot=" ".join(p for p in parts if p)[:2000],
        created_at=utcnow(),
    )
    db.add(row)
    db.flush()
    notice.notify_admins(
        db,
        "report",
        f"{_name_of(me)} 举报了一条聊天消息",
        text,
        "#/admin/chat",
    )
    return row


# --------------------------------------------------------------------------- #
# 图片 / 表情包
# --------------------------------------------------------------------------- #
def _normalize_image(raw: bytes, *, max_side: int, keep_alpha: bool, quality: int = 88) -> bytes:
    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img)
        if keep_alpha and (img.mode in ("RGBA", "LA", "P") or "transparency" in img.info):
            img = img.convert("RGBA")
        else:
            if img.mode in ("RGBA", "LA", "P"):
                # 透明底铺白，否则转 RGB 会把透明区域变成黑块
                rgba = img.convert("RGBA")
                canvas = Image.new("RGB", rgba.size, (255, 255, 255))
                canvas.paste(rgba, mask=rgba.split()[-1])
                img = canvas
            else:
                img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_side:
            scale = max_side / float(max(w, h))
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        try:
            img.save(buf, "WEBP", quality=quality, method=4)
            return buf.getvalue()
        except Exception:  # Pillow 没带 webp 支持
            buf = io.BytesIO()
            img.save(buf, "PNG", optimize=True)
            return buf.getvalue()


def _write_media(prefix: str, data: bytes) -> str:
    ext = "webp"
    with Image.open(io.BytesIO(data)) as img:
        if img.format and img.format.upper() == "PNG":
            ext = "png"
    settings.chat_dir.mkdir(parents=True, exist_ok=True)
    name = f"{prefix}-{secrets.token_hex(8)}.{ext}"
    target = settings.chat_dir / name
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    return name


def save_chat_image(user: User, raw: bytes) -> dict[str, Any]:
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有收到图片数据")
    if len(raw) > IMAGE_MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "图片不能超过 8 MB")
    try:
        data = _normalize_image(raw, max_side=IMAGE_MAX_SIDE, keep_alpha=False)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "无法识别这张图片，请用 png / jpg / webp") from None
    name = _write_media("img", data)
    with Image.open(io.BytesIO(data)) as img:
        width, height = img.size
    return {"name": name, "url": f"/api/chat/media/{name}", "width": width, "height": height}


def media_path(name: str):
    if not MEDIA_RE.fullmatch(name or ""):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    path = settings.chat_dir / name
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    return path


def list_stickers(db: Session, me: User) -> dict[str, Any]:
    rows = list(
        db.scalars(
            select(Sticker).where(Sticker.user_id == me.id).order_by(Sticker.id.desc())
        ).all()
    )
    return {
        "items": [
            {
                "id": row.id,
                "name": row.name,
                "url": f"/api/chat/media/{row.file}",
                "size": row.size,
            }
            for row in rows
        ],
        "limit": sticker_max(db),
    }


def add_sticker(db: Session, me: User, raw: bytes, name: str = "") -> Sticker:
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有收到图片数据")
    if len(raw) > STICKER_MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "表情图不能超过 2 MB")
    limit = sticker_max(db)
    used = int(
        db.scalar(select(func.count()).select_from(Sticker).where(Sticker.user_id == me.id)) or 0
    )
    if used >= limit:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"表情包最多存 {limit} 张，先删几张再加"
        )
    try:
        data = _normalize_image(raw, max_side=STICKER_MAX_SIDE, keep_alpha=True, quality=90)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "无法识别这张图片，请用 png / gif / webp") from None
    file = _write_media("stk", data)
    row = Sticker(
        user_id=me.id,
        name=(name or "").strip()[:40] or "表情",
        file=file,
        size=len(data),
        created_at=utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def delete_sticker(db: Session, me: User, sticker_id: int) -> str | None:
    row = db.get(Sticker, int(sticker_id))
    if not row or row.user_id != me.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "表情不存在")
    file = row.file
    db.delete(row)
    db.flush()
    return file


def drop_sticker_file(file: str | None) -> None:
    if not file or not MEDIA_RE.fullmatch(file):
        return
    (settings.chat_dir / file).unlink(missing_ok=True)


def total_unread(db: Session, me: User) -> int:
    return sum(unread_map(db, me).values())


# --------------------------------------------------------------------------- #
# 后台审核
# --------------------------------------------------------------------------- #
def admin_list_reports(
    db: Session, *, report_status: str = "pending", page: int = 1, page_size: int = 20
) -> dict[str, Any]:
    stmt = select(ChatReport)
    count_stmt = select(func.count()).select_from(ChatReport)
    if report_status and report_status != "all":
        stmt = stmt.where(ChatReport.status == report_status)
        count_stmt = count_stmt.where(ChatReport.status == report_status)
    total = int(db.scalar(count_stmt) or 0)
    rows = list(
        db.scalars(
            stmt.order_by(ChatReport.id.desc())
            .offset(max(0, (page - 1) * page_size))
            .limit(page_size)
        ).all()
    )
    users = _users(
        db,
        [r.reporter_id for r in rows] + [r.target_id for r in rows],
    )
    convs = {
        c.id: c
        for c in db.scalars(
            select(ChatConversation).where(
                ChatConversation.id.in_([r.conversation_id for r in rows])
            )
        ).all()
    }
    items = []
    for row in rows:
        conv = convs.get(row.conversation_id)
        items.append(
            {
                "id": row.id,
                "messageId": row.message_id,
                "conversationId": row.conversation_id,
                "conversationKind": conv.kind if conv else "direct",
                "conversationTitle": (conv.title if conv and conv.kind == "group" else "单聊")
                or "单聊",
                "reason": row.reason,
                "snapshot": row.snapshot,
                "status": row.status,
                "reporter": user_card(users.get(row.reporter_id)),
                "target": user_card(users.get(row.target_id)),
                "createdAt": iso(row.created_at),
                "handledAt": iso(row.handled_at) if row.handled_at else None,
            }
        )
    return {"items": items, "total": total, "page": page, "pageSize": page_size}


def admin_handle_report(
    db: Session, admin: User, report_id: int, *, approve: bool, reason: str = ""
) -> dict[str, Any]:
    row = db.get(ChatReport, int(report_id))
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "举报不存在")
    if row.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, "这条举报已经处理过了")
    row.status = "approved" if approve else "rejected"
    row.handled_by = admin.id
    row.handled_at = utcnow()

    msg = db.get(ChatMessage, row.message_id)
    if approve and msg:
        # 通过 = 消息下架（清空内容 + 标撤回），发送者收到通知
        msg.recalled_at = utcnow()
        msg.body = ""
        msg.payload = None
        notice.notify(
            db,
            row.target_id,
            "report",
            "你的一条聊天消息因被举报已下架",
            reason or row.reason,
            f"#/chat/{row.conversation_id}",
        )
    notice.notify(
        db,
        row.reporter_id,
        "report",
        "你的聊天举报已处理",
        ("已核实并下架该消息" if approve else "经核实未发现问题，消息保留")
        + (f"：{reason}" if reason else ""),
        f"#/chat/{row.conversation_id}",
    )
    return {"ok": True, "status": row.status}
