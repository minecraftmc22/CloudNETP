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

import hashlib
import io
import json
import re
import secrets
from typing import Any

from fastapi import HTTPException, status
from PIL import Image, ImageOps
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from . import friend_service as fsv
from . import node_service as ns
from . import notice_service as notice
from .config import settings
from .models import (
    ChatBlock,
    ChatConversation,
    ChatMember,
    ChatMemberNote,
    ChatMessage,
    ChatReport,
    Friendship,
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
# 表情大小：默认值与硬上限都在下面，实际以后台设置（sticker_max_mb / sticker_gif_max_mb）为准
STICKER_MAX_MB_CAP = 20
STICKER_GIF_MAX_MB_CAP = 30
IMAGE_MAX_SIDE = 1600
STICKER_MAX_SIDE = 512
# 动图原样保存（不转码），只挡明显过分的尺寸/帧数，免得一张图把内存吃穿
STICKER_ANIM_MAX_SIDE = 1200
STICKER_ANIM_MAX_FRAMES = 240

MEDIA_RE = re.compile(r"^(img|stk)-[0-9a-f]{16}\.(webp|png|gif)$")

DEFAULT_STICKER_MAX = 100
DEFAULT_GROUP_MAX = 200
DEFAULT_RECALL_MINUTES = 10
DEFAULT_STICKER_MB = 2
DEFAULT_STICKER_GIF_MB = 6


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


def sticker_max_mb(db: Session) -> int:
    """单张静态表情的大小上限（MB，后台可调）。"""
    return max(1, min(STICKER_MAX_MB_CAP, get_int(db, "sticker_max_mb") or DEFAULT_STICKER_MB))


def sticker_gif_max_mb(db: Session) -> int:
    """单张动图表情的大小上限（MB，后台可调）：动图体积天然比静态图大。"""
    return max(
        1, min(STICKER_GIF_MAX_MB_CAP, get_int(db, "sticker_gif_max_mb") or DEFAULT_STICKER_GIF_MB)
    )


def sticker_max_bytes(db: Session) -> int:
    return sticker_max_mb(db) * 1024 * 1024


def sticker_gif_max_bytes(db: Session) -> int:
    return sticker_gif_max_mb(db) * 1024 * 1024


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
# 拉黑
# --------------------------------------------------------------------------- #
def block_pair(db: Session, me_id: int, other_id: int) -> tuple[bool, bool]:
    """返回 (我拉黑了他, 他拉黑了我)。"""
    rows = db.execute(
        select(ChatBlock.blocker_id, ChatBlock.blocked_id).where(
            or_(
                and_(ChatBlock.blocker_id == me_id, ChatBlock.blocked_id == other_id),
                and_(ChatBlock.blocker_id == other_id, ChatBlock.blocked_id == me_id),
            )
        )
    ).all()
    mine = any(int(b) == other_id for b, _ in rows)
    theirs = any(int(b) == me_id for _, b in rows)
    return mine, theirs


def blocked_ids(db: Session, me_id: int) -> set[int]:
    return {
        int(x)
        for x in db.scalars(
            select(ChatBlock.blocked_id).where(ChatBlock.blocker_id == int(me_id))
        ).all()
    }


def blocked_me_ids(db: Session, me_id: int) -> set[int]:
    return {
        int(x)
        for x in db.scalars(
            select(ChatBlock.blocker_id).where(ChatBlock.blocked_id == int(me_id))
        ).all()
    }


def block_peer(db: Session, me: User, other_id: int) -> dict[str, Any]:
    """拉黑：断好友关系 + 我的会话列表里删掉这段聊天。

    「拉黑并删除」合成一步：留着一条发不出消息的会话只会让人以为功能坏了。
    对方仍看得到历史（他的 ChatMember 不动），只是再也发不进来。
    """
    other_id = int(other_id)
    if other_id == me.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能拉黑自己")
    other = db.get(User, other_id)
    if not other:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if not db.get(ChatBlock, {"blocker_id": me.id, "blocked_id": other_id}):
        db.add(ChatBlock(blocker_id=me.id, blocked_id=other_id, created_at=utcnow()))
    # 好友关系一并断掉；会话只在我这边隐藏
    fsv.remove_between(db, me.id, other_id)
    key = f"{min(me.id, other_id)}-{max(me.id, other_id)}"
    conv = db.scalar(select(ChatConversation).where(ChatConversation.direct_key == key))
    if conv:
        member = member_of(db, conv.id, me.id)
        if member:
            latest = db.scalar(
                select(func.max(ChatMessage.id)).where(
                    ChatMessage.conversation_id == conv.id
                )
            )
            member.cleared_id = int(latest or 0)
            member.last_read_id = int(latest or 0)
            member.hidden_at = utcnow()
    db.flush()
    return {"blocked": True, "peer": user_card(other)}


def unblock(db: Session, me: User, other_id: int) -> dict[str, Any]:
    row = db.get(ChatBlock, {"blocker_id": me.id, "blocked_id": int(other_id)})
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有拉黑这个人")
    db.delete(row)
    db.flush()
    return {"blocked": False, "peerId": int(other_id)}


def list_blocks(db: Session, me: User) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(ChatBlock)
            .where(ChatBlock.blocker_id == me.id)
            .order_by(ChatBlock.created_at.desc())
        ).all()
    )
    users = _users(db, [row.blocked_id for row in rows])
    out: list[dict[str, Any]] = []
    for row in rows:
        user = users.get(row.blocked_id)
        if not user:
            continue
        out.append(user_card(user) | {"blockedAt": iso(row.created_at)})
    return out


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
    member: ChatMember | None = None,
    i_blocked: bool = False,
    blocked_by_peer: bool = False,
    peer_alias: str = "",
    notes: dict[int, str] | None = None,
) -> dict[str, Any]:
    rows = members if members is not None else members_of(db, conv.id)
    users = _users(db, [row.user_id for row in rows])
    note_map = notes or {}
    now = utcnow()
    people = []
    for row in rows:
        item = user_card(users.get(row.user_id))
        alias = note_map.get(int(row.user_id)) or ""
        if alias:
            item["alias"] = alias
        item["isAdmin"] = row.admin_at is not None
        # 禁言过期了就当没有：前端不用再拿客户端时钟比一遍
        if row.mute_until and row.mute_until > now:
            item["muteUntil"] = iso(row.mute_until)
        people.append(item)
    mine = next((p for p in people if p["id"] == me.id), None)
    if conv.kind == "direct":
        peer = next((p for p in people if p["id"] != me.id), None)
        # 好友备注优先于昵称：备注就是「我认得的那个名字」
        title = peer_alias or (peer["displayName"] if peer else "已注销用户")
        avatar_url = peer["avatarUrl"] if peer else None
    else:
        title = conv.title or "群聊"
        avatar_url = f"/api/chat/group-avatar/{conv.avatar}" if conv.avatar else None
        peer = None

    my_role = "owner" if conv.owner_id == me.id else ("admin" if member and member.admin_at else "member")
    is_group = conv.kind == "group"

    # 「清空聊天记录」只影响我这边：清空点之前的最后一条不该再当预览显示
    cleared = int(getattr(member, "cleared_id", 0) or 0)
    preview = ""
    if last is not None and last.id > cleared:
        preview = _preview(last)
    return {
        "id": conv.id,
        "kind": conv.kind,
        "title": title,
        "avatarUrl": avatar_url,
        "peerId": (peer or {}).get("id") if conv.kind == "direct" else None,
        "ownerId": conv.owner_id,
        "isOwner": conv.owner_id == me.id,
        # 群资料：单聊一律为空 / false，前端不用再判断 kind
        "description": conv.description or "" if is_group else "",
        "isPublic": bool(conv.is_public) if is_group else False,
        "groupNo": conv.group_no if is_group else None,
        "shareEnabled": bool(conv.share_token) if is_group else False,
        "myRole": my_role if is_group else "member",
        "canManage": bool(is_group and (conv.owner_id == me.id or (member and member.admin_at))),
        # 我被禁言到什么时候（过期/没禁言都是 None）；前端据此锁输入框
        "myMuteUntil": (
            iso(member.mute_until)
            if (is_group and member and member.mute_until and member.mute_until > now)
            else None
        ),
        "members": people,
        "memberCount": len(people),
        "me": mine,
        "lastMessage": preview,
        "lastMessageAt": iso(conv.last_message_at or conv.created_at),
        "unread": int(unread or 0),
        # 单聊才有：我拉黑了对方 / 对方拉黑了我（后者决定输入框能不能用）
        "blocked": bool(i_blocked),
        "blockedByPeer": bool(blocked_by_peer),
        "canSend": not (i_blocked or blocked_by_peer),
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
    # 已「删除聊天」的会话不在这里出现，但成员行还在：对方发新消息时会恢复
    members = list(
        db.scalars(
            select(ChatMember).where(
                ChatMember.user_id == me.id, ChatMember.hidden_at.is_(None)
            )
        ).all()
    )
    if not members:
        return {"items": [], "unread": 0}
    mine_by_conv = {row.conversation_id: row for row in members}
    convs = list(
        db.scalars(
            select(ChatConversation)
            .where(ChatConversation.id.in_(list(mine_by_conv)))
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

    # 拉黑状态一次查全，别在循环里每人两条 SQL
    i_blocked = blocked_ids(db, me.id)
    blocked_me = blocked_me_ids(db, me.id)
    friend_aliases = alias_map(db, me.id)
    group_notes = member_notes_map(db, me.id, conv_ids)

    items = []
    for conv in convs:
        member = mine_by_conv[conv.id]
        people = by_conv.get(conv.id, [])
        peer_ids = [row.user_id for row in people if row.user_id != me.id]
        # 群里我起的备注优先，其次用好友备注兜底：备注就是「我认得的那个名字」
        merged = dict(friend_aliases)
        merged.update(group_notes.get(conv.id, {}))
        items.append(
            _conv_payload(
                db,
                conv,
                me,
                unread=unread.get(conv.id, 0),
                members=people,
                last=lasts.get(conv.last_message_id) if conv.last_message_id else None,
                member=member,
                # 拉黑只影响单聊：共同群里大家照常说话，不能因为一个被拉黑的人锁全群
                i_blocked=conv.kind == "direct" and any(pid in i_blocked for pid in peer_ids),
                blocked_by_peer=conv.kind == "direct" and any(pid in blocked_me for pid in peer_ids),
                peer_alias=merged.get(peer_ids[0], "") if conv.kind == "direct" and peer_ids else "",
                notes=merged,
            )
        )
    # 隐藏的会话不参与未读统计：否则角标会指向一个列表里看不到的会话
    total = sum(unread.get(c.id, 0) for c in convs)
    return {"items": items, "unread": total}


def friends_for_picker(db: Session, me: User) -> list[dict[str, Any]]:
    """新建单聊 / 群聊 / 快传选人用的好友简表。"""
    data = fsv.list_all(db, me)
    return data.get("friends", [])


def alias_map(db: Session, me_id: int) -> dict[int, str]:
    """我对每位好友的备注：{对方 id: 备注}。会话标题优先用它。"""
    rows = db.scalars(
        select(Friendship).where(
            Friendship.status == "accepted",
            or_(Friendship.requester_id == me_id, Friendship.addressee_id == me_id),
        )
    ).all()
    out: dict[int, str] = {}
    for row in rows:
        other = row.addressee_id if row.requester_id == me_id else row.requester_id
        alias = (row.requester_alias if row.requester_id == me_id else row.addressee_alias) or ""
        if alias:
            out[int(other)] = alias
    return out


def member_notes_map(db: Session, me_id: int, conv_ids: list[int]) -> dict[int, dict[int, str]]:
    """我在这些群里给成员起的备注：{群 id: {成员 id: 备注}}。一次查全，别按会话循环。"""
    if not conv_ids:
        return {}
    rows = db.scalars(
        select(ChatMemberNote).where(
            ChatMemberNote.owner_id == me_id,
            ChatMemberNote.conversation_id.in_(conv_ids),
        )
    ).all()
    out: dict[int, dict[int, str]] = {}
    for row in rows:
        if row.alias:
            out.setdefault(int(row.conversation_id), {})[int(row.target_id)] = row.alias
    return out


def note_of(db: Session, me_id: int, conv_id: int, target_id: int) -> str:
    row = db.get(
        ChatMemberNote,
        {"conversation_id": int(conv_id), "owner_id": int(me_id), "target_id": int(target_id)},
    )
    return (row.alias if row else "") or ""


def set_member_note(db: Session, me: User, conv_id: int, target_id: int, alias: str) -> dict[str, Any]:
    """给群里的某位成员起备注。备注只对我可见；空串 = 删除备注。"""
    conv, _ = require_member(db, conv_id, me)
    if conv.kind != "group":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "单聊请用好友备注")
    target_id = int(target_id)
    if target_id == me.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不用给自己起备注")
    if not member_of(db, conv.id, target_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "他不在这个群里")
    text = " ".join((alias or "").split())[:32]
    row = db.get(
        ChatMemberNote,
        {"conversation_id": conv.id, "owner_id": me.id, "target_id": target_id},
    )
    if text:
        if row:
            row.alias = text
            row.updated_at = utcnow()
        else:
            db.add(
                ChatMemberNote(
                    conversation_id=conv.id,
                    owner_id=me.id,
                    target_id=target_id,
                    alias=text,
                    updated_at=utcnow(),
                )
            )
    elif row:
        db.delete(row)
    db.flush()
    return {"ok": True, "alias": text}


def get_or_create_direct(db: Session, me: User, other_id: int) -> ChatConversation:
    other_id = int(other_id)
    if other_id == me.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能和自己聊天")
    other = db.get(User, other_id)
    if not other or other.banned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if not _are_friends(db, me.id, other_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "先加好友才能聊天")
    mine, theirs = block_pair(db, me.id, other_id)
    if mine:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "你已把对方拉黑，先在好友页解除")
    if theirs:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "对方暂时无法接收消息")
    key = f"{min(me.id, other_id)}-{max(me.id, other_id)}"
    conv = db.scalar(select(ChatConversation).where(ChatConversation.direct_key == key))
    if conv:
        # 之前「删除聊天」过：重新打开就恢复显示
        member = member_of(db, conv.id, me.id)
        if member and member.hidden_at:
            member.hidden_at = None
            db.flush()
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
    # 建群就给一个群号：搜索、分享、二维码都要用，别等到第一次打开分享面板才生成
    from . import group_service

    group_service.ensure_group_no(db, conv)
    group_service.ensure_share_token(db, conv)
    _system_message(db, conv, f"{_name_of(me)} 创建了群聊")
    return conv


