"""快传服务：扫码直传 / 站内快传 / 免登录取件码。

三种形态共用 `quick_transfers` 一张表，靠 kind 区分：

- **drop**：电脑上开一次会话拿到 token（二维码），手机扫码后凭 token 往指定目录上传，
  上传记在会话主人的配额里；会话到期或手动关闭即失效。
- **send**：把选中的文件 / 文件夹复制到另一个用户的网盘根目录，复用同一个 blob
  （只加引用、不复制物理数据），对方在「快传 → 收到」里看到记录。
- **pickup**：为若干文件生成 6 位取件码，任何人输码即可下载，到期 / 次数用完 / 手动撤销失效。
  只收文件，文件夹请走分享链接（分享页能浏览目录，取件页不重复造这个轮子）。
"""

from __future__ import annotations

import json
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import node_service as ns
from .models import Node, QuickTransfer, User, utcnow
from .share_service import CODE_ALPHABET, is_blocked

DROP_MINUTES = (10, 30, 60, 120)
PICKUP_DAYS = (1, 3, 7, 30)
PICKUP_CODE_LEN = 6
FLASH_MESSAGE_MAX = 200
MAX_ITEMS = 100

# 取件码是可枚举的短口令，按 IP 限流：10 分钟内最多试 12 次，成功一次就清零
_PICKUP_WINDOW = 10 * 60
_PICKUP_LIMIT = 12
_pickup_tries: dict[str, deque[float]] = defaultdict(deque)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _iso(value) -> str | None:
    return value.isoformat() + "Z" if value else None


def _hint(minutes: int | None = None, days: int | None = None) -> datetime | None:
    if minutes:
        return utcnow() + timedelta(minutes=minutes)
    if days:
        return utcnow() + timedelta(days=days)
    return None


def _dead(qt: QuickTransfer) -> bool:
    return bool(qt.revoked or (qt.expires_at and qt.expires_at <= utcnow()))


def _expire_reason(qt: QuickTransfer) -> str:
    if qt.revoked:
        return "这个快传已被取消"
    return "这个快传已过期"


def _nodes(db: Session, ids: list[int], user_id: int) -> list[Node]:
    if not ids:
        return []
    rows = db.scalars(select(Node).where(Node.id.in_(ids), Node.user_id == user_id)).all()
    order = {nid: idx for idx, nid in enumerate(ids)}
    rows = [n for n in rows if not n.deleted_at]
    rows.sort(key=lambda n: order.get(n.id, len(order)))
    return rows


def _summary(nodes: list[Node]) -> tuple[str, int, int]:
    total = sum(int(n.size or 0) for n in nodes if n.type == "file")
    if len(nodes) == 1:
        return nodes[0].name, total, 1
    return f"{len(nodes)} 个项目", total, len(nodes)


def id_list(qt: QuickTransfer) -> list[int]:
    if not qt.node_ids:
        return []
    try:
        data = json.loads(qt.node_ids)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [int(x) for x in data if isinstance(x, (int, str)) and str(x).isdigit()]


def _code_free(db: Session, code: str) -> bool:
    row = db.scalars(
        select(QuickTransfer).where(QuickTransfer.kind == "pickup", QuickTransfer.code == code)
    ).first()
    if row is None:
        return True
    # 失效的码可以回收：只有「还活着」的码才算占用
    return _dead(row) or (row.max_uses is not None and row.uses >= row.max_uses)


def new_code(db: Session) -> str:
    for _ in range(16):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(PICKUP_CODE_LEN))
        if _code_free(db, code):
            return code
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "取件码生成失败，请稍后再试")


def payload(db: Session, qt: QuickTransfer, *, with_code: bool = True) -> dict:
    """创建者视角：列表里展示的一条。"""
    data = {
        "id": qt.id,
        "kind": qt.kind,
        "token": qt.token,
        "name": qt.name,
        "size": qt.size,
        "count": qt.count,
        "uses": qt.uses,
        "maxUses": qt.max_uses,
        "expiresAt": _iso(qt.expires_at),
        "createdAt": _iso(qt.created_at),
        "revoked": qt.revoked,
        "status": "revoked" if qt.revoked else ("expired" if _dead(qt) else "active"),
    }
    if qt.kind == "pickup" and with_code:
        data["code"] = qt.code
        data["url"] = f"/pickup?code={qt.code}"
    elif qt.kind == "drop":
        data["url"] = f"/drop/{qt.token}"
    if qt.kind == "send":
        data["message"] = qt.message
        data["seen"] = qt.seen
        data["peerId"] = qt.peer_id
        data["nodeIds"] = id_list(qt)
    return data


