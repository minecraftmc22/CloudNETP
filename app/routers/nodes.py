"""文件与目录路由：浏览、增删改、移动、搜索、回收站、下载、上传。"""

from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import node_service as ns
from .. import storage
from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import Node, User
from ..schemas import FolderIn, MoveIn, RenameIn
from ..security import check_origin, normalize_name
from ..settings_store import get_int
from ..user_service import ensure_settings, log_action

router = APIRouter(prefix="/api", tags=["nodes"])

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


# --------------------------------------------------------------------------- #
# 浏览
# --------------------------------------------------------------------------- #
@router.get("/nodes")
def list_nodes(
    db: DbSession,
    user: CurrentUser,
    parentId: int | None = Query(default=None, alias="parentId"),
    sort: str = Query(default="name", pattern="^(name|size|updated|created)$"),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
) -> dict:
    if parentId is not None:
        ns.require_parent(db, user.id, parentId)
    items = ns.list_children(db, user.id, parentId, sort, order)
    return {
        "parentId": parentId,
        "breadcrumb": ns.breadcrumb(db, user.id, parentId),
        "items": items,
        "total": len(items),
    }


@router.get("/nodes/{node_id}")
def node_detail(node_id: int, db: DbSession, user: CurrentUser) -> dict:
    node = ns.get_node(db, user.id, node_id, allow_deleted=True)
    return {"node": ns.node_payload(node)}


