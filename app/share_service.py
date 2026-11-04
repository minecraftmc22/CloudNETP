"""分享链接：创建、状态判定、提取码校验与公开访问。

设计要点：
- token 用 secrets.token_urlsafe(32)（43 字符，不可枚举）
- 提取码只存 scrypt 哈希，服务端核验；连错 8 次锁 10 分钟（按 IP + 分享）
- 通过后发一个 HMAC 签名 Cookie（6 小时），下载/预览/文本/目录共用
- 文件夹分享只允许访问被分享子树内的节点，防止顺着 parent_id 越权
"""

from __future__ import annotations

import json
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta

from fastapi import HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import node_service as ns
from .models import BlockedBlob, Node, Share, User, utcnow
from .profile_service import author_payload
from .security import (
    client_ip,
    hash_password,
    sign_resource,
    verify_password,
    verify_resource,
)

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

_UNLOCK_WINDOW = 10 * 60
_UNLOCK_LIMIT = 8
_unlock_failures: dict[str, deque[float]] = defaultdict(deque)

# 举报限流：同一 IP 对同一分享 10 分钟 1 次，全站每小时 30 次
_REPORT_WINDOW = 10 * 60
_REPORT_LIMIT = 1
_REPORT_GLOBAL_WINDOW = 3600
_REPORT_GLOBAL_LIMIT = 30
_report_failures: dict[str, deque[float]] = defaultdict(deque)
_report_global: deque[float] = deque()


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def normalize_code(code: str | None) -> str:
    return (code or "").strip().replace(" ", "").upper()


def code_problem(code: str | None) -> str | None:
    text = normalize_code(code)
    if not text:
        return None
    if not 1 <= len(text) <= 8:
        return "提取码请填 1–8 位字母或数字"
    if not text.isalnum():
        return "提取码只能包含字母或数字"
    return None


def random_code(length: int = 4) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def _iso(value) -> str | None:
    return value.isoformat() + "Z" if value else None


def cookie_name(token: str) -> str:
    return f"cnp_s_{token[:12]}"


# --------------------------------------------------------------------------- #
# 创建与展示
# --------------------------------------------------------------------------- #
def root_ids(share: Share) -> list[int]:
    """分享的根节点 id 列表：合集取 node_ids，单节点回退 node_id。"""
    if share.node_ids:
        try:
            data = json.loads(share.node_ids)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, list):
            ids = [int(x) for x in data if isinstance(x, (int, str)) and str(x).isdigit()]
            if ids:
                return ids
    return [share.node_id]


def is_multi(share: Share) -> bool:
    return len(root_ids(share)) > 1


def create_share(
    db: Session,
    user: User,
    node: Node,
    *,
    days: int | None = None,
    code: str | None = None,
    max_downloads: int | None = None,
    nodes: list[Node] | None = None,
) -> Share:
    """创建分享：nodes 多于一项时生成「合集分享」，否则就是普通的单节点分享。"""
    items = nodes or [node]
    for item in items:
        assert_shareable(db, item)
    text = normalize_code(code)
    share = Share(
        user_id=user.id,
        node_id=items[0].id,
        node_ids=json.dumps([i.id for i in items]) if len(items) > 1 else None,
        token=secrets.token_urlsafe(32),
        extract_code=hash_password(text) if text else None,
        expires_at=utcnow() + timedelta(days=days) if days else None,
        max_downloads=max_downloads,
        download_count=0,
        revoked=False,
    )
    db.add(share)
    db.flush()
    return share


def _status_from(share: Share, alive: bool) -> str:
    if share.revoked:
        return "revoked"
    if share.expires_at and share.expires_at <= utcnow():
        return "expired"
    if share.max_downloads is not None and share.download_count >= share.max_downloads:
        return "exhausted"
    return "active" if alive else "missing"


