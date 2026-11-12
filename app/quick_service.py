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
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import node_service as ns
from .models import (
    BlockedBlob,
    Node,
    QuickPickupItem,
    QuickTransfer,
    ShareReport,
    User,
    UserSetting,
    utcnow,
)
from .settings_store import get_int, get_value
from .share_service import CODE_ALPHABET, is_blocked

DROP_MINUTES = (10, 30, 60, 120)
PICKUP_DAYS = (1, 3, 7, 30)
PICKUP_CODE_LEN = 6
FLASH_MESSAGE_MAX = 200
MAX_ITEMS = 100

# 与前端提示、管理后台文案保持一致：命中黑名单时对外只说这一句
BLOCKED_UPLOAD_MSG = "此文件包含法律禁止的内容，无法上传"
TERMS_REQUIRED_MSG = "请先阅读并同意快传使用规范"

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


# --------------------------------------------------------------------------- #
# 使用规范：必须先同意，且上传内容要过黑名单
# --------------------------------------------------------------------------- #
def terms_of(db: Session, user_id: int) -> dict:
    row = db.get(UserSetting, user_id)
    at = row.quick_terms_at if row else None
    return {"accepted": at is not None, "acceptedAt": _iso(at)}


def accept_terms(db: Session, user: User) -> dict:
    row = db.get(UserSetting, user.id)
    if row is None:
        # 正常注册时会建，这里兜一下历史账号
        row = UserSetting(user_id=user.id)
        db.add(row)
    if row.quick_terms_at is None:
        row.quick_terms_at = utcnow()
        db.flush()
    return terms_of(db, user.id)


def require_terms(db: Session, user: User) -> None:
    """创建任何快传之前都要同意一次规范（后端把关，不只靠前端弹窗）。"""
    if terms_of(db, user.id)["accepted"]:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, TERMS_REQUIRED_MSG)


def assert_upload_allowed(db: Session, sha256: str | None) -> None:
    """命中被举报确认的黑名单就不给传。

    只在服务端算出的哈希上判断（complete 阶段），客户端声明的哈希只当提前拦截，
    否则改个 sha256 就能绕过。
    """
    digest = (sha256 or "").strip().lower()
    if digest and is_blocked(db, digest):
        raise HTTPException(status.HTTP_403_FORBIDDEN, BLOCKED_UPLOAD_MSG)


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
        data["items"] = [_entry_payload(entry) for entry in pickup_entries(db, qt)]
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
    require_terms(db, user)
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
def _has_blocked_content(db: Session, user_id: int, node: Node) -> bool:
    """文件（或文件夹里任意一层）是否含已被举报确认的内容。"""
    if node.type == "file":
        return bool(node.blob_sha256 and is_blocked(db, node.blob_sha256))
    ids = ns.subtree_ids(db, user_id, node.id)
    if not ids:
        return False
    shas = db.scalars(
        select(Node.blob_sha256).where(
            Node.id.in_(ids), Node.type == "file", Node.blob_sha256.is_not(None)
        )
    ).all()
    if not shas:
        return False
    return db.scalars(
        select(BlockedBlob.sha256).where(BlockedBlob.sha256.in_(set(shas))).limit(1)
    ).first() is not None


def send_files(
    db: Session,
    sender: User,
    nodes: list[Node],
    *,
    to_username: str,
    message: str | None,
) -> QuickTransfer:
    require_terms(db, sender)
    for node in nodes:
        if _has_blocked_content(db, sender.id, node):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"「{node.name}」{BLOCKED_UPLOAD_MSG}")

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
# 每个文件各自有有效期与下载次数（quick_pickup_items）。老的取件码没有 item 行，
# 读取时回落到 quick_transfers 上的同名字段，行为与以前一致。
def limits(db: Session) -> dict:
    """管理员设的上限：有效期最长几天、单个文件最多下载几次（0 = 不限）、过期后保留几天。"""
    max_days = get_int(db, "quick_max_days") or 30
    max_uses = get_int(db, "quick_max_uses")
    retention = get_int(db, "quick_retention_days")
    if get_value(db, "quick_retention_days").strip() == "":
        retention = 7
    return {
        "maxDays": max(1, min(365, max_days)),
        "maxUses": max(0, min(10000, max_uses)),
        "retentionDays": max(0, min(365, retention)),
    }


