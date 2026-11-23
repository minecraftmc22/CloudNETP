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
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from . import node_service as ns
from . import notice_service
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
from .settings_store import QUICK_COPY_TEMPLATE_DEFAULT, get_flag, get_int, get_value
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


def is_dead(qt: QuickTransfer) -> bool:
    """对外用的名字：已取消或已过期，都不该再被使用。"""
    return _dead(qt)


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
        # 接收状态：接收方同意之前文件只在暂存区，不占「我的文件」
        data["sendStatus"] = send_status(qt)
        data["decidedAt"] = _iso(qt.decided_at)
        data["resultSeen"] = qt.result_seen
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
# 站内快传（send）：先放进暂存区，接收方同意后才落进「我的文件」
# --------------------------------------------------------------------------- #
# 待接收的有效期：超过就自动作废（记录留着给双方看，文件由清理任务回收）
SEND_DAYS = 7
DEFAULT_INBOX_NAME = "快传的文件"


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


def send_status(qt: QuickTransfer) -> str:
    """对外暴露的接收状态。

    「过期」是算出来的，不用后台改库：到点了还挂在待接收就按过期展示，
    文件在清理任务里回收。
    """
    if qt.kind != "send":
        return ""
    if qt.send_status == "pending" and qt.expires_at and qt.expires_at <= utcnow():
        return "expired"
    return qt.send_status or "accepted"


def receive_pref(db: Session, user_id: int) -> dict:
    """接收偏好：允许接收 / 自动同意 / 存放目录。没有设置行时按默认值走。"""
    row = db.get(UserSetting, user_id)
    return {
        "enabled": True if row is None else bool(row.quick_receive_enabled),
        "autoAccept": False if row is None else bool(row.quick_auto_accept),
        "folderId": None if row is None else row.quick_inbox_folder,
    }


def _stage_subtree(
    db: Session,
    owner: User,
    node: Node,
    qt_id: int,
    parent_id: int | None,
    _depth: int = 0,
) -> Node:
    """把节点（含子树）复制成 owner 名下的「待接收副本」。

    与「我的文件」里的复制不同，这里打上 quick_owned + pending_send_id：
    不占配额、不进搜索/WebDAV/我的文件，接收方只能在「快传」里预览。
    文件复用同一个 blob，只加引用、不复制物理数据。
    """
    if _depth > ns.MAX_DEPTH:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "目录层级过深，无法发送")
    if node.type == "folder":
        clone = Node(
            user_id=owner.id,
            parent_id=parent_id,
            type="folder",
            name=node.name,
            size=0,
            quick_owned=True,
            pending_send_id=qt_id,
        )
        db.add(clone)
        db.flush()
        children = db.scalars(
            select(Node).where(
                Node.user_id == node.user_id,
                Node.parent_id == node.id,
                Node.deleted_at.is_(None),
            )
        ).all()
        for child in children:
            _stage_subtree(db, owner, child, qt_id, clone.id, _depth + 1)
        return clone
    if node.blob_sha256:
        ns.retain_blob(db, node.blob_sha256, int(node.size or 0))
    clone = Node(
        user_id=owner.id,
        parent_id=parent_id,
        type="file",
        name=node.name,
        blob_sha256=node.blob_sha256,
        size=int(node.size or 0),
        mime=node.mime,
        quick_owned=True,
        pending_send_id=qt_id,
    )
    db.add(clone)
    db.flush()
    return clone


def inbox_folder(db: Session, user: User) -> Node:
    """收到的文件存到哪：用户挑的目录；没挑（或目录被删了）就用 我的文件/快传的文件。"""
    row = db.get(UserSetting, user.id)
    folder_id = row.quick_inbox_folder if row else None
    if folder_id:
        node = db.get(Node, folder_id)
        if node and node.user_id == user.id and node.type == "folder" and not node.deleted_at:
            return node
        # 目录没了就悄悄回默认，别让接收一直失败
        if row is not None:
            row.quick_inbox_folder = None
            db.flush()
    existing = db.scalars(
        select(Node).where(
            Node.user_id == user.id,
            Node.parent_id.is_(None),
            Node.type == "folder",
            Node.deleted_at.is_(None),
            Node.quick_owned.is_(False),
            func.lower(Node.name) == DEFAULT_INBOX_NAME.lower(),
        )
    ).first()
    if existing is not None:
        return existing
    return ns.create_folder(db, user, None, DEFAULT_INBOX_NAME)