def status_of(db: Session, share: Share, node: Node | None = None) -> str:
    ids = root_ids(share)
    if node is not None and len(ids) == 1:
        alive = not node.deleted_at
    else:
        alive = any(
            target is not None and not target.deleted_at
            for target in (db.get(Node, i) for i in ids)
        )
    return _status_from(share, alive)


def share_payload(
    db: Session, share: Share, node: Node | None = None, nodes: dict[int, Node] | None = None
) -> dict:
    ids = root_ids(share)
    multi = len(ids) > 1
    if nodes is None:
        nodes = {}
    if node is not None:
        nodes[ids[0]] = node
    if multi:
        for rid in ids:
            if rid not in nodes:
                nodes[rid] = db.get(Node, rid)
    alive_nodes = [n for n in (nodes.get(i) for i in ids) if n is not None and not n.deleted_at]

    if multi:
        total = sum(int(n.size or 0) for n in alive_nodes if n.type == "file")
        alive_count = len(alive_nodes)
        return {
            "id": share.id,
            "token": share.token,
            "url": f"/s/{share.token}",
            "nodeId": share.node_id,
            "nodeIds": ids,
            "itemCount": alive_count,
            "name": f"{alive_count} 个项目",
            "type": "multi",
            "size": total,
            "icon": "i-folder",
            "previewKind": "none",
            "hasCode": bool(share.extract_code),
            "status": _status_from(share, bool(alive_nodes)),
            "expiresAt": _iso(share.expires_at),
            "maxDownloads": share.max_downloads,
            "downloadCount": share.download_count,
            "createdAt": _iso(share.created_at),
        }

    target = nodes.get(ids[0])
    alive = bool(target and not target.deleted_at)
    return {
        "id": share.id,
        "token": share.token,
        "url": f"/s/{share.token}",
        "nodeId": share.node_id,
        "nodeIds": ids,
        "itemCount": 1,
        "name": target.name if target else "（已删除）",
        "type": target.type if target else "file",
        "size": target.size if target else 0,
        "icon": ns.icon_for(target) if alive else "i-file",
        "previewKind": ns.preview_kind(target) if alive else "none",
        "hasCode": bool(share.extract_code),
        "status": status_of(db, share, target),
        "expiresAt": _iso(share.expires_at),
        "maxDownloads": share.max_downloads,
        "downloadCount": share.download_count,
        "createdAt": _iso(share.created_at),
    }


def list_for_user(db: Session, user_id: int, limit: int = 200) -> list[dict]:
    shares = db.scalars(
        select(Share)
        .where(Share.user_id == user_id)
        .order_by(Share.created_at.desc())
        .limit(limit)
    ).all()
    if not shares:
        return []
    # 一次批量取节点，避免远程库逐条往返（每条 0.5s 会拖到几十秒）
    ids: set[int] = set()
    for s in shares:
        ids.update(root_ids(s))
    nodes = {n.id: n for n in db.scalars(select(Node).where(Node.id.in_(ids))).all()}
    return [share_payload(db, s, nodes.get(s.node_id), nodes) for s in shares]


# --------------------------------------------------------------------------- #
# 公开访问
# --------------------------------------------------------------------------- #
def load_share(db: Session, token: str) -> Share:
    share = db.scalars(select(Share).where(Share.token == token)).first()
    if not share:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分享不存在，链接可能有误")
    if share.revoked:
        raise HTTPException(status.HTTP_410_GONE, "该分享已被取消")
    if share.expires_at and share.expires_at <= utcnow():
        raise HTTPException(status.HTTP_410_GONE, "该分享已过期")
    if share.max_downloads is not None and share.download_count >= share.max_downloads:
        raise HTTPException(status.HTTP_410_GONE, "该分享的下载次数已用完")
    return share


def within_share(db: Session, share: Share, node: Node) -> bool:
    """节点是否落在任一被分享的子树内（防止越权访问同用户其它文件）。"""
    roots = set(root_ids(share))
    current: Node | None = node
    for _ in range(512):
        if current is None:
            return False
        if current.id in roots:
            return True
        if current.parent_id is None:
            return False
        current = db.get(Node, current.parent_id)
    return False