def clamp_days(db: Session, days: int | None) -> int:
    cap = limits(db)["maxDays"]
    try:
        value = int(days if days is not None else 7)
    except (TypeError, ValueError):
        value = 7
    return max(1, min(cap, value))


def clamp_uses(db: Session, max_uses: int | None) -> int | None:
    """None / 0 = 不限；超过管理员上限就按上限来。"""
    try:
        value = int(max_uses) if max_uses is not None else 0
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return None
    cap = limits(db)["maxUses"]
    if cap and value > cap:
        value = cap
    return min(10000, value)


def _item_rows(db: Session, qt: QuickTransfer) -> list[QuickPickupItem]:
    return db.scalars(
        select(QuickPickupItem)
        .where(QuickPickupItem.quick_id == qt.id)
        .order_by(QuickPickupItem.id.asc())
    ).all()


def pickup_entries(db: Session, qt: QuickTransfer) -> list[dict]:
    """取件码里的文件 + 各自的有效期/次数；老记录回落到整条码的设置。"""
    nodes = {n.id: n for n in _nodes(db, id_list(qt), qt.owner_id)}
    rows = {row.node_id: row for row in _item_rows(db, qt)}
    out: list[dict] = []
    for node_id in id_list(qt):
        node = nodes.get(node_id)
        if node is None:
            continue
        row = rows.get(node_id)
        expires = row.expires_at if row is not None else qt.expires_at
        max_uses = row.max_uses if row is not None else qt.max_uses
        uses = row.uses if row is not None else qt.uses
        expired = bool(expires and expires <= utcnow())
        used_up = bool(max_uses is not None and uses >= max_uses)
        out.append(
            {
                "node": node,
                "row": row,
                "expiresAt": expires,
                "maxUses": max_uses,
                "uses": uses,
                "expired": expired,
                "usedUp": used_up,
                "alive": not expired and not used_up,
            }
        )
    return out


def _entry_payload(entry: dict) -> dict:
    node = entry["node"]
    remaining = None if entry["maxUses"] is None else max(0, entry["maxUses"] - entry["uses"])
    return {
        "id": node.id,
        "name": node.name,
        "size": node.size,
        "mime": node.mime,
        "icon": ns.icon_for(node),
        "previewKind": ns.preview_kind(node),
        "expiresAt": _iso(entry["expiresAt"]),
        "maxUses": entry["maxUses"],
        "uses": entry["uses"],
        "remaining": remaining,
        "expired": entry["expired"],
        "usedUp": entry["usedUp"],
        "alive": entry["alive"],
    }


def create_pickup(
    db: Session,
    user: User,
    entries: list[dict],
    *,
    days: int | None = None,
    max_uses: int | None = None,
) -> QuickTransfer:
    """entries: [{"node": Node, "days": int|None, "maxUses": int|None}]

    不传 docs 的逐文件设置时，回落到入参里的 days / max_uses（老调用方与 AI 工具都能用）。
    """
    require_terms(db, user)
    if not entries:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请先选择文件")
    if any(entry["node"].type != "file" for entry in entries):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "取件码只支持文件，文件夹请用分享链接")

    now = utcnow()
    farthest: datetime | None = None
    total = 0
    for entry in entries:
        node = entry["node"]
        if node.blob_sha256 and is_blocked(db, node.blob_sha256):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "包含已被举报确认的文件，不能生成取件码")
        day_value = clamp_days(db, entry.get("days", days))
        uses_value = clamp_uses(db, entry.get("maxUses", max_uses))
        entry["days"] = day_value
        entry["maxUses"] = uses_value
        entry["expiresAt"] = now + timedelta(days=day_value)
        total += int(node.size or 0)
        if farthest is None or entry["expiresAt"] > farthest:
            farthest = entry["expiresAt"]

    nodes = [entry["node"] for entry in entries]
    name, _, _ = _summary(nodes)
    code = new_code(db)
    qt = QuickTransfer(
        kind="pickup",
        owner_id=user.id,
        token=secrets.token_urlsafe(32),
        code=code,
        node_ids=json.dumps([n.id for n in nodes]),
        name=name,
        size=total,
        count=len(nodes),
        max_uses=None,
        expires_at=farthest,
    )
    db.add(qt)
    db.flush()
    for entry in entries:
        db.add(
            QuickPickupItem(
                quick_id=qt.id,
                node_id=entry["node"].id,
                max_uses=entry["maxUses"],
                uses=0,
                expires_at=entry["expiresAt"],
            )
        )
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
    entries = pickup_entries(db, qt)
    if not entries:
        raise HTTPException(status.HTTP_410_GONE, "这个取件码里的文件已经失效")
    if not any(entry["alive"] for entry in entries):
        raise HTTPException(status.HTTP_410_GONE, "这个取件码已经过期或下载次数用完了")
    return qt