def rename_group(db: Session, me: User, conversation_id: int, title: str) -> ChatConversation:
    conv, member = require_member(db, conversation_id, me)
    if conv.kind != "group":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "单聊没有群名")
    if conv.owner_id != me.id and not (member and member.admin_at):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有群主和管理员能改群名")
    name = (title or "").strip()[:60]
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "群名不能为空")
    conv.title = name
    conv.updated_at = utcnow()
    _system_message(db, conv, f"群名改为「{name}」")
    return conv


def add_members(db: Session, me: User, conversation_id: int, ids: list[int]) -> ChatConversation:
    conv, member = require_member(db, conversation_id, me)
    if conv.kind != "group":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "单聊不能加人")
    if conv.owner_id != me.id and not (member and member.admin_at):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有群主和管理员能邀请成员")
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
    conv, member = require_member(db, conversation_id, me)
    if conv.kind != "group":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "单聊不能踢人")
    user_id = int(user_id)
    if user_id == me.id:
        return leave_group(db, me, conversation_id)
    is_owner = conv.owner_id == me.id
    is_admin = bool(member and member.admin_at)
    if not (is_owner or is_admin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有群主和管理员能移除成员")
    row = member_of(db, conv.id, user_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "他不在这个群里")
    # 管理员只能清普通成员：不能踢群主、也不能踢同级管理员
    if not is_owner and (user_id == conv.owner_id or row.admin_at):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "没有权限移除群主或管理员")
    db.delete(row)
    db.flush()
    for note in db.scalars(
        select(ChatMemberNote).where(
            ChatMemberNote.conversation_id == conv.id,
            (ChatMemberNote.owner_id == user_id) | (ChatMemberNote.target_id == user_id),
        )
    ).all():
        db.delete(note)
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
    # 我在这个群里给别人写的备注跟着一起走（别人的备注不受影响）
    for note in db.scalars(
        select(ChatMemberNote).where(
            ChatMemberNote.conversation_id == conv.id, ChatMemberNote.owner_id == me.id
        )
    ).all():
        db.delete(note)
    db.flush()
    rows = members_of(db, conv.id)
    _system_message(db, conv, f"{_name_of(me)} 退出了群聊")
    if not rows:
        conv_id = conv.id
        db.delete(conv)
        db.flush()
        from . import group_service

        group_service.purge_storage(conv_id)
        return {"ok": True, "closed": True}
    # 群主退群 → 顺位交给最早加入的人，否则没人能邀请/移除
    if conv.owner_id == me.id:
        heir = rows[0]
        heir.role = "owner"
        heir.admin_at = None
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