def inbox_path(db: Session, user: User) -> dict:
    """设置页展示的「收到后存到哪」，含是否还在用默认位置。"""
    row = db.get(UserSetting, user.id)
    folder_id = row.quick_inbox_folder if row else None
    node = db.get(Node, folder_id) if folder_id else None
    if node is not None and node.user_id == user.id and node.type == "folder" and not node.deleted_at:
        trail = ns.breadcrumb(db, user.id, node.id)
        return {"folderId": node.id, "isDefault": False, "path": trail}
    return {
        "folderId": None,
        "isDefault": True,
        "path": [
            {"id": None, "name": "我的文件"},
            {"id": None, "name": DEFAULT_INBOX_NAME},
        ],
    }


def set_inbox_folder(db: Session, user: User, folder_id: int | None) -> dict:
    """folder_id=None 表示恢复默认（我的文件/快传的文件）。"""
    row = db.get(UserSetting, user.id)
    if row is None:
        row = UserSetting(user_id=user.id)
        db.add(row)
    if folder_id is None:
        row.quick_inbox_folder = None
    else:
        node = ns.require_parent(db, user.id, folder_id)
        row.quick_inbox_folder = node.id if node else None
    db.flush()
    return inbox_path(db, user)


def send_files(
    db: Session,
    sender: User,
    nodes: list[Node],
    *,
    to_username: str,
    message: str | None,
) -> QuickTransfer:
    """投递到对方的暂存区，等对方同意后才搬进他的「我的文件」。

    对方在设置里开了「自动同意」就直接落盘（等于以前的行为），
    关了「接收快传文件」则直接 403。
    """
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
    pref = receive_pref(db, receiver.id)
    if not pref["enabled"]:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "对方关闭了「接收快传文件」，现在发不过去"
        )

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

    name, _, count = _summary(nodes)
    qt = QuickTransfer(
        kind="send",
        owner_id=sender.id,
        peer_id=receiver.id,
        token=secrets.token_urlsafe(32),
        name=name,
        size=total,
        count=count,
        message=(message or "").strip()[:FLASH_MESSAGE_MAX] or None,
        seen=False,
        send_status="pending",
        result_seen=True,
        expires_at=_hint(days=SEND_DAYS),
    )
    db.add(qt)
    db.flush()
    staged = [_stage_subtree(db, receiver, node, qt.id, None) for node in nodes]
    qt.node_ids = json.dumps([n.id for n in staged])
    db.flush()
    if pref["autoAccept"]:
        accept_send(db, receiver, qt)
    # 接收方的消息中心也提一条：否则只有打开「快传 → 收到」才看得到
    sender_name = (sender.display_name or sender.username) or "有人"
    if pref["autoAccept"]:
        notice_service.notify(
            db,
            receiver.id,
            "quick",
            f"{sender_name} 给你发了 {qt.count} 个文件",
            "已按你的设置自动接收，存放在「快传 → 收到」",
            "#/quick",
        )
    else:
        notice_service.notify(
            db,
            receiver.id,
            "quick",
            f"{sender_name} 给你发了 {qt.count} 个文件",
            (qt.message or "打开快传查看，同意后才会存进你的网盘"),
            "#/quick",
        )
    return qt


def get_send(db: Session, user_id: int, qt_id: int, *, as_peer: bool = True) -> QuickTransfer:
    qt = db.get(QuickTransfer, qt_id)
    if not qt or qt.kind != "send":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "记录不存在")
    if as_peer and qt.peer_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "记录不存在")
    if not as_peer and qt.owner_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "记录不存在")
    return qt


def _send_staging(db: Session, qt: QuickTransfer) -> list[Node]:
    """这条快传还在暂存区里的顶层副本（已存入/已拒绝时为空）。"""
    out: list[Node] = []
    for node_id in id_list(qt):
        node = db.get(Node, node_id)
        if node is not None and node.pending_send_id == qt.id:
            out.append(node)
    return out