@router.get("/search")
def search_nodes(
    db: DbSession,
    user: CurrentUser,
    q: str = Query(default="", max_length=128),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    items = ns.search(db, user.id, q, limit)
    return {"query": q, "items": items, "total": len(items)}


# --------------------------------------------------------------------------- #
# 目录与重命名
# --------------------------------------------------------------------------- #
@router.post("/nodes/folder", status_code=status.HTTP_201_CREATED)
def create_folder(payload: FolderIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    name = normalize_name(payload.name)
    node = ns.create_folder(db, user, payload.parent_id, name)
    log_action(db, "folder_create", user.id, {"name": node.name, "parentId": payload.parent_id}, request)
    db.commit()
    db.refresh(node)
    return {"node": ns.node_payload(node, 0)}


@router.patch("/nodes/{node_id}")
def patch_node(
    node_id: int, payload: RenameIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    node = ns.get_node(db, user.id, node_id)
    ns.rename_node(db, user, node, normalize_name(payload.name))
    log_action(db, "node_rename", user.id, {"id": node_id, "name": node.name}, request)
    db.commit()
    db.refresh(node)
    return {"node": ns.node_payload(node)}


@router.post("/nodes/{node_id}/move")
def move_node(node_id: int, payload: MoveIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    node = ns.get_node(db, user.id, node_id)
    ns.move_node(db, user, node, payload.parent_id)
    log_action(db, "node_move", user.id, {"id": node_id, "parentId": payload.parent_id}, request)
    db.commit()
    db.refresh(node)
    return {"node": ns.node_payload(node)}


# --------------------------------------------------------------------------- #
# 删除 / 回收站
# --------------------------------------------------------------------------- #
@router.delete("/nodes/{node_id}")
def delete_node(node_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    node = ns.get_node(db, user.id, node_id)

    # 回收站被用户关掉时，删除即彻底删除（用户在设置里已用验证码确认过这个后果）
    if not ensure_settings(db, user.id).trash_enabled:
        removed = ns.purge_node(db, user, node)
        ns.recalc_usage(db, user)
        log_action(db, "node_delete_permanent", user.id, {"id": node_id, "name": node.name}, request)
        db.commit()
        return {"deleted": removed, "purged": True}

    count = ns.soft_delete(db, user, node)
    log_action(db, "node_delete", user.id, {"id": node_id, "name": node.name, "count": count}, request)
    db.commit()
    return {"deleted": count, "purged": False}


@router.post("/nodes/{node_id}/restore")
def restore_node(node_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    node = ns.get_node(db, user.id, node_id, allow_deleted=True)
    if not node.deleted_at:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该文件不在回收站里")
    ns.restore_node(db, user, node)
    log_action(db, "node_restore", user.id, {"id": node_id, "name": node.name}, request)
    db.commit()
    db.refresh(node)
    return {"node": ns.node_payload(node)}


@router.delete("/nodes/{node_id}/purge")
def purge_node(node_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    node = ns.get_node(db, user.id, node_id, allow_deleted=True)
    removed = ns.purge_node(db, user, node)
    ns.recalc_usage(db, user)
    log_action(db, "node_purge", user.id, {"id": node_id, "name": node.name}, request)
    db.commit()
    return {"purged": removed}


@router.get("/trash")
def trash(db: DbSession, user: CurrentUser) -> dict:
    items = ns.trash_list(db, user.id)
    return {"items": items, "total": len(items), "usedBytes": user.used_bytes}


@router.post("/trash/empty")
def empty_trash(request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    items = ns.trash_list(db, user.id)
    removed = 0
    for item in items:
        node = db.get(Node, item["id"])
        if node:
            removed += ns.purge_node(db, user, node)
    ns.recalc_usage(db, user)
    log_action(db, "trash_empty", user.id, {"removed": removed}, request)
    db.commit()
    return {"removed": removed}


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
@router.get("/nodes/{node_id}/download")
def download(node_id: int, request: Request, db: DbSession, user: CurrentUser):
    node = ns.get_node(db, user.id, node_id)
    if node.type != "file" or not node.blob_sha256:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件夹暂不支持直接下载")
    path = storage.blob_path(node.blob_sha256)
    return storage.file_response(path, node.name, node.mime, request)


# --------------------------------------------------------------------------- #
# 在线预览
# --------------------------------------------------------------------------- #
@router.get("/nodes/{node_id}/preview")
def preview(node_id: int, request: Request, db: DbSession, user: CurrentUser):
    """内嵌预览的二进制流（图片/音视频/PDF），支持 Range，浏览器可直接解码。

    文本不走这里，改走 /text 返回 JSON，避免中文编码问题。
    """
    node = ns.get_node(db, user.id, node_id)
    if node.type != "file" or not node.blob_sha256:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件夹不支持在线预览")

    kind = ns.preview_kind(node)
    if kind == "none":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "该类型暂不支持在线预览")

    path = storage.blob_path(node.blob_sha256)
    return storage.file_response(
        path,
        node.name,
        node.mime,
        request,
        inline=True,
        # PDF 交给浏览器内置查看器，sandbox 会让它渲染不出来
        csp="" if kind == "pdf" else None,
    )


_TEXT_PREVIEW_DEFAULT = 1_000_000


@router.get("/nodes/{node_id}/text")
def preview_text(
    node_id: int,
    db: DbSession,
    user: CurrentUser,
    max_bytes: int = Query(default=_TEXT_PREVIEW_DEFAULT, ge=1024, le=4_000_000),
) -> dict:
    node = ns.get_node(db, user.id, node_id)
    if node.type != "file" or not node.blob_sha256:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件夹不支持在线预览")
    if ns.preview_kind(node) != "text":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "该文件不是文本类型")

    try:
        return storage.text_payload(storage.blob_path(node.blob_sha256), max_bytes)
    except ValueError:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "这是二进制文件，无法按文本预览"
        ) from None


# --------------------------------------------------------------------------- #
# 同名字幕 / 缩略图 / 章节 / 弹幕（播放器插件的数据来源）
# --------------------------------------------------------------------------- #
SUB_EXT = {".vtt", ".srt", ".ass"}
_THUMB_SUFFIX = (".thumbs.vtt", ".thumbnail.vtt")
_CHAPTER_SUFFIX = (".chapters.json",)
_DANMAKU_SUFFIX = (".danmaku.json",)


def _stem(name: str) -> str:
    dot = name.rfind(".")
    return name[:dot] if dot > 0 else name


@router.get("/nodes/{node_id}/sidecars")
def sidecars(node_id: int, db: DbSession, user: CurrentUser) -> dict:
    """找同目录下与视频同名的伴生文件，供播放器插件自动挂载。

    约定（<同名> 指去掉扩展名的文件名）：
    - `<同名>.vtt` / `<同名>.srt` / `<同名>.ass`，或 `<同名>.zh.vtt` → 字幕
    - `<同名>.thumbs.vtt`  → 进度条缩略图（VTT 需带 #xywh）
    - `<同名>.chapters.json` → 章节，格式 [{"start":0,"end":60,"title":"开场"}]
    - `<同名>.danmaku.json`  → 弹幕，格式 [{"time":12.5,"text":"前排"}]
    """
    node = ns.get_node(db, user.id, node_id)
    if node.type != "file":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有文件才有伴生资源")

    stem = _stem(node.name)
    escaped = stem.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = db.scalars(
        select(Node)
        .where(
            Node.user_id == user.id,
            Node.parent_id == node.parent_id if node.parent_id is not None else Node.parent_id.is_(None),
            Node.type == "file",
            Node.deleted_at.is_(None),
            Node.id != node.id,
            Node.name.like(f"{escaped}.%", escape="\\"),
        )
        .order_by(Node.name)
    ).all()

    subtitles: list[dict] = []
    thumbnails = None
    chapters = None
    danmaku = None
    for row in rows:
        lower = row.name.lower()
        url = f"/api/nodes/{row.id}/preview"
        if lower.endswith(_THUMB_SUFFIX):
            thumbnails = thumbnails or url
        elif lower.endswith(_CHAPTER_SUFFIX):
            chapters = chapters or url
        elif lower.endswith(_DANMAKU_SUFFIX):
            danmaku = danmaku or url
        else:
            ext = lower[lower.rfind(".") :]
            if ext in SUB_EXT:
                middle = lower[len(stem) + 1 : -len(ext)]
                subtitles.append(
                    {
                        "name": row.name,
                        "url": url,
                        "type": ext.lstrip("."),
                        "lang": middle.strip(".") or "",
                    }
                )

    return {
        "subtitles": subtitles,
        "thumbnails": thumbnails,
        "chapters": chapters,
        "danmaku": danmaku,
        # 缩略图 VTT 里写的是相对文件名，前端据此改写成可访问的预览地址
        "siblings": {row.name: row.id for row in rows},
    }


# --------------------------------------------------------------------------- #
# 上传（单次流式 PUT，无需 python-multipart）
# --------------------------------------------------------------------------- #
@router.put("/nodes/upload", status_code=status.HTTP_201_CREATED)
async def upload(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    name: str = Query(..., min_length=1, max_length=255),
    parentId: int | None = Query(default=None),
) -> dict:
    check_origin(request)
    filename = normalize_name(name)
    target_parent = parentId
    ns.require_parent(db, user.id, target_parent)

    declared = request.headers.get("content-length")
    expected = int(declared) if declared and declared.isdigit() else 0
    max_size = get_int(db, "max_file_size_bytes") or settings.max_file_size_bytes
    if expected and expected > max_size:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"文件超过单文件上限（{max_size // 1024 // 1024} MB）",
        )
    if expected and user.used_bytes + expected > user.quota_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "存储空间不足，请清理回收站或联系管理员扩容"
        )

    tmp = storage.new_tmp_path()
    digest = hashlib.sha256()
    written = 0
    try:
        with tmp.open("wb") as fh:
            async for block in request.stream():
                if not block:
                    continue
                written += len(block)
                if written > max_size:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"文件超过单文件上限（{max_size // 1024 // 1024} MB）",
                    )
                if user.used_bytes + written > user.quota_bytes:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "存储空间不足")
                digest.update(block)
                fh.write(block)
        if written == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能上传空文件")
        sha = digest.hexdigest()
        dedup = storage.blob_exists(sha, written)
        storage.commit_tmp(tmp, sha)
    finally:
        tmp.unlink(missing_ok=True)

    header_mime = (request.headers.get("x-file-mime") or "").strip()[:127] or None
    node = ns.create_file(db, user, target_parent, filename, sha, written, header_mime)
    ns.recalc_usage(db, user)
    log_action(
        db,
        "node_upload",
        user.id,
        {"name": node.name, "size": written, "dedup": dedup},
        request,
    )
    db.commit()
    db.refresh(node)
    return {"node": ns.node_payload(node), "deduplicated": dedup}


@router.post("/nodes/check-hash")
def check_hash(
    db: DbSession,
    user: CurrentUser,
    sha256: str = Query(..., min_length=64, max_length=64),
    size: int = Query(default=0, ge=0),
) -> dict:
    """秒传探测：服务端已有同内容 blob 时前端可跳过上传。"""
    exists = ns.blob_ready(sha256, size) if sha256.isalnum() else False
    return {"exists": exists}