def _quote_text(msg: ChatMessage) -> str:
    """引用块里显示的一行摘要。只存 id，所以撤回/改名都能实时反映。"""
    if msg.recalled_at:
        return "消息已撤回"
    if msg.kind == "text":
        return (msg.body or "")[:80]
    if msg.kind == "image":
        return "[图片]"
    if msg.kind == "sticker":
        return "[表情]"
    if msg.kind == "file":
        try:
            nodes = (json.loads(msg.payload or "{}") or {}).get("nodes") or []
        except Exception:
            nodes = []
        if len(nodes) == 1:
            return f"[文件] {nodes[0].get('name') or ''}"[:80]
        return f"[文件] {len(nodes)} 项" if nodes else "[文件]"
    return ""


def message_payload(
    db: Session,
    msg: ChatMessage,
    senders: dict[int, User] | None = None,
    recall_window: int | None = None,
    quotes: dict[int, ChatMessage] | None = None,
) -> dict[str, Any]:
    """recall_window：可撤回的时间窗（秒），None/0 表示不限。

    撤回时限算在服务端，前端只看 recallable —— 客户端时钟不准时也不会误判。
    quotes：被引用的消息（批量查好传进来，避免每条消息一次查询）。
    """
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
    recallable = not recalled and msg.kind != "system"
    if recallable and recall_window:
        recallable = (utcnow() - msg.created_at).total_seconds() <= recall_window
    reply = None
    if msg.reply_to:
        quoted = quotes.get(msg.reply_to) if quotes is not None else db.get(ChatMessage, msg.reply_to)
        if quoted is not None:
            qs = (senders or {}).get(quoted.sender_id) or db.get(User, quoted.sender_id)
            reply = {
                "id": quoted.id,
                "senderId": quoted.sender_id,
                "senderName": user_card(qs).get("displayName") if qs else "",
                "kind": quoted.kind,
                "text": _quote_text(quoted),
                "recalled": quoted.recalled_at is not None,
            }
    return {
        "id": msg.id,
        "conversationId": msg.conversation_id,
        "kind": msg.kind,
        "body": "" if recalled else (msg.body or ""),
        "payload": payload,
        # @ 提及（文本消息才有）：被 @ 的成员 id / 是否 @所有人；前端高亮用
        "mentions": (payload or {}).get("mentions") or [],
        "mentionAll": bool((payload or {}).get("mentionAll")),
        "sender": user_card(sender) if msg.sender_id else None,
        "recalled": recalled,
        "recallable": recallable,
        "forwardedFrom": msg.forwarded_from,
        "reply": reply,
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
    conv, member = require_member(db, conversation_id, me)
    limit = max(1, min(HISTORY_MAX, int(limit)))
    stmt = select(ChatMessage).where(ChatMessage.conversation_id == conv.id)
    # 「清空聊天记录」只对我生效：清空点之前的历史当作不存在
    if member.cleared_id:
        stmt = stmt.where(ChatMessage.id > member.cleared_id)
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
    window = recall_minutes(db) * 60 or None
    # 引用到的原消息可能在更早的一页里，单独批量取一次
    reply_ids = {m.reply_to for m in rows if m.reply_to}
    quotes: dict[int, ChatMessage] = {}
    if reply_ids:
        quotes = {
            q.id: q
            for q in db.scalars(select(ChatMessage).where(ChatMessage.id.in_(reply_ids))).all()
        }
        for q in quotes.values():
            if q.sender_id and q.sender_id not in senders:
                sender = db.get(User, q.sender_id)
                if sender:
                    senders[q.sender_id] = sender
    return {
        "items": [message_payload(db, m, senders, window, quotes) for m in rows],
        "hasMore": len(rows) == limit and not after_id,
    }


def sent_payload(db: Session, msg: ChatMessage) -> dict[str, Any]:
    """刚发出去那条的 payload：引用最多指向一条消息，顺手带上省一次往返。"""
    quotes: dict[int, ChatMessage] = {}
    if msg.reply_to:
        quoted = db.get(ChatMessage, msg.reply_to)
        if quoted is not None:
            quotes[quoted.id] = quoted
    return message_payload(db, msg, quotes=quotes)


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
    reply_to: int | None = None,
    mentions: list[int] | None = None,
    mention_all: bool = False,
) -> ChatMessage:
    conv, member = require_member(db, conversation_id, me)
    if conv.kind == "direct":
        rows = members_of(db, conv.id)
        peer_id = next((row.user_id for row in rows if row.user_id != me.id), None)
        if peer_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "对方已注销，会话不可用")
        if not _are_friends(db, me.id, peer_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "你们已经不是好友，无法继续聊天")
        mine, theirs = block_pair(db, me.id, peer_id)
        if mine:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "你已把对方拉黑，先在好友页解除")
        if theirs:
            # 对方的输入框还在（他看不见我拉黑了他），但消息发不进来
            raise HTTPException(status.HTTP_403_FORBIDDEN, "对方拒收了你的消息")

    # 禁言：到期时间已过就当作没禁言，不需要谁去解
    if conv.kind == "group" and member is not None and member.mute_until:
        left = (member.mute_until - utcnow()).total_seconds()
        if left > 0:
            mins = int(left // 60) + 1
            if mins >= 1440:
                span = f"{mins // 1440} 天"
            elif mins >= 60:
                span = f"{mins // 60} 小时"
            else:
                span = f"{mins} 分钟"
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"你已被禁言，还有 {span}")

    kind = (kind or "text").strip()
    text = ""
    data: dict[str, Any] | None = None
    if kind == "text":
        text = _check_text(body)
        # 群聊 @ 提及：只收「确实在群里」的成员，私聊没有 @ 的意义直接忽略
        if conv.kind == "group":
            ids = {int(x) for x in (mentions or []) if int(x or 0) != me.id}
            member_ids = {int(row.user_id) for row in members_of(db, conv.id)}
            valid = sorted(ids & member_ids)
            if valid or mention_all:
                data = {"mentions": valid, "mentionAll": bool(mention_all)}
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

    quoted = None
    if reply_to:
        quoted = db.get(ChatMessage, int(reply_to))
        if quoted is None or quoted.conversation_id != conv.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "引用的消息不在了")
        if quoted.recalled_at is not None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "这条消息已撤回，换一条引用吧")

    msg = ChatMessage(
        conversation_id=conv.id,
        sender_id=me.id,
        kind=kind,
        body=text,
        payload=json.dumps(data, ensure_ascii=False) if data else None,
        reply_to=quoted.id if quoted else None,
        created_at=utcnow(),
    )
    db.add(msg)
    db.flush()
    # 新消息让「删除过的聊天」重新回到列表（否则删掉一次就再也收不到这个人的消息）
    db.execute(
        update(ChatMember)
        .where(ChatMember.conversation_id == conv.id, ChatMember.hidden_at.is_not(None))
        .values(hidden_at=None)
    )
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


