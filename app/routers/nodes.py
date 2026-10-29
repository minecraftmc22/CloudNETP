"""文件与目录路由：浏览、增删改、移动、搜索、回收站、下载、上传。"""

from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
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
from ..user_service import log_action

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
    count = ns.soft_delete(db, user, node)
    log_action(db, "node_delete", user.id, {"id": node_id, "name": node.name, "count": count}, request)
    db.commit()
    return {"deleted": count}


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
