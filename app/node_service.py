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
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".m3u8", ".mpd"}
AUDIO_EXT = {
    ".mp3", ".flac", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".opus",
    ".wma", ".ape", ".aiff", ".aif", ".dsf", ".wv",
}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".bz2", ".tgz"}
DOC_EXT = {".doc", ".docx", ".odt", ".rtf", ".pdf", ".pages"}
SHEET_EXT = {".xls", ".xlsx", ".csv", ".tsv", ".ods", ".numbers"}
SLIDE_EXT = {".ppt", ".pptx", ".pps", ".ppsx", ".odp", ".key"}
TEXT_EXT = {".txt", ".md", ".markdown", ".log", ".srt", ".vtt", ".ass", ".sub", ".nfo"}
CODE_EXT = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".html", ".css", ".scss", ".vue",
    ".java", ".c", ".h", ".cpp", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".sql",
    ".yml", ".yaml", ".toml", ".ini", ".md", ".txt", ".log", ".xml",
    # 字幕与伴生资源也要能取到原始文本（播放器插件直接 fetch）
    ".vtt", ".srt", ".ass",
}

# 能在线预览的 Office 格式：
# - OOXML（docx/xlsx/pptx）：前端用 docx-preview / SheetJS / 自带解析器直接读 zip
# - ODF（odt/ods/odp）：同样是 zip，读 content.xml 就能还原
# - .doc/.ppt 是老的 OLE2 二进制，前端只能做文本级提取（保留文字、丢格式）
# - .rtf 是纯文本控制的富文本，自己写个小解析器足够
PREVIEW_DOCX_EXT = {".docx", ".docm"}
PREVIEW_SHEET_EXT = {".xlsx", ".xlsm", ".xls", ".csv", ".tsv"}
# 放映（.ppsx）和模板（.potx）跟 .pptx 是同一套 OOXML 结构，只差 presentation.xml
# 里的 show 标记，解析器照常读 ppt/presentation.xml + slideN.xml 即可。
PREVIEW_SLIDE_EXT = {".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm"}
PREVIEW_MD_EXT = {".md", ".markdown"}
PREVIEW_ODOC_EXT = {".odt", ".ott", ".fodt"}
PREVIEW_OSHEET_EXT = {".ods", ".ots"}
PREVIEW_OSLIDE_EXT = {".odp", ".otp"}
PREVIEW_RTF_EXT = {".rtf"}
PREVIEW_LEGACY_DOC_EXT = {".doc"}
PREVIEW_LEGACY_PPT_EXT = {".ppt", ".pps", ".pot"}


def extension(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def icon_for(node: Node) -> str:
    """列表/网格里显示的文件类型图标（对应 index.html icon-sprite 里的 symbol id）。

    先按扩展名判定，认不出再按 MIME 兜底（无扩展名的文件也能有个像样的图标）。
    """
    if node.type == "folder":
        return "i-folder"
    ext = extension(node.name)
    if ext in IMAGE_EXT:
        return "i-image"
    if ext in VIDEO_EXT:
        return "i-video"
    if ext in AUDIO_EXT:
        return "i-music"
    if ext == ".pdf" or (node.mime or "").lower() == "application/pdf":
        return "i-pdf"
    if ext in DOC_EXT:
        return "i-doc"
    if ext in SHEET_EXT:
        return "i-sheet"
    if ext in SLIDE_EXT:
        return "i-slide"
    if ext in ARCHIVE_EXT:
        return "i-archive"
    if ext in TEXT_EXT:
        return "i-text"
    if ext in CODE_EXT:
        return "i-code"

    mime = (node.mime or "").lower()
    if mime.startswith("image/"):
        return "i-image"
    if mime.startswith("video/"):
        return "i-video"
    if mime.startswith("audio/"):
        return "i-music"
    if mime == "application/pdf":
        return "i-pdf"
    if mime.startswith("text/"):
        return "i-text"
    return "i-file"


def preview_mime(kind: str, mime: str | None) -> str | None:
    """预览下发的 Content-Type。

    Office 文档统一给 application/octet-stream：前端只读字节自己解析，
    而且 Chrome 遇到 application/vnd.ms-powerpoint 这类插件 MIME 时，
    会把 fetch 的响应直接换成 204 空响应（实测），文件就读不到了。
    """
    if kind in {
        "docx", "doc", "odoc", "rtf",
        "sheet", "osheet",
        "slide", "ppt", "oslide",
    }:
        return "application/octet-stream"
    return mime


# 文档类的在线预览上限：这些格式要把整个文件读进内存再解析，体积一大浏览器就卡死，
# 不如干脆让用户下载。图片/音视频/PDF 是流式或浏览器原生渲染，不受此限。
PREVIEW_MAX_BYTES = 50 * 1024 * 1024
PREVIEW_SIZE_LIMITED = {
    "docx", "doc", "odoc", "rtf",
    "sheet", "osheet",
    "slide", "ppt", "oslide",
    "markdown", "text",
}


def preview_kind(node: Node, *, is_admin: bool = False) -> str:
    """前端预览器类型。

    取值：image / video / audio / pdf / markdown / text /
          docx / sheet / slide（OOXML）/ odoc / osheet / oslide（OpenDocument）/
          rtf / doc / ppt（旧二进制，文本级提取）/
          toolarge（文档类超 50 MB，只提示下载）/ none。

    管理员不受 50 MB 体积限制（admin 预览大文件不走 toolarge 分支）。
    HTML、SVG 一律按文本处理，不给它们在同源下当文档执行的机会。
    所有 Office 解析都在浏览器里做，服务端只把二进制流出去。
    """
    kind = _preview_kind_raw(node)
    if kind in PREVIEW_SIZE_LIMITED and (node.size or 0) > PREVIEW_MAX_BYTES and not is_admin:
        return "toolarge"
    return kind


def _preview_kind_raw(node: Node) -> str:
    if node.type != "file" or not node.blob_sha256:
        return "none"
    ext = extension(node.name)
    mime = (node.mime or "").lower()

    if ext == ".pdf":
        return "pdf"
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in PREVIEW_MD_EXT:
        return "markdown"
    if ext in PREVIEW_DOCX_EXT:
        return "docx"
    if ext in PREVIEW_SHEET_EXT:
        return "sheet"
    if ext in PREVIEW_SLIDE_EXT:
        return "slide"
    if ext in PREVIEW_ODOC_EXT:
        return "odoc"
    if ext in PREVIEW_OSHEET_EXT:
        return "osheet"
    if ext in PREVIEW_OSLIDE_EXT:
        return "oslide"
    if ext in PREVIEW_RTF_EXT:
        return "rtf"
    if ext in PREVIEW_LEGACY_DOC_EXT:
        return "doc"
    if ext in PREVIEW_LEGACY_PPT_EXT:
        return "ppt"
    if ext in CODE_EXT or mime.startswith("text/"):
        return "text"
    return "none"


def node_payload(node: Node, child_count: int | None = None, *, is_admin: bool = False) -> dict:
    return {
        "id": node.id,
        "type": node.type,
        "name": node.name,
        "size": node.size,
        "mime": node.mime,
        "icon": icon_for(node),
        "ext": extension(node.name),
        "previewKind": preview_kind(node, is_admin=is_admin),
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
    # 快传专用文件只能通过取件码访问，不能按 id 当普通文件操作
    if node.quick_owned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在或无权访问")
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
    *,
    is_admin: bool = False,
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
            # 快传文件不属于「我的文件」，只在快传里出现
            Node.quick_owned.is_(False),
        )
        # 文件夹永远排在文件前面（'folder' > 'file'，倒序即文件夹在前）
        .order_by(Node.type.desc(), ordering)
    ).all()

    counts = child_counts(db, user_id, [n.id for n in rows if n.type == "folder"])
    return [node_payload(n, counts.get(n.id), is_admin=is_admin) for n in rows]


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