def clear_history(db: Session, me: User, conversation_id: int) -> dict[str, Any]:
    """清空聊天记录：只动我自己的 ChatMember 边界，对方那边一条不少。"""
    conv, row = require_member(db, conversation_id, me)
    latest = int(
        db.scalar(
            select(func.max(ChatMessage.id)).where(ChatMessage.conversation_id == conv.id)
        )
        or 0
    )
    row.cleared_id = latest
    # 顺手把已读推到最新：否则清空后角标还挂着一个看不见的未读数
    if latest > int(row.last_read_id or 0):
        row.last_read_id = latest
    db.flush()
    return {"ok": True, "clearedId": latest}


def hide_conversation(db: Session, me: User, conversation_id: int) -> dict[str, Any]:
    """删除聊天（单聊）：隐藏 + 清空，双方成员行都保留，对方再发消息会恢复。"""
    conv, row = require_member(db, conversation_id, me)
    if conv.kind == "group":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "群聊请用「退出群聊」")
    latest = int(
        db.scalar(
            select(func.max(ChatMessage.id)).where(ChatMessage.conversation_id == conv.id)
        )
        or 0
    )
    row.cleared_id = latest
    row.last_read_id = max(int(row.last_read_id or 0), latest)
    row.hidden_at = utcnow()
    db.flush()
    return {"ok": True}


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
def _anim_format(raw: bytes) -> str | None:
    """动图（GIF / 动图 WebP）返回扩展名，静态图返回 None。

    动图**不做转码**：Pillow 把 GIF 压成 webp 只会留下第一帧，表情就不动了。
    所以这里只做校验（帧数、边长、逐帧解码），字节原样落盘，由浏览器播放。
    """
    try:
        with Image.open(io.BytesIO(raw)) as img:
            fmt = (img.format or "").upper()
            frames = int(getattr(img, "n_frames", 1) or 1)
            if fmt not in ("GIF", "WEBP") or frames <= 1 or not getattr(img, "is_animated", False):
                return None
            if frames > STICKER_ANIM_MAX_FRAMES:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"动图表情最多 {STICKER_ANIM_MAX_FRAMES} 帧，先压缩一下再传",
                )
            width, height = img.size
            if max(width, height) > STICKER_ANIM_MAX_SIDE:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"动图表情边长不能超过 {STICKER_ANIM_MAX_SIDE}px，先缩小再传",
                )
            # 逐帧解码一遍：截断 / 损坏的 GIF 在这一步就会抛异常，别把坏图存下来
            for index in range(frames):
                img.seek(index)
                img.load()
            return "gif" if fmt == "GIF" else "webp"
    except HTTPException:
        raise
    except Exception:
        return None


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
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            if fmt == "PNG":
                ext = "png"
            elif fmt == "GIF":
                ext = "gif"
    except Exception:
        ext = "webp"
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
        # 单张大小上限（MB），前端据此提前挡下超大文件并提示
        "maxMb": sticker_max_mb(db),
        "gifMaxMb": sticker_gif_max_mb(db),
    }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sticker_hash(row: Sticker) -> str:
    """老库补列后 hash 是 NULL：按文件内容回填一次并记住。"""
    if row.hash:
        return row.hash
    if not MEDIA_RE.fullmatch(row.file or ""):
        return ""
    path = settings.chat_dir / row.file
    if not path.is_file():
        return ""
    row.hash = _sha256(path.read_bytes())
    return row.hash