def node_of(db: Session, share: Share, node_id: int | None = None) -> Node:
    roots = root_ids(share)
    target_id = roots[0] if node_id is None else node_id
    node = db.get(Node, target_id)
    if not node or node.user_id != share.user_id or node.deleted_at:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在或已被删除")
    if node.id not in roots and not within_share(db, share, node):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在或已被删除")
    if node.type == "file" and node.blob_sha256 and is_blocked(db, node.blob_sha256):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该文件已被举报确认，禁止分享")
    return node


def root_node(db: Session, share: Share) -> Node:
    """分享的代表节点：合集里首项被删/被拉黑时顺延到下一项。"""
    for rid in root_ids(share):
        node = db.get(Node, rid)
        if not node or node.user_id != share.user_id or node.deleted_at:
            continue
        if node.type == "file" and node.blob_sha256 and is_blocked(db, node.blob_sha256):
            continue
        return node
    raise HTTPException(status.HTTP_404_NOT_FOUND, "分享的内容已被删除")


# --------------------------------------------------------------------------- #
# 哈希黑名单（举报审核通过后生效）
# --------------------------------------------------------------------------- #
def is_blocked(db: Session, sha256: str) -> bool:
    return db.get(BlockedBlob, sha256) is not None


def blocked_in_subtree(db: Session, node: Node) -> list[str]:
    """返回节点子树里被拉黑的 blob 哈希（文件夹分享创建前检查用）。"""
    if node.type == "file":
        return [node.blob_sha256] if node.blob_sha256 and is_blocked(db, node.blob_sha256) else []
    ids = ns.subtree_ids(db, node.user_id, node.id)
    if not ids:
        return []
    tree_shas = set(
        db.scalars(
            select(Node.blob_sha256).where(
                Node.id.in_(ids), Node.blob_sha256.is_not(None)
            )
        ).all()
    )
    if not tree_shas:
        return []
    blocked = set(db.scalars(select(BlockedBlob.sha256)).all())
    return sorted(tree_shas & blocked)


def assert_shareable(db: Session, node: Node) -> None:
    """创建分享前的黑名单拦截（含文件夹子树）。"""
    if blocked_in_subtree(db, node):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "包含已被举报确认的文件，禁止分享")


def filter_blocked(db: Session, items: list[dict]) -> list[dict]:
    """文件夹公开列表里剔除被拉黑的文件。"""
    file_ids = [int(i["id"]) for i in items if i.get("type") == "file"]
    if not file_ids:
        return items
    shas = {
        sha
        for sha in db.scalars(
            select(Node.blob_sha256).where(Node.id.in_(file_ids), Node.blob_sha256.is_not(None))
        ).all()
        if sha
    }
    if not shas:
        return items
    blocked = set(
        db.scalars(select(BlockedBlob.sha256).where(BlockedBlob.sha256.in_(shas))).all()
    )
    if not blocked:
        return items
    blocked_nodes = set(
        db.scalars(
            select(Node.id).where(Node.id.in_(file_ids), Node.blob_sha256.in_(blocked))
        ).all()
    )
    return [i for i in items if int(i["id"]) not in blocked_nodes]


# --------------------------------------------------------------------------- #
# 举报限流
# --------------------------------------------------------------------------- #
def report_guard(ip: str, token: str) -> None:
    now = time.time()
    key = f"{ip}|{token}"
    q = _report_failures[key]
    while q and now - q[0] > _REPORT_WINDOW:
        q.popleft()
    g = _report_global
    while g and now - g[0] > _REPORT_GLOBAL_WINDOW:
        g.popleft()
    if len(q) >= _REPORT_LIMIT or len(g) >= _REPORT_GLOBAL_LIMIT:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "举报太频繁，请稍后再试")