def search(db: Session, user_id: int, keyword: str, limit: int = 200, *, is_admin: bool = False) -> list[dict]:
    text = (keyword or "").strip()
    if not text:
        return []
    pattern = f"%{text.lower()}%"
    rows = db.scalars(
        select(Node)
        .where(
            Node.user_id == user_id,
            Node.deleted_at.is_(None),
            Node.quick_owned.is_(False),
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
        payload = node_payload(node, counts.get(node.id), is_admin=is_admin)
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
        # 快传文件不在「我的文件」里，不参与同名判断
        Node.quick_owned.is_(False),
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


def copy_subtree(
    db: Session,
    user: User,
    node: Node,
    parent_id: int | None,
    *,
    _depth: int = 0,
) -> tuple[Node, int]:
    """把节点（含子树）复制为 user 名下 parent_id 的副本。

    文件复用同一 blob（retain_blob），不复制物理数据。
    返回 (新根节点, 新增逻辑字节数)。
    """
    if _depth > 64:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "目录层级过深，无法转存")
    require_parent(db, user.id, parent_id)
    if node.type == "folder":
        clone = Node(
            user_id=user.id,
            parent_id=parent_id,
            type="folder",
            name=unique_name(db, user.id, parent_id, node.name),
            size=0,
        )
        db.add(clone)
        db.flush()
        added = 0
        children = db.scalars(
            select(Node).where(
                Node.user_id == node.user_id,
                Node.parent_id == node.id,
                Node.deleted_at.is_(None),
            )
        ).all()
        for child in children:
            _, child_added = copy_subtree(
                db, user, child, clone.id, _depth=_depth + 1
            )
            added += child_added
        return clone, added
    # 文件：blob 引用 +1，名字落到目标目录（重名自动 (2)）
    if node.blob_sha256:
        retain_blob(db, node.blob_sha256, node.size)
    clone = Node(
        user_id=user.id,
        parent_id=parent_id,
        type="file",
        name=unique_name(db, user.id, parent_id, node.name),
        blob_sha256=node.blob_sha256,
        size=node.size,
        mime=node.mime,
    )
    db.add(clone)
    db.flush()
    return clone, int(node.size or 0)


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
    *,
    quick: bool = False,
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
        quick_owned=quick,
    )
    db.add(node)
    db.flush()
    return node


def recalc_usage(db: Session, user: User) -> int:
    """按 node 表重算已用空间（含回收站），避免增量记账漂移。"""
    total = db.scalar(
        select(func.coalesce(func.sum(Node.size), 0)).where(
            Node.user_id == user.id,
            Node.type == "file",
            # 快传文件不占用户配额（过期由后台清理）
            Node.quick_owned.is_(False),
        )
    )
    user.used_bytes = int(total or 0)
    return user.used_bytes


# --------------------------------------------------------------------------- #
# 回收站
# --------------------------------------------------------------------------- #
def trash_list(db: Session, user_id: int, limit: int = 500, *, is_admin: bool = False) -> list[dict]:
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
        payload = node_payload(node, is_admin=is_admin)
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