def pickup_public(db: Session, qt: QuickTransfer) -> dict:
    owner = db.get(User, qt.owner_id)
    entries = pickup_entries(db, qt)
    alive = [entry for entry in entries if entry["alive"]]
    return {
        "code": qt.code,
        "owner": (owner.display_name or owner.username) if owner else "（已注销）",
        "name": qt.name,
        "size": qt.size,
        "count": len(entries),
        # 整条码的「最后失效时间」= 里面最晚过期的那个文件
        "expiresAt": _iso(qt.expires_at),
        "remaining": None,
        "aliveCount": len(alive),
        "items": [_entry_payload(entry) for entry in entries],
    }


def pickup_entry(db: Session, qt: QuickTransfer, node_id: int | None) -> dict:
    entries = pickup_entries(db, qt)
    target = entries[0] if node_id is None else None
    if node_id is not None:
        for entry in entries:
            if entry["node"].id == node_id:
                target = entry
                break
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不在这条取件码里")
    if not target["alive"]:
        raise HTTPException(
            status.HTTP_410_GONE,
            "这个文件已经过期" if target["expired"] else "这个文件的下载次数已用完",
        )
    node = target["node"]
    if node.deleted_at or not node.blob_sha256:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件已被删除")
    if is_blocked(db, node.blob_sha256):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该文件已被举报确认，禁止下载")
    return target


def pickup_node(db: Session, qt: QuickTransfer, node_id: int | None) -> Node:
    return pickup_entry(db, qt, node_id)["node"]


def consume_pickup(db: Session, qt: QuickTransfer, node: Node) -> None:
    """一次真实下载：整条码的计数 + 这个文件的计数各加一。"""
    qt.uses += 1
    qt.updated_at = utcnow()
    row = db.scalars(
        select(QuickPickupItem).where(
            QuickPickupItem.quick_id == qt.id, QuickPickupItem.node_id == node.id
        )
    ).first()
    if row is not None:
        row.uses += 1


def pickup_save(
    db: Session, user: User, qt: QuickTransfer, parent_id: int | None
) -> list[dict]:
    """登录用户把取件码里的文件转存到自己的网盘（正常文件，占自己的配额）。"""
    ns.require_parent(db, user.id, parent_id)
    entries = [entry for entry in pickup_entries(db, qt) if entry["alive"]]
    if not entries:
        raise HTTPException(status.HTTP_410_GONE, "这条取件码里没有可取的文件了")
    total = sum(int(entry["node"].size or 0) for entry in entries)
    if (user.used_bytes or 0) + total > (user.quota_bytes or 0):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "你的空间不够存放这些文件，先清理一下")

    created: list[Node] = []
    for entry in entries:
        node = entry["node"]
        if not node.blob_sha256:
            continue
        if is_blocked(db, node.blob_sha256):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"「{node.name}」已被举报确认，不能转存")
        created.append(
            ns.create_file(db, user, parent_id, node.name, node.blob_sha256, int(node.size or 0), node.mime)
        )
    ns.recalc_usage(db, user)
    return created


def overview(db: Session) -> dict:
    """管理后台的快传概况：多少条码、多少快传文件、占多少物理空间。"""
    rows = db.scalars(select(QuickTransfer).where(QuickTransfer.kind == "pickup")).all()
    alive = sum(1 for qt in rows if not _dead(qt))
    files = db.execute(
        select(func.count(Node.id), func.coalesce(func.sum(Node.size), 0)).where(
            Node.quick_owned.is_(True)
        )
    ).one()
    limit = limits(db)
    return {
        "codes": len(rows),
        "alive": alive,
        "expired": len(rows) - alive,
        "files": int(files[0] or 0),
        "bytes": int(files[1] or 0),
        **limit,
    }