def _store_sticker(db: Session, me: User, data: bytes, name: str) -> tuple[Sticker, bool]:
    """归一化后的字节入库，按 sha256 去重。

    哈希算在**归一化之后**的字节上：同一张图哪怕上传两次元信息不同，
    落到 webp 也是同一串字节，去重才靠得住。
    """
    digest = _sha256(data)
    for row in db.scalars(select(Sticker).where(Sticker.user_id == me.id)).all():
        if _sticker_hash(row) == digest:
            return row, False
    limit = sticker_max(db)
    used = int(
        db.scalar(select(func.count()).select_from(Sticker).where(Sticker.user_id == me.id)) or 0
    )
    if used >= limit:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"表情包最多存 {limit} 张，先删几张再加"
        )
    file = _write_media("stk", data)
    row = Sticker(
        user_id=me.id,
        name=(name or "").strip()[:40] or "表情",
        file=file,
        size=len(data),
        hash=digest,
        created_at=utcnow(),
    )
    db.add(row)
    db.flush()
    return row, True


def add_sticker(db: Session, me: User, raw: bytes, name: str = "") -> tuple[Sticker, bool]:
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有收到图片数据")
    anim = _anim_format(raw)
    if anim:
        # 动图上限单独一套（默认 6 MB）：GIF 天生就比静态图大得多
        cap = sticker_gif_max_bytes(db)
        if len(raw) > cap:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"动图表情不能超过 {cap // 1024 // 1024} MB",
            )
        data = raw
    else:
        cap = sticker_max_bytes(db)
        if len(raw) > cap:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"表情图不能超过 {cap // 1024 // 1024} MB",
            )
        try:
            data = _normalize_image(raw, max_side=STICKER_MAX_SIDE, keep_alpha=True, quality=90)
        except Exception:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "无法识别这张图片，请用 png / jpg / gif / webp"
            ) from None
    return _store_sticker(db, me, data, name)