# --------------------------------------------------------------------------- #
# 扫码直传（drop）
# --------------------------------------------------------------------------- #
def create_drop(db: Session, user: User, *, parent_id: int | None, minutes: int) -> QuickTransfer:
    minutes = max(5, min(720, int(minutes or 30)))
    ns.require_parent(db, user.id, parent_id)
    target = db.get(Node, parent_id) if parent_id else None
    qt = QuickTransfer(
        kind="drop",
        owner_id=user.id,
        target_id=parent_id,
        token=secrets.token_urlsafe(32),
        name=target.name if target else "我的文件",
        count=0,
        uses=0,
        expires_at=_hint(minutes=minutes),
    )
    db.add(qt)
    db.flush()
    return qt


def load_drop(db: Session, token: str) -> QuickTransfer:
    qt = db.scalars(
        select(QuickTransfer).where(
            QuickTransfer.token == token, QuickTransfer.kind == "drop"
        )
    ).first()
    if not qt:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "这个快传链接不存在")
    if _dead(qt):
        raise HTTPException(status.HTTP_410_GONE, _expire_reason(qt))
    return qt


def load_drop_for(db: Session, token: str, user: User) -> QuickTransfer:
    """上传侧入口：链接可以公开看，但只有发起人本人能往里传。"""
    qt = load_drop(db, token)
    if qt.owner_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "这不是你的快传，只有发起人本人能往这个目录上传"
        )
    return qt


def drop_public(db: Session, qt: QuickTransfer) -> dict:
    owner = db.get(User, qt.owner_id)
    target = db.get(Node, qt.target_id) if qt.target_id else None
    files = _nodes(db, id_list(qt), qt.owner_id)
    return {
        "owner": (owner.display_name or owner.username) if owner else "（已注销）",
        "folder": target.name if target else "我的文件",
        "expiresAt": _iso(qt.expires_at),
        "count": qt.uses,
        "files": [
            {
                "id": n.id,
                "name": n.name,
                "size": n.size,
                "icon": ns.icon_for(n),
                "previewKind": ns.preview_kind(n),
            }
            for n in files
        ],
    }


def note_drop_upload(db: Session, qt: QuickTransfer, node: Node) -> None:
    """手机上每传完一个文件就记进去，页面刷新也能看到已传列表。"""
    ids = id_list(qt)
    ids.append(node.id)
    qt.node_ids = json.dumps(ids[-200:])
    qt.uses += 1
    qt.count = qt.uses
    qt.updated_at = utcnow()


# --------------------------------------------------------------------------- #
# 站内快传（send）
# --------------------------------------------------------------------------- #
def send_files(
    db: Session,
    sender: User,
    nodes: list[Node],
    *,
    to_username: str,
    message: str | None,
) -> QuickTransfer:
    receiver = db.scalars(select(User).where(User.username == to_username.strip())).first()
    if receiver is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到这个用户")
    if receiver.id == sender.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能发给自己，自己的文件直接移动就好")
    if receiver.banned:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "对方已被封禁，无法接收")

    total = 0
    for node in nodes:
        if node.type == "file":
            total += int(node.size or 0)
            continue
        ids = ns.subtree_ids(db, sender.id, node.id)
        total += int(
            sum(
                int(n.size or 0)
                for n in db.scalars(
                    select(Node).where(Node.id.in_(ids), Node.type == "file")
                ).all()
            )
        )
    if receiver.used_bytes + total > receiver.quota_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "对方空间不足，先让他清理一下")

    clones: list[Node] = []
    added = 0
    for node in nodes:
        clone, size = ns.copy_subtree(db, receiver, node, None)
        clones.append(clone)
        added += size
    ns.recalc_usage(db, receiver)

    name, _, count = _summary(nodes)
    qt = QuickTransfer(
        kind="send",
        owner_id=sender.id,
        peer_id=receiver.id,
        token=secrets.token_urlsafe(32),
        node_ids=json.dumps([c.id for c in clones]),
        name=name,
        size=added,
        count=count,
        message=(message or "").strip()[:FLASH_MESSAGE_MAX] or None,
        seen=False,
    )
    db.add(qt)
    db.flush()
    return qt


def _user_brief(db: Session, users: dict[int, User], uid: int | None) -> dict:
    user = users.get(uid) if uid else None
    if user is None:
        return {"id": uid, "username": "（已注销）", "displayName": "（已注销）"}
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name or user.username,
    }


def send_list(db: Session, user_id: int, *, incoming: bool, limit: int = 100) -> list[dict]:
    column = QuickTransfer.peer_id if incoming else QuickTransfer.owner_id
    rows = db.scalars(
        select(QuickTransfer)
        .where(QuickTransfer.kind == "send", column == user_id)
        .order_by(QuickTransfer.created_at.desc())
        .limit(limit)
    ).all()
    if not rows:
        return []
    peer_ids = {row.owner_id if incoming else row.peer_id for row in rows}
    users = {
        u.id: u
        for u in db.scalars(select(User).where(User.id.in_([p for p in peer_ids if p]))).all()
    }
    items = []
    for row in rows:
        data = payload(db, row, with_code=False)
        peer = row.owner_id if incoming else row.peer_id
        data["peer"] = _user_brief(db, users, peer)
        items.append(data)
    return items


