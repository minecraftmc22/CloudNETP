"""节点树业务逻辑：目录浏览、重名处理、移动环检测、级联软删、配额统计。

约定：
- 所有查询都带 user_id，天然多租户隔离；
- 文件名只做展示，永不参与磁盘路径（磁盘用的是 sha256）；
- used_bytes 统计包含回收站内的文件（空间确实还被占着）。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .models import Blob, Node, User, utcnow
from .storage import blob_exists, delete_blob

MAX_DEPTH = 64  # 目录层级保护，同时防止异常数据造成死循环

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif", ".ico"}
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v"}
AUDIO_EXT = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".opus"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".bz2"}
CODE_EXT = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".html", ".css", ".scss", ".vue",
    ".java", ".c", ".h", ".cpp", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".sql",
    ".yml", ".yaml", ".toml", ".ini", ".md", ".txt", ".log", ".xml",
}


def extension(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def icon_for(node: Node) -> str:
    if node.type == "folder":
        return "i-folder"
    ext = extension(node.name)
    if ext in IMAGE_EXT:
        return "i-image"
    if ext in VIDEO_EXT or ext in AUDIO_EXT:
        return "i-star"
    if ext in ARCHIVE_EXT:
        return "i-download"
    return "i-file"


def node_payload(node: Node, child_count: int | None = None) -> dict:
    return {
        "id": node.id,
        "type": node.type,
        "name": node.name,
        "size": node.size,
        "mime": node.mime,
        "icon": icon_for(node),
        "ext": extension(node.name),
        "parentId": node.parent_id,
        "createdAt": node.created_at.isoformat() + "Z" if node.created_at else None,
        "updatedAt": node.updated_at.isoformat() + "Z" if node.updated_at else None,
        "deletedAt": node.deleted_at.isoformat() + "Z" if node.deleted_at else None,
        "childCount": child_count,
    }


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def get_node(db: Session, user_id: int, node_id: int, *, allow_deleted: bool = False) -> Node:
    node = db.get(Node, node_id)
    if not node or node.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在或无权访问")
    if node.deleted_at and not allow_deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "该文件在回收站中")
    return node


def require_parent(db: Session, user_id: int, parent_id: int | None) -> Node | None:
    """校验目标文件夹：必须存在、属于自己、是文件夹、不在回收站。"""
    if parent_id is None:
        return None
    parent = get_node(db, user_id, parent_id)
    if parent.type != "folder":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "目标不是文件夹")
    return parent


def breadcrumb(db: Session, user_id: int, parent_id: int | None) -> list[dict]:
    """从根到当前目录的路径，第一项固定是根。"""
    trail: list[dict] = []
    current = parent_id
    guard = 0
    while current is not None and guard < MAX_DEPTH:
        guard += 1
        node = db.get(Node, current)
        if not node or node.user_id != user_id:
            break
        trail.append({"id": node.id, "name": node.name})
        current = node.parent_id
    trail.reverse()
    return [{"id": None, "name": "我的文件"}, *trail]


def list_children(
    db: Session,
    user_id: int,
    parent_id: int | None,
    sort: str = "name",
    order: str = "asc",
) -> list[dict]:
    sort_column = {
        "name": Node.name,
        "size": Node.size,
        "updated": Node.updated_at,
        "created": Node.created_at,
    }.get(sort, Node.name)
    ordering = sort_column.desc() if order == "desc" else sort_column.asc()

    rows = db.scalars(
        select(Node)
        .where(
            Node.user_id == user_id,
            Node.parent_id.is_(None) if parent_id is None else Node.parent_id == parent_id,
            Node.deleted_at.is_(None),
        )
        # 文件夹永远排在文件前面（'folder' > 'file'，倒序即文件夹在前）
        .order_by(Node.type.desc(), ordering)
    ).all()

    counts = child_counts(db, user_id, [n.id for n in rows if n.type == "folder"])
    return [node_payload(n, counts.get(n.id)) for n in rows]


def child_counts(db: Session, user_id: int, folder_ids: Iterable[int]) -> dict[int, int]:
    ids = [i for i in folder_ids]
    if not ids:
        return {}
    rows = db.execute(
        select(Node.parent_id, func.count(Node.id))
        .where(
            Node.user_id == user_id,
            Node.parent_id.in_(ids),
            Node.deleted_at.is_(None),
        )
        .group_by(Node.parent_id)
    ).all()
    return {int(pid): int(count) for pid, count in rows if pid is not None}


def search(db: Session, user_id: int, keyword: str, limit: int = 200) -> list[dict]:
    text = (keyword or "").strip()
    if not text:
        return []
    pattern = f"%{text.lower()}%"
    rows = db.scalars(
        select(Node)
        .where(
            Node.user_id == user_id,
            Node.deleted_at.is_(None),
            func.lower(Node.name).like(pattern),
        )
        .order_by(Node.type.desc(), Node.updated_at.desc())
        .limit(limit)
    ).all()

    # 补上面包屑，搜索结果也能看出文件在哪
    paths: dict[int, str] = {}

    def path_of(node: Node) -> str:
        if node.parent_id is None:
            return "我的文件"
        if node.parent_id in paths:
            return paths[node.parent_id]
        trail = breadcrumb(db, user_id, node.parent_id)
        label = " / ".join(part["name"] for part in trail)
        paths[node.parent_id] = label
        return label

    counts = child_counts(db, user_id, [n.id for n in rows if n.type == "folder"])
    items = []
    for node in rows:
        payload = node_payload(node, counts.get(node.id))
        payload["path"] = path_of(node)
        items.append(payload)
    return items


# --------------------------------------------------------------------------- #
# 名称
# --------------------------------------------------------------------------- #
def name_conflict(
    db: Session, user_id: int, parent_id: int | None, name: str, exclude_id: int | None = None
) -> bool:
    stmt = select(Node.id).where(
        Node.user_id == user_id,
        Node.parent_id.is_(None) if parent_id is None else Node.parent_id == parent_id,
        Node.deleted_at.is_(None),
        func.lower(Node.name) == name.lower(),
    )
    if exclude_id is not None:
        stmt = stmt.where(Node.id != exclude_id)
    return db.scalar(stmt.limit(1)) is not None


def unique_name(
    db: Session, user_id: int, parent_id: int | None, name: str, exclude_id: int | None = None
) -> str:
    """同名时自动加 (2) (3)…，和 Windows 资源管理器行为一致。"""
    if not name_conflict(db, user_id, parent_id, name, exclude_id):
        return name
    stem, dot, suffix = name.rpartition(".")
    if not dot or len(stem) < 1:
        stem, suffix = name, ""
    index = 2
    while index < 1000:
        candidate = f"{stem} ({index})" + (f".{suffix}" if suffix else "")
        if not name_conflict(db, user_id, parent_id, candidate, exclude_id):
            return candidate
        index += 1
    raise HTTPException(status.HTTP_409_CONFLICT, "同名文件过多，请换个名字")


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def create_folder(db: Session, user: User, parent_id: int | None, name: str) -> Node:
    require_parent(db, user.id, parent_id)
    node = Node(
        user_id=user.id,
        parent_id=parent_id,
        type="folder",
        name=unique_name(db, user.id, parent_id, name),
        size=0,
    )
    db.add(node)
    db.flush()
    return node


def rename_node(db: Session, user: User, node: Node, name: str) -> Node:
    if node.name == name:
        return node
    node.name = unique_name(db, user.id, node.parent_id, name, exclude_id=node.id)
    node.updated_at = utcnow()
    return node


def move_node(db: Session, user: User, node: Node, target_parent: int | None) -> Node:
    if target_parent == node.parent_id:
        return node
    if node.type == "folder" and target_parent is not None:
        if target_parent == node.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能移动到自身内部")
        # 目标不能是自己的子孙，否则会形成环
        if node.id in ancestor_ids(db, user.id, target_parent):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能移动到自己的子文件夹里")
    require_parent(db, user.id, target_parent)
    node.parent_id = target_parent
    node.name = unique_name(db, user.id, target_parent, node.name, exclude_id=node.id)
    node.updated_at = utcnow()
    return node


def ancestor_ids(db: Session, user_id: int, node_id: int | None) -> set[int]:
    """自身 + 所有上级 id。"""
    result: set[int] = set()
    current = node_id
    guard = 0
    while current is not None and guard < MAX_DEPTH:
        guard += 1
        result.add(current)
        parent = db.scalar(select(Node.parent_id).where(Node.id == current, Node.user_id == user_id))
        current = int(parent) if parent is not None else None
    return result


def subtree_ids(db: Session, user_id: int, root_id: int) -> list[int]:
    """广度优先收集整棵子树（含自身）。"""
    collected = [root_id]
    frontier = [root_id]
    guard = 0
    while frontier and guard < MAX_DEPTH:
        guard += 1
        children = db.scalars(
            select(Node.id).where(
                Node.user_id == user_id,
                Node.parent_id.in_(frontier),
                Node.deleted_at.is_(None),
            )
        ).all()
        frontier = [int(i) for i in children]
        collected.extend(frontier)
    return collected


def soft_delete(db: Session, user: User, node: Node) -> int:
    ids = subtree_ids(db, user.id, node.id) if node.type == "folder" else [node.id]
    now = utcnow()
    db.execute(
        update(Node)
        .where(Node.id.in_(ids))
        .values(deleted_at=now, restore_parent_id=node.parent_id, updated_at=now)
    )
    return len(ids)


def restore_node(db: Session, user: User, node: Node) -> None:
    """还原整棵子树；原父目录若不在了就回到根目录。"""
    target = node.restore_parent_id
    if target is not None:
        parent = db.get(Node, target)
        if not parent or parent.user_id != user.id or parent.deleted_at or parent.type != "folder":
            target = None
    ids = subtree_ids(db, user.id, node.id)  # 回收站内的子节点也一起找出来
    if node.type == "folder":
        deleted_children = db.scalars(
            select(Node.id).where(
                Node.user_id == user.id,
                Node.parent_id.in_([node.id]),
            )
        ).all()
        ids = list({*ids, *[int(i) for i in deleted_children]})
    node.parent_id = target
    node.name = unique_name(db, user.id, target, node.name, exclude_id=node.id)
    node.deleted_at = None
    node.restore_parent_id = None
    node.updated_at = utcnow()
    for child_id in ids:
        if child_id == node.id:
            continue
        child = db.get(Node, child_id)
        if child and child.deleted_at:
            child.deleted_at = None
            child.restore_parent_id = None


def purge_node(db: Session, user: User, node: Node) -> int:
    """彻底删除：子节点行一起删，并递减 blob 引用计数。"""
    ids = subtree_ids(db, user.id, node.id)
    if node.type == "folder":
        rows = db.scalars(select(Node.id).where(Node.user_id == user.id, Node.parent_id.in_(ids))).all()
        ids = list({*ids, *[int(i) for i in rows]})
    blobs = db.scalars(
        select(Node.blob_sha256).where(Node.id.in_(ids), Node.blob_sha256.is_not(None))
    ).all()
    db.execute(Node.__table__.delete().where(Node.id.in_(ids)))
    for sha in {b for b in blobs if b}:
        release_blob(db, sha)
    return len(ids)


def release_blob(db: Session, sha256: str) -> None:
    blob = db.get(Blob, sha256)
    if not blob:
        return
    blob.ref_count = max(0, blob.ref_count - 1)
    if blob.ref_count == 0:
        db.delete(blob)
        # 引用归零就清盘，避免"DB 没了但文件还在"的垃圾堆积
        delete_blob(sha256)


def retain_blob(db: Session, sha256: str, size: int) -> Blob:
    blob = db.get(Blob, sha256)
    if blob is None:
        blob = Blob(sha256=sha256, size=size, ref_count=1)
        db.add(blob)
        return blob
    blob.ref_count += 1
    return blob


def create_file(
    db: Session,
    user: User,
    parent_id: int | None,
    name: str,
    sha256: str,
    size: int,
    mime: str | None,
) -> Node:
    require_parent(db, user.id, parent_id)
    retain_blob(db, sha256, size)
    node = Node(
        user_id=user.id,
        parent_id=parent_id,
        type="file",
        name=unique_name(db, user.id, parent_id, name),
        blob_sha256=sha256,
        size=size,
        mime=mime,
    )
    db.add(node)
    db.flush()
    return node


def recalc_usage(db: Session, user: User) -> int:
    """按 node 表重算已用空间（含回收站），避免增量记账漂移。"""
    total = db.scalar(
        select(func.coalesce(func.sum(Node.size), 0)).where(Node.user_id == user.id, Node.type == "file")
    )
    user.used_bytes = int(total or 0)
    return user.used_bytes


# --------------------------------------------------------------------------- #
# 回收站
# --------------------------------------------------------------------------- #
def trash_list(db: Session, user_id: int, limit: int = 500) -> list[dict]:
    """只展示回收站里的“顶层”条目：父节点没被删的那些。"""
    rows = db.scalars(
        select(Node)
        .where(Node.user_id == user_id, Node.deleted_at.is_not(None))
        .order_by(Node.deleted_at.desc())
        .limit(limit)
    ).all()
    deleted_ids = {n.id for n in rows}
    items = []
    for node in rows:
        if node.parent_id is not None and node.parent_id in deleted_ids:
            continue
        payload = node_payload(node)
        payload["deletedAt"] = node.deleted_at.isoformat() + "Z" if node.deleted_at else None
        payload["originalParentId"] = node.restore_parent_id
        items.append(payload)
    return items


def purge_expired(db: Session, days: int) -> int:
    """清理超过保留期的回收站条目。"""
    if days <= 0:
        return 0
    from datetime import timedelta

    cutoff = datetime.now() - timedelta(days=days)
    rows = db.scalars(
        select(Node).where(Node.deleted_at.is_not(None), Node.deleted_at < cutoff).limit(500)
    ).all()
    removed = 0
    for node in rows:
        user = db.get(User, node.user_id)
        if not user:
            continue
        removed += purge_node(db, user, node)
    return removed


def blob_ready(sha256: str, size: int) -> bool:
    return blob_exists(sha256, size)