def _purge_staging(db: Session, qt: QuickTransfer) -> int:
    """删掉暂存区里的副本（含子树）并回收 blob 引用。只动 pending_send_id 指向自己的节点。"""
    ids: list[int] = []
    for node in _send_staging(db, qt):
        ids.extend(ns.subtree_ids(db, node.user_id, node.id))
    qt.node_ids = json.dumps([])
    if not ids:
        return 0
    shas = db.scalars(
        select(Node.blob_sha256).where(Node.id.in_(ids), Node.blob_sha256.is_not(None))
    ).all()
    # 先删节点再减 blob 引用：node.blob_sha256 有外键，顺序反了会撞 FK
    db.execute(Node.__table__.delete().where(Node.id.in_(ids)))
    db.flush()
    for sha in {s for s in shas if s}:
        ns.release_blob(db, sha)
    db.flush()
    return len(ids)


def accept_send(db: Session, user: User, qt: QuickTransfer) -> list[Node]:
    """接收方同意：把暂存区的副本搬进收件目录，成为普通的「我的文件」。"""
    if qt.send_status != "pending":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "这条快传已经处理过了")
    if send_status(qt) == "expired":
        raise HTTPException(status.HTTP_410_GONE, "这条快传已经过期了")
    roots = _send_staging(db, qt)
    if not roots:
        raise HTTPException(status.HTTP_410_GONE, "文件已经不在暂存区了")

    total = 0
    all_ids: list[int] = []
    for root in roots:
        ids = ns.subtree_ids(db, user.id, root.id)
        all_ids.extend(ids)
        total += int(
            sum(
                int(n or 0)
                for n in db.scalars(
                    select(Node.size).where(Node.id.in_(ids), Node.type == "file")
                ).all()
            )
        )
    if (user.used_bytes or 0) + total > (user.quota_bytes or 0):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "你的空间不够存放这些文件，先清理一下")

    parent = inbox_folder(db, user)
    parent_id = parent.id if parent else None
    for root in roots:
        # 收件目录里已有同名 → 落成「名字 (1)」，和资源管理器一致
        root.name = ns.unique_name(db, user.id, parent_id, root.name, exclude_id=root.id)
        root.parent_id = parent_id
        root.updated_at = utcnow()
    db.execute(
        update(Node)
        .where(Node.id.in_(all_ids))
        .values(quick_owned=False, pending_send_id=None)
    )
    db.flush()

    qt.node_ids = json.dumps([root.id for root in roots])
    qt.send_status = "accepted"
    qt.decided_at = utcnow()
    qt.updated_at = utcnow()
    # 做过决定了就等于看过了，别再算进「未读」
    qt.seen = True
    # 同意是「好消息」，不打扰发送方；只有被拒绝 / 过期才提示一次
    qt.result_seen = True
    db.flush()
    ns.recalc_usage(db, user)
    return roots


def reject_send(db: Session, user: User, qt: QuickTransfer) -> None:
    """接收方拒绝：暂存区的副本直接删掉，发送方看到「文件被拒绝」。"""
    if qt.send_status != "pending":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "这条快传已经处理过了")
    _purge_staging(db, qt)
    qt.send_status = "rejected"
    qt.decided_at = utcnow()
    qt.updated_at = utcnow()
    qt.seen = True
    qt.result_seen = False
    db.flush()