def unread_count(db: Session, user_id: int) -> int:
    rows = db.scalars(
        select(QuickTransfer.id).where(
            QuickTransfer.kind == "send",
            QuickTransfer.peer_id == user_id,
            QuickTransfer.seen.is_(False),
        )
    ).all()
    return len(rows)


def mark_seen(db: Session, user_id: int, ids: list[int] | None = None) -> int:
    rows = db.scalars(
        select(QuickTransfer).where(
            QuickTransfer.kind == "send",
            QuickTransfer.peer_id == user_id,
            QuickTransfer.seen.is_(False),
        )
    ).all()
    changed = 0
    for row in rows:
        if ids and row.id not in ids:
            continue
        row.seen = True
        changed += 1
    return changed


# --------------------------------------------------------------------------- #
# 取件码（pickup）
# --------------------------------------------------------------------------- #
def create_pickup(
    db: Session, user: User, nodes: list[Node], *, days: int, max_uses: int | None
) -> QuickTransfer:
    days = max(1, min(30, int(days or 7)))
    if any(node.type != "file" for node in nodes):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "取件码只支持文件，文件夹请用分享链接"
        )
    for node in nodes:
        if node.blob_sha256 and is_blocked(db, node.blob_sha256):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "包含已被举报确认的文件，不能生成取件码")
    name, total, _ = _summary(nodes)
    qt = QuickTransfer(
        kind="pickup",
        owner_id=user.id,
        token=secrets.token_urlsafe(32),
        code=new_code(db),
        node_ids=json.dumps([n.id for n in nodes]),
        name=name,
        size=total,
        count=len(nodes),
        max_uses=max_uses,
        expires_at=_hint(days=days),
    )
    db.add(qt)
    db.flush()
    return qt


def pickup_guard(ip: str) -> None:
    now = time.time()
    q = _pickup_tries[ip]
    while q and now - q[0] > _PICKUP_WINDOW:
        q.popleft()
    if len(q) >= _PICKUP_LIMIT:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "取件码试错次数过多，请 10 分钟后再试")


def pickup_miss(ip: str) -> None:
    _pickup_tries[ip].append(time.time())


def pickup_hit(ip: str) -> None:
    _pickup_tries.pop(ip, None)


def normalize(code: str | None) -> str:
    return (code or "").strip().replace(" ", "").upper()


def find_pickup(db: Session, code: str) -> QuickTransfer:
    text = normalize(code)
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请输入取件码")
    qt = db.scalars(
        select(QuickTransfer).where(
            QuickTransfer.kind == "pickup", QuickTransfer.code == text
        )
    ).first()
    if not qt:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "取件码不存在，检查一下有没有输错")
    if _dead(qt):
        raise HTTPException(status.HTTP_410_GONE, _expire_reason(qt))
    if qt.max_uses is not None and qt.uses >= qt.max_uses:
        raise HTTPException(status.HTTP_410_GONE, "这个取件码的下载次数已用完")
    return qt


def pickup_public(db: Session, qt: QuickTransfer) -> dict:
    owner = db.get(User, qt.owner_id)
    nodes = _nodes(db, id_list(qt), qt.owner_id)
    return {
        "code": qt.code,
        "owner": (owner.display_name or owner.username) if owner else "（已注销）",
        "name": qt.name,
        "size": qt.size,
        "count": len(nodes),
        "expiresAt": _iso(qt.expires_at),
        "remaining": None if qt.max_uses is None else max(0, qt.max_uses - qt.uses),
        "items": [
            {
                "id": n.id,
                "name": n.name,
                "size": n.size,
                "mime": n.mime,
                "icon": ns.icon_for(n),
                "previewKind": ns.preview_kind(n),
            }
            for n in nodes
        ],
    }


def pickup_node(db: Session, qt: QuickTransfer, node_id: int | None) -> Node:
    ids = id_list(qt)
    target = ids[0] if node_id is None else node_id
    if target not in ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不在这条取件码里")
    node = db.get(Node, target)
    if not node or node.deleted_at or node.type != "file" or not node.blob_sha256:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件已被删除")
    if is_blocked(db, node.blob_sha256):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该文件已被举报确认，禁止下载")
    return node


def list_for_user(db: Session, user_id: int, kind: str, limit: int = 100) -> list[dict]:
    if kind == "send":
        return send_list(db, user_id, incoming=False, limit=limit)
    rows = db.scalars(
        select(QuickTransfer)
        .where(QuickTransfer.owner_id == user_id, QuickTransfer.kind == kind)
        .order_by(QuickTransfer.created_at.desc())
        .limit(limit)
    ).all()
    return [payload(db, row) for row in rows]


def get_owned(db: Session, user_id: int, qt_id: int, *, kind: str | None = None) -> QuickTransfer:
    qt = db.get(QuickTransfer, qt_id)
    if not qt or qt.owner_id != user_id or (kind and qt.kind != kind):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "记录不存在")
    return qt


def revoke(qt: QuickTransfer) -> None:
    qt.revoked = True
    qt.updated_at = utcnow()