def cleanup_expired(db: Session, *, retention_days: int | None = None) -> dict:
    """回收过期 / 撤销的取件码文件（含物理 blob），返回清理数量。

    只删 quick_owned 的节点：从「我的文件」里选来生成取件码的那些文件属于用户自己的网盘，
    过期只是不能再取件，绝不能连用户的文件一起删。
    """
    if retention_days is None:
        retention_days = limits(db)["retentionDays"]
    retention_days = max(0, min(365, int(retention_days)))
    now = utcnow()
    cutoff = now - timedelta(days=retention_days)

    rows = db.scalars(select(QuickTransfer).where(QuickTransfer.kind == "pickup")).all()
    removed_files = 0
    removed_bytes = 0
    removed_codes = 0
    doomed: list[Node] = []
    for qt in rows:
        if not _dead(qt):
            continue
        dead_since = qt.updated_at if qt.revoked else (qt.expires_at or qt.updated_at)
        if dead_since and dead_since > cutoff:
            continue
        node_ids = id_list(qt)
        # 还被别的活着的取件码引用就不动
        for node_id in node_ids:
            node = db.get(Node, node_id)
            if node is None or not node.quick_owned:
                continue
            referenced = db.scalars(
                select(QuickPickupItem.id).where(QuickPickupItem.node_id == node_id)
            ).all()
            busy = False
            for item_id in referenced:
                item = db.get(QuickPickupItem, item_id)
                if item is None or item.quick_id == qt.id:
                    continue
                other = db.get(QuickTransfer, item.quick_id)
                if other is not None and not _dead(other):
                    busy = True
                    break
            if busy:
                continue
            removed_bytes += int(node.size or 0)
            doomed.append(node)
            removed_files += 1
        db.execute(
            delete(QuickPickupItem).where(QuickPickupItem.quick_id == qt.id)
        )
        db.delete(qt)
        removed_codes += 1

    # 上传了但没被任何取件码引用（创建中途失败）的孤儿文件，也按保留期回收
    orphans = db.scalars(
        select(Node).where(Node.quick_owned.is_(True), Node.created_at <= cutoff)
    ).all()
    for node in orphans:
        used = db.scalars(
            select(QuickPickupItem.id).where(QuickPickupItem.node_id == node.id).limit(1)
        ).first()
        if used is not None:
            continue
        removed_bytes += int(node.size or 0)
        doomed.append(node)
        removed_files += 1

    # 先删节点再减 blob 引用：node.blob_sha256 有外键，顺序反了会撞 FK 约束
    shas = [node.blob_sha256 for node in doomed if node.blob_sha256]
    for node in doomed:
        db.delete(node)
    db.flush()
    for sha in shas:
        ns.release_blob(db, sha)
    db.flush()
    return {
        "codes": removed_codes,
        "files": removed_files,
        "bytes": removed_bytes,
        "retentionDays": retention_days,
    }


def report_pickup(
    db: Session, qt: QuickTransfer, node: Node, *, reason: str | None, ip: str | None
) -> ShareReport:
    """取件页的举报：落在分享举报表里，管理员用同一套审核流程拉黑哈希。

    同一个取件码里的同一个文件重复举报不重复入库，避免刷屏。
    """
    sha = node.blob_sha256 or ""
    if sha:
        exists = db.scalars(
            select(ShareReport).where(
                ShareReport.quick_id == qt.id,
                ShareReport.blob_sha256 == sha,
                ShareReport.status == "pending",
            )
        ).first()
        if exists is not None:
            return exists
    report = ShareReport(
        share_id=0,
        quick_id=qt.id,
        token=qt.token,
        blob_sha256=sha or None,
        first_name=node.name,
        size=int(node.size or 0),
        is_folder=False,
        reason=(reason or "").strip()[:FLASH_MESSAGE_MAX],
        reporter_ip=ip,
    )
    db.add(report)
    db.flush()
    return report


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