def send_items(db: Session, qt: QuickTransfer, user: User, parent_id: int | None = None) -> dict:
    """接收方在同意前逐层浏览这条快传里到底有什么（双击文件夹/文件时用）。"""
    if qt.peer_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "记录不存在")
    if send_status(qt) != "pending":
        raise HTTPException(status.HTTP_410_GONE, "这条快传已经处理过了")

    if parent_id is None:
        nodes = _send_staging(db, qt)
        breadcrumb = [{"id": None, "name": qt.name}]
    else:
        folder = db.get(Node, parent_id)
        if (
            folder is None
            or folder.pending_send_id != qt.id
            or folder.user_id != user.id
            or folder.type != "folder"
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "目录不存在")
        nodes = db.scalars(
            select(Node).where(
                Node.user_id == user.id,
                Node.parent_id == folder.id,
                Node.deleted_at.is_(None),
                Node.pending_send_id == qt.id,
            )
        ).all()
        trail: list[dict] = []
        cursor: Node | None = folder
        guard = 0
        while cursor is not None and cursor.pending_send_id == qt.id and guard < ns.MAX_DEPTH:
            guard += 1
            trail.append({"id": cursor.id, "name": cursor.name})
            cursor = db.get(Node, cursor.parent_id) if cursor.parent_id else None
        trail.reverse()
        breadcrumb = [{"id": None, "name": qt.name}, *trail]

    # 文件夹在前，再按名字排，跟「我的文件」的列表顺序一致
    ordered = sorted(nodes, key=lambda n: (n.type != "folder", (n.name or "").lower()))
    counts = ns.child_counts(db, user.id, [n.id for n in ordered if n.type == "folder"])
    return {
        "items": [ns.node_payload(n, counts.get(n.id)) for n in ordered],
        "breadcrumb": breadcrumb,
        "send": payload(db, qt, with_code=False),
    }


def pending_count(db: Session, user_id: int) -> int:
    rows = db.scalars(
        select(QuickTransfer).where(
            QuickTransfer.kind == "send",
            QuickTransfer.peer_id == user_id,
            QuickTransfer.send_status == "pending",
        )
    ).all()
    return sum(1 for row in rows if send_status(row) == "pending")


def unseen_results(db: Session, user_id: int) -> int:
    """发送方还没看过的接收结果（被拒绝 / 已过期时提示一次）。"""
    rows = db.scalars(
        select(QuickTransfer).where(
            QuickTransfer.kind == "send",
            QuickTransfer.owner_id == user_id,
            QuickTransfer.result_seen.is_(False),
        )
    ).all()
    return sum(1 for row in rows if send_status(row) != "pending")


def ack_results(db: Session, user_id: int) -> int:
    rows = db.scalars(
        select(QuickTransfer).where(
            QuickTransfer.kind == "send",
            QuickTransfer.owner_id == user_id,
            QuickTransfer.result_seen.is_(False),
        )
    ).all()
    changed = 0
    for row in rows:
        if send_status(row) == "pending":
            continue
        row.result_seen = True
        changed += 1
    return changed


def cleanup_sends(db: Session, *, keep_days: int = 30) -> dict:
    """清理站内快传：过期的待接收作废并回收暂存文件，处理完的老记录按天数删。"""
    now = utcnow()
    expired = 0
    purged = 0
    for qt in db.scalars(select(QuickTransfer).where(QuickTransfer.kind == "send")).all():
        status_now = send_status(qt)
        if status_now == "expired" and qt.send_status == "pending":
            _purge_staging(db, qt)
            qt.send_status = "expired"
            qt.result_seen = False
            qt.updated_at = now
            expired += 1
            continue
        if status_now == "pending":
            continue
        stamp = qt.decided_at or qt.updated_at
        if stamp and (now - stamp).days >= keep_days:
            _purge_staging(db, qt)  # 兜底：中途失败可能留下暂存文件
            db.delete(qt)
            purged += 1
    db.flush()
    return {"expired": expired, "purged": purged}


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
    # 待接收的排前面：接收方一进来就该看到「等你同意」的那几条
    rows = sorted(rows, key=lambda row: (send_status(row) != "pending", -row.id))
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
        # 生成后自动复制分享文案 + 文案模板（占位符 {site} {file} {code} {link}）
        "autoCopy": get_flag(db, "quick_auto_copy"),
        "copyTemplate": copy_template(db),
    }


def copy_template(db: Session) -> str:
    """管理员改过的分享文案；清空就回落到默认那段。"""
    text = get_value(db, "quick_copy_template")
    return text if text.strip() else QUICK_COPY_TEMPLATE_DEFAULT