def report_hit(ip: str, token: str) -> None:
    now = time.time()
    _report_failures[f"{ip}|{token}"].append(now)
    _report_global.append(now)


def _has_access(request: Request, share: Share) -> bool:
    if not share.extract_code:
        return True
    raw = request.cookies.get(cookie_name(share.token))
    return bool(raw and verify_resource("share", share.id, raw, share.token))


def _set_access_cookie(response: Response, share: Share) -> None:
    value = sign_resource("share", share.id, ttl=6 * 3600, extra=share.token)
    response.set_cookie(
        key=cookie_name(share.token),
        value=value,
        max_age=6 * 3600,
        httponly=True,
        samesite="lax",
        path=f"/api/public/shares/{share.token}",
    )


def set_access_cookie(response: Response, share: Share) -> None:
    """外部入口：用提取码换来的授权写到响应上。"""
    _set_access_cookie(response, share)


def _guard(key: str) -> None:
    now = time.time()
    q = _unlock_failures[key]
    while q and now - q[0] > _UNLOCK_WINDOW:
        q.popleft()
    if len(q) >= _UNLOCK_LIMIT:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "提取码错误次数过多，请 10 分钟后再试")


def check_access(request: Request, share: Share, code: str | None = None) -> bool:
    """校验访问权：免密直接过；有提取码则认 Cookie 或本次传入的 code。

    返回 True 表示本次是「用 code 换来的新授权」，调用方应调用 set_access_cookie 落 Cookie。
    """
    if _has_access(request, share):
        return False
    key = f"{client_ip(request)}|{share.id}"
    _guard(key)
    if code and share.extract_code and verify_password(normalize_code(code), share.extract_code):
        _unlock_failures.pop(key, None)
        return True
    _unlock_failures[key].append(time.time())
    if not code:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "需要提取码")
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "提取码不正确")


def public_payload(request: Request, share: Share, node: Node, db: Session) -> dict:
    ids = root_ids(share)
    base = {
        "token": share.token,
        "hasCode": bool(share.extract_code),
        "unlocked": _has_access(request, share),
        "expiresAt": _iso(share.expires_at),
        "maxDownloads": share.max_downloads,
        "downloadCount": share.download_count,
        "createdAt": _iso(share.created_at),
        "author": author_payload(db, share),
        "remainingDownloads": (
            None if share.max_downloads is None else max(0, share.max_downloads - share.download_count)
        ),
    }

    if len(ids) == 1:
        base.update(
            {
                "name": node.name,
                "type": node.type,
                "size": node.size,
                "mime": node.mime,
                "icon": ns.icon_for(node),
                "previewKind": ns.preview_kind(node),
                "itemCount": 1,
                "items": None,
            }
        )
        return base

    # 合集分享：一次取全部根节点，按创建时选择的顺序排列
    nodes = db.scalars(
        select(Node).where(Node.id.in_(ids), Node.deleted_at.is_(None))
    ).all()
    order = {rid: idx for idx, rid in enumerate(ids)}
    nodes.sort(key=lambda n: order.get(n.id, len(order)))
    folder_ids = [n.id for n in nodes if n.type == "folder"]
    counts: dict[int, int] = {}
    if folder_ids:
        for pid, cnt in db.execute(
            select(Node.parent_id, func.count())
            .where(Node.parent_id.in_(folder_ids), Node.deleted_at.is_(None))
            .group_by(Node.parent_id)
        ).all():
            counts[int(pid)] = int(cnt)

    items = [
        ns.node_payload(n, counts.get(n.id) if n.type == "folder" else None) for n in nodes
    ]
    items = filter_blocked(db, items)
    base.update(
        {
            "name": f"{len(items)} 个项目",
            "type": "multi",
            "size": sum(i["size"] for i in items if i["type"] == "file"),
            "mime": None,
            "icon": "i-folder",
            "previewKind": "none",
            "itemCount": len(items),
            "items": items,
        }
    )
    return base
