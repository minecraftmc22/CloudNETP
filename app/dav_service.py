"""WebDAV 服务层：路径解析、PROPFIND 响应拼装、内存锁、子树复制。

设计取舍：
- 路径只是 nodes 树上的逻辑映射，文件名永不参与磁盘路径（磁盘按 sha256 寻址）；
- LOCK 采用「会话内假锁」：只完成协议握手（Windows 资源管理器必须有），
  不强制互斥，避免客户端异常退出后把文件永久锁死；
- 名称在 WebDAV 侧严格按客户端给的名字落库（不做 (2)(3) 改名），
  否则「另存为 / 覆盖」这类语义会变得不可预测。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from email.utils import formatdate
from urllib.parse import quote, unquote
from xml.sax.saxutils import escape

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import node_service as ns
from .models import Node, User, utcnow

MAX_DEPTH = 8          # 递归深度保护（PROPFIND Depth: infinity 容易被滥用）
MAX_ENTRIES = 5000     # 单次 multistatus 的条目上限
LOCK_TTL = 3600        # 假锁有效期（秒）


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def split_path(path: str) -> list[str]:
    """把 URL 路径拆成未编码的层级名。

    必须在拆分之后逐段解码：整体解码会把 %2F 变成真分隔符，产生越权路径。
    """
    segments: list[str] = []
    for part in (path or "").split("/"):
        if not part:
            continue
        name = unquote(part, encoding="utf-8", errors="replace")
        if name in ("", "."):
            continue
        if name == "..":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "路径不能包含 ..")
        if len(name) > 255:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "路径层级名称过长")
        segments.append(name)
    if len(segments) > MAX_DEPTH:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "目录层级过深")
    return segments


def href_for(base: str, segments: list[str], is_dir: bool) -> str:
    """生成给客户端回填的 href（百分号编码，目录带尾斜杠）。"""
    path = base.rstrip("/") + "/" + "/".join(quote(seg, safe="") for seg in segments)
    if is_dir and not path.endswith("/"):
        path += "/"
    return path


def child_by_name(db: Session, user_id: int, parent_id: int | None, name: str) -> Node | None:
    stmt = select(Node).where(
        Node.user_id == user_id,
        Node.deleted_at.is_(None),
        func.lower(Node.name) == name.lower(),
    )
    if parent_id is None:
        stmt = stmt.where(Node.parent_id.is_(None))
    else:
        stmt = stmt.where(Node.parent_id == parent_id)
    return db.scalars(stmt.limit(1)).first()


def resolve(
    db: Session, user_id: int, segments: list[str]
) -> tuple[Node | None, Node | None, str | None]:
    """解析路径 → (父目录, 目标节点, 末段名)。

    根目录返回 (None, None, None)；中间层级缺失或不是目录时抛 404/409。
    """
    if not segments:
        return None, None, None
    parent: Node | None = None
    for index, name in enumerate(segments):
        node = child_by_name(db, user_id, parent.id if parent else None, name)
        if index == len(segments) - 1:
            return parent, node, name
        if node is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"路径不存在：{'/'.join(segments[: index + 1])}")
        if node.type != "folder":
            raise HTTPException(status.HTTP_409_CONFLICT, "路径中间层级不是文件夹")
        parent = node
    return parent, None, None


def require_collection(db: Session, user_id: int, segments: list[str]) -> Node | None:
    """解析一个已存在的目录（根目录返回 None）。"""
    parent, node, _ = resolve(db, user_id, segments)
    if not segments:
        return None
    if node is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "目标目录不存在")
    if node.type != "folder":
        raise HTTPException(status.HTTP_405_METHOD_NOT_ALLOWED, "目标不是文件夹")
    return node


def children(db: Session, user_id: int, parent_id: int | None) -> list[Node]:
    stmt = select(Node).where(Node.user_id == user_id, Node.deleted_at.is_(None))
    if parent_id is None:
        stmt = stmt.where(Node.parent_id.is_(None))
    else:
        stmt = stmt.where(Node.parent_id == parent_id)
    return list(db.scalars(stmt.order_by(Node.type.desc(), func.lower(Node.name))).all())


# --------------------------------------------------------------------------- #
# XML 属性
# --------------------------------------------------------------------------- #
def http_date(value: datetime | None) -> str:
    stamp = (value or utcnow()).replace(tzinfo=timezone.utc).timestamp()
    return formatdate(stamp, usegmt=True)


def iso_date(value: datetime | None) -> str:
    return (value or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def etag(node: Node | None) -> str:
    if node is None:
        return '"root"'
    if node.type == "folder":
        return f'"d{node.id}-{int(node.updated_at.timestamp())}"'
    return f'"{(node.blob_sha256 or "none")[:20]}-{node.size}"'


SUPPORTED_LOCK = (
    "<D:supportedlock>"
    "<D:lockentry><D:lockscope><D:exclusive/></D:lockscope><D:locktype><D:write/></D:locktype></D:lockentry>"
    "<D:lockentry><D:lockscope><D:shared/></D:lockscope><D:locktype><D:write/></D:locktype></D:lockentry>"
    "</D:supportedlock>"
)


def prop_xml(node: Node | None, quota: tuple[int, int] | None = None) -> str:
    """单个资源的全部属性。客户端只认自己需要的，多余的会被忽略。"""
    if node is None:
        props = [
            "<D:displayname>我的文件</D:displayname>",
            "<D:resourcetype><D:collection/></D:resourcetype>",
            f"<D:creationdate>{iso_date(None)}</D:creationdate>",
            f"<D:getlastmodified>{http_date(None)}</D:getlastmodified>",
            f"<D:getetag>{etag(None)}</D:getetag>",
        ]
    else:
        props = [
            f"<D:displayname>{escape(node.name)}</D:displayname>",
            f"<D:creationdate>{iso_date(node.created_at)}</D:creationdate>",
            f"<D:getlastmodified>{http_date(node.updated_at)}</D:getlastmodified>",
            f"<D:getetag>{etag(node)}</D:getetag>",
        ]
        if node.type == "folder":
            props.append("<D:resourcetype><D:collection/></D:resourcetype>")
            props.append("<D:getcontentlength>0</D:getcontentlength>")
        else:
            props.append("<D:resourcetype/>")
            props.append(f"<D:getcontentlength>{int(node.size or 0)}</D:getcontentlength>")
            props.append(
                f"<D:getcontenttype>{escape(node.mime or 'application/octet-stream')}</D:getcontenttype>"
            )
    props.append(SUPPORTED_LOCK)
    props.append("<D:lockdiscovery/>")
    if quota is not None:
        props.append(f"<D:quota-available-bytes>{max(0, quota[0])}</D:quota-available-bytes>")
        props.append(f"<D:quota-used-bytes>{max(0, quota[1])}</D:quota-used-bytes>")
    return "".join(props)


def response_xml(base: str, segments: list[str], node: Node | None, quota=None) -> str:
    is_dir = node is None or node.type == "folder"
    href = href_for(base, segments, is_dir)
    return (
        "<D:response>"
        f"<D:href>{escape(href)}</D:href>"
        f"<D:propstat><D:prop>{prop_xml(node, quota)}</D:prop>"
        "<D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
        "</D:response>"
    )


def multistatus(entries: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:multistatus xmlns:D="DAV:">' + "".join(entries) + "</D:multistatus>"
    )


# --------------------------------------------------------------------------- #
# 遍历
# --------------------------------------------------------------------------- #
def walk(
    db: Session,
    user_id: int,
    node: Node | None,
    segments: list[str],
    depth: int,
    quota: tuple[int, int] | None,
    base: str = "/dav",
) -> list[str]:
    """按 Depth 展开一个资源，返回已拼好的 <D:response> 列表。"""
    base_entries = [response_xml(base, segments, node, quota)]
    if depth <= 0:
        return base_entries
    if node is not None and node.type != "folder":
        return base_entries

    limit = MAX_ENTRIES
    frontier: list[tuple[Node | None, list[str], int]] = [(node, segments, depth)]
    while frontier and len(base_entries) < limit:
        current, current_segments, remaining = frontier.pop(0)
        if remaining <= 0:
            continue
        for child in children(db, user_id, current.id if current else None):
            child_segments = [*current_segments, child.name]
            base_entries.append(response_xml(base, child_segments, child))
            if child.type == "folder" and remaining > 1:
                frontier.append((child, child_segments, remaining - 1))
            if len(base_entries) >= limit:
                break
    return base_entries


# --------------------------------------------------------------------------- #
# 内存锁
# --------------------------------------------------------------------------- #
_locks: dict[str, tuple[str, float]] = {}


def _lock_key(user_id: int, segments: list[str]) -> str:
    return f"{user_id}:" + "/".join(segments)


def _now() -> float:
    import time

    return time.time()


def acquire_lock(user_id: int, segments: list[str], ttl: int = LOCK_TTL) -> str:
    key = _lock_key(user_id, segments)
    existing = _locks.get(key)
    if existing and existing[1] > _now():
        # 已持有：续期，返回同一个 token（Windows 的 LOCK 刷新走这条）
        token = existing[0]
    else:
        token = f"opaquelocktoken:{uuid.uuid4()}"
    _locks[key] = (token, _now() + ttl)
    return token


def release_lock(user_id: int, segments: list[str], token: str | None) -> bool:
    """释放锁。token 不匹配时返回 False（客户端会当成 409 处理）。"""
    key = _lock_key(user_id, segments)
    existing = _locks.get(key)
    if not existing:
        return True  # 幂等：客户端可能重复 UNLOCK
    if token and existing[1] > _now():
        given = token.strip().strip("<>")
        if given and not given.startswith("opaquelocktoken:") and given != existing[0]:
            return False
    _locks.pop(key, None)
    return True


def purge_expired_locks() -> None:
    now = _now()
    for key in [k for k, v in _locks.items() if v[1] <= now]:
        _locks.pop(key, None)


def lock_xml(token: str, href: str, depth: str, ttl: int = LOCK_TTL) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:prop xmlns:D="DAV:"><D:lockdiscovery><D:activelock>'
        "<D:locktype><D:write/></D:locktype>"
        "<D:lockscope><D:exclusive/></D:lockscope>"
        f"<D:depth>{escape(depth or 'infinity')}</D:depth>"
        "<D:owner/>"
        f"<D:timeout>Second-{ttl}</D:timeout>"
        f"<D:locktoken><D:href>{escape(token)}</D:href></D:locktoken>"
        f"<D:lockroot><D:href>{escape(href)}</D:href></D:lockroot>"
        "</D:activelock></D:lockdiscovery></D:prop>"
    )


# --------------------------------------------------------------------------- #
# 写入辅助
# --------------------------------------------------------------------------- #
_INVALID_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def valid_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "名称不能为空")
    if len(name) > 255:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "名称过长（上限 255 字符）")
    if _INVALID_NAME.search(name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, r'名称不能包含 \ / : * ? " < > | 等字符')
    return name


def quota_of(user: User) -> tuple[int, int]:
    return user.quota_bytes - user.used_bytes, user.used_bytes


def make_file(
    db: Session,
    user: User,
    parent_id: int | None,
    name: str,
    sha256: str,
    size: int,
    mime: str | None,
    *,
    replace: Node | None = None,
) -> Node:
    """PUT 落库：同名覆盖时复用节点行，并把旧 blob 引用还回去。"""
    now = utcnow()
    if replace is None:
        ns.retain_blob(db, sha256, size)
        node = Node(
            user_id=user.id,
            parent_id=parent_id,
            type="file",
            name=name,
            blob_sha256=sha256,
            size=size,
            mime=mime,
            created_at=now,
            updated_at=now,
        )
        db.add(node)
        db.flush()
        return node

    old_sha = replace.blob_sha256
    if old_sha != sha256:
        ns.retain_blob(db, sha256, size)
        replace.blob_sha256 = sha256
        if old_sha:
            ns.release_blob(db, old_sha)
    replace.size = size
    replace.mime = mime
    replace.name = name
    replace.updated_at = now
    return replace


def make_folder(db: Session, user: User, parent_id: int | None, name: str) -> Node:
    now = utcnow()
    node = Node(
        user_id=user.id,
        parent_id=parent_id,
        type="folder",
        name=name,
        size=0,
        created_at=now,
        updated_at=now,
    )
    db.add(node)
    db.flush()
    return node


def copy_subtree(db: Session, user: User, source: Node, dest_parent_id: int | None, dest_name: str) -> Node:
    """递归复制（文件夹含全部子项）；文件走 blob 引用，不复制字节。"""
    if source.type == "file":
        ns.retain_blob(db, source.blob_sha256, source.size)
        now = utcnow()
        clone = Node(
            user_id=user.id,
            parent_id=dest_parent_id,
            type="file",
            name=dest_name,
            blob_sha256=source.blob_sha256,
            size=source.size,
            mime=source.mime,
            created_at=now,
            updated_at=now,
        )
        db.add(clone)
        db.flush()
        return clone

    root = make_folder(db, user, dest_parent_id, dest_name)
    queue: list[tuple[Node, Node]] = [(source, root)]
    guard = 0
    while queue and guard < MAX_ENTRIES:
        src_dir, dst_dir = queue.pop(0)
        for child in children(db, user.id, src_dir.id):
            guard += 1
            if child.type == "folder":
                new_dir = make_folder(db, user, dst_dir.id, child.name)
                queue.append((child, new_dir))
            else:
                ns.retain_blob(db, child.blob_sha256, child.size)
                now = utcnow()
                db.add(
                    Node(
                        user_id=user.id,
                        parent_id=dst_dir.id,
                        type="file",
                        name=child.name,
                        blob_sha256=child.blob_sha256,
                        size=child.size,
                        mime=child.mime,
                        created_at=now,
                        updated_at=now,
                    )
                )
    return root