def render_copy(template: str, *, site: str, file: str, code: str, link: str) -> str:
    """把 {site} {file} {code} {link} 换掉；占位符大小写不敏感（{File} 也认）。"""
    values = {"site": site, "file": file, "code": code, "link": link}
    out = template
    for key, value in values.items():
        out = re.sub(
            r"\{" + key + r"\}",
            lambda _match, text=value: text,
            out,
            flags=re.IGNORECASE,
        )
    return out


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

    # 上传了但没被任何取件码引用（创建中途失败）的孤儿文件，也按保留期回收。
    # 站内快传的待接收副本（pending_send_id 非空）不在此列：它们归 send 的生命周期管。
    orphans = db.scalars(
        select(Node).where(
            Node.quick_owned.is_(True),
            Node.pending_send_id.is_(None),
            Node.created_at <= cutoff,
        )
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
    sends = cleanup_sends(db)
    return {
        "codes": removed_codes,
        "files": removed_files,
        "bytes": removed_bytes,
        "retentionDays": retention_days,
        **sends,
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


def list_for_user(
    db: Session, user_id: int, kind: str, limit: int = 100, scope: str = "all"
) -> list[dict]:
    """scope: active 只看可用 / history 只看失效 / all 全都要（默认）。"""
    if kind == "send":
        return send_list(db, user_id, incoming=False, limit=limit)
    rows = db.scalars(
        select(QuickTransfer)
        .where(QuickTransfer.owner_id == user_id, QuickTransfer.kind == kind)
        .order_by(QuickTransfer.created_at.desc())
        .limit(limit)
    ).all()
    if scope == "active":
        rows = [row for row in rows if not _dead(row)]
    elif scope == "history":
        rows = [row for row in rows if _dead(row)]
    return [payload(db, row) for row in rows]


def counts_for(db: Session, user_id: int, kind: str) -> dict:
    """某个 kind 下「还能用 / 已失效」各有多少条，给列表上的角标用。"""
    rows = db.scalars(
        select(QuickTransfer).where(
            QuickTransfer.owner_id == user_id, QuickTransfer.kind == kind
        )
    ).all()
    dead = sum(1 for row in rows if _dead(row))
    return {"active": len(rows) - dead, "history": dead}


def get_owned(db: Session, user_id: int, qt_id: int, *, kind: str | None = None) -> QuickTransfer:
    qt = db.get(QuickTransfer, qt_id)
    if not qt or qt.owner_id != user_id or (kind and qt.kind != kind):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "记录不存在")
    return qt


def revoke(qt: QuickTransfer) -> None:
    qt.revoked = True
    qt.updated_at = utcnow()


def purge(db: Session, qt: QuickTransfer) -> None:
    """彻底删掉一条已失效的记录。

    取件码顺手回收它自己的快传暂存文件（`quick_owned`）：被别的取件码还引用着的、
    或者本来就是从「我的文件」选来的，一律不动 —— 那属于用户自己的网盘。
    """
    if qt.kind == "pickup":
        node_ids = id_list(qt)
        db.execute(delete(QuickPickupItem).where(QuickPickupItem.quick_id == qt.id))
        db.flush()
        doomed: list[Node] = []
        for node_id in node_ids:
            node = db.get(Node, node_id)
            if node is None or not node.quick_owned:
                continue
            still_used = db.scalars(
                select(QuickPickupItem.id).where(QuickPickupItem.node_id == node_id).limit(1)
            ).first()
            if still_used is not None:
                continue
            doomed.append(node)
        # 先删节点再减 blob 引用（node.blob_sha256 有外键，顺序反了会撞 FK）
        shas = [node.blob_sha256 for node in doomed if node.blob_sha256]
        for node in doomed:
            db.delete(node)
        db.flush()
        for sha in shas:
            ns.release_blob(db, sha)
        db.flush()
    db.delete(qt)


def purge_dead(db: Session, user_id: int, kind: str) -> int:
    """清空历史：把某个 kind 下所有已失效的记录删掉，返回条数。"""
    rows = db.scalars(
        select(QuickTransfer).where(
            QuickTransfer.owner_id == user_id, QuickTransfer.kind == kind
        )
    ).all()
    doomed = [row for row in rows if _dead(row)]
    for row in doomed:
        purge(db, row)
    return len(doomed)