def add_sticker_from_message(db: Session, me: User, message_id: int) -> tuple[Sticker, bool]:
    """把别人发的表情收进我的表情：复制一份独立文件，别人删了不影响我。"""
    msg = db.get(ChatMessage, int(message_id))
    if not msg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "消息不存在")
    require_member(db, msg.conversation_id, me)
    if msg.recalled_at or msg.kind != "sticker":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "这条消息不是表情")
    try:
        payload = json.loads(msg.payload or "{}")
    except Exception:
        payload = {}
    name = str(payload.get("name") or "")
    if not MEDIA_RE.fullmatch(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "表情文件已失效")
    path = settings.chat_dir / name
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "表情文件已失效")
    data = path.read_bytes()
    label = str(payload.get("label") or "").strip()
    if not label:
        origin = db.get(Sticker, int(payload.get("stickerId") or 0))
        label = (origin.name if origin else "") or "表情"
    return _store_sticker(db, me, data, label)


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

    visible = set(        db.scalars(
            select(ChatMember.conversation_id).where(
                ChatMember.user_id == me.id, ChatMember.hidden_at.is_(None)
            )
        ).all()
    )
    return sum(n for cid, n in unread_map(db, me).items() if cid in visible)


def alert_summary(db: Session, me: User, after_id: int = 0, limit: int = 6) -> dict[str, Any]:
    """消息提醒用的轻量摘要：``after_id`` 之后的未读消息，最多几条。

    为什么不让前端直接看会话列表：那个接口按会话聚合（成员、最后一条、未读），
    每 20 秒轮询一次太重；这里只读「比上次提醒点更新的未读消息」，
    一条 join 查询就够，页面在后台时也靠它弹系统通知。
    """
    rows = db.execute(
        select(ChatMessage, ChatConversation)
        .join(
            ChatMember,
            and_(
                ChatMember.conversation_id == ChatMessage.conversation_id,
                ChatMember.user_id == me.id,
            ),
        )
        .join(ChatConversation, ChatConversation.id == ChatMessage.conversation_id)
        .where(
            ChatMessage.id > int(after_id or 0),
            ChatMessage.sender_id != me.id,
            ChatMessage.kind != "system",
            ChatMember.hidden_at.is_(None),
            ChatMessage.id > ChatMember.cleared_id,
        )
        .order_by(ChatMessage.id.desc())
        .limit(max(1, min(20, limit)))
    ).all()

    latest = int(after_id or 0)
    unread = total_unread(db, me)
    if not rows:
        return {"messages": [], "latest": latest, "unread": unread}

    # 拉黑的、撤回的都不提醒：前者是用户明确表示不想看见，后者没有内容可看。
    # 拉黑只影响单聊——共同群里被拉黑的人说话照常提醒，不然群里像被人掐了麦
    blocked = blocked_ids(db, me.id)
    rows = [
        r
        for r in rows
        if (r[1].kind != "direct" or r[0].sender_id not in blocked) and not r[0].recalled_at
    ]
    if not rows:
        return {"messages": [], "latest": latest, "unread": unread}

    convs = {conv.id: conv for _, conv in rows}
    direct_ids = [cid for cid, conv in convs.items() if conv.kind == "direct"]
    peer_of: dict[int, int] = {}
    if direct_ids:
        for cid, uid in db.execute(
            select(ChatMember.conversation_id, ChatMember.user_id).where(
                ChatMember.conversation_id.in_(direct_ids), ChatMember.user_id != me.id
            )
        ).all():
            peer_of[int(cid)] = int(uid)

    senders = _users(db, [msg.sender_id for msg, _ in rows])
    aliases = alias_map(db, me.id)
    notes = member_notes_map(db, me.id, list(convs))
    peers = _users(db, [peer for peer in peer_of.values()])
    out = []
    for msg, conv in rows:
        group_notes = notes.get(conv.id, {})
        # 铃铛里显示「谁在哪儿说了什么」：群聊给群名、单聊给对方（备注优先）
        if conv.kind == "direct":
            title = aliases.get(peer_of.get(conv.id, 0), "") or _name_of(
                peers.get(peer_of.get(conv.id, 0))
            )
        else:
            title = conv.title or "群聊"
        preview = _preview(msg)
        # 被 @ 的消息在提醒里点明，不然混在预览文本里根本看不出来
        if conv.kind == "group" and msg.recalled_at is None:
            try:
                pdata = json.loads(msg.payload) if msg.payload else {}
            except Exception:
                pdata = {}
            if pdata.get("mentionAll"):
                preview = f"[@所有人] {preview}"
            elif me.id in {int(x) for x in pdata.get("mentions") or []}:
                preview = f"[@你] {preview}"
        out.append(
            {
                "id": msg.id,
                "conversationId": conv.id,
                "kind": conv.kind,
                "title": title,
                "senderName": group_notes.get(msg.sender_id)
                or aliases.get(msg.sender_id, "")
                or _name_of(senders.get(msg.sender_id)),
                "senderId": msg.sender_id,
                # 弹窗卡片直接显示说话人的头像
                "avatarUrl": user_card(senders.get(msg.sender_id))["avatarUrl"],
                "preview": preview,
                "createdAt": iso(msg.created_at),
            }
        )
    out.reverse()  # 按时间正序：弹通知时先弹早的，最新的最后落下
    return {"messages": out, "latest": max(item["id"] for item in out), "unread": unread}


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
