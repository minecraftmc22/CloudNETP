"""分享链接路由：创建 / 列表 / 取消 + 公开访问（提取码、浏览、预览、下载）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import node_service as ns
from .. import profile_service as ps
from .. import share_service as ss
from .. import storage
from ..db import get_db
from ..deps import get_current_user
from ..models import Share, User
from ..schemas import ShareIn, ShareUnlockIn
from ..security import check_origin
from ..user_service import log_action

router = APIRouter(prefix="/api", tags=["shares"])

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


# --------------------------------------------------------------------------- #
# 创建 / 管理（需登录）
# --------------------------------------------------------------------------- #
@router.post("/shares")
def create_share(body: ShareIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    node = ns.get_node(db, user.id, body.node_id)

    problem = ss.code_problem(body.extract_code)
    if problem:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)

    share = ss.create_share(
        db,
        user,
        node,
        days=body.expires_days,
        code=body.extract_code,
        max_downloads=body.max_downloads,
    )
    log_action(db, "share_create", user.id, {"id": share.id, "node": node.name}, request)
    db.commit()
    db.refresh(share)
    return {"share": ss.share_payload(db, share, node)}


@router.get("/shares")
def list_shares(db: DbSession, user: CurrentUser) -> dict:
    items = ss.list_for_user(db, user.id)
    return {"items": items, "total": len(items)}


@router.delete("/shares/{share_id}")
def revoke_share(share_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    share = db.get(Share, share_id)
    if not share or share.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分享不存在")
    share.revoked = True
    log_action(db, "share_revoke", user.id, {"id": share.id}, request)
    db.commit()
    return {"revoked": True}


# --------------------------------------------------------------------------- #
# 公开访问（无需登录）
# --------------------------------------------------------------------------- #
@router.get("/public/shares/{token}")
def public_meta(token: str, request: Request, db: DbSession) -> dict:
    share = ss.load_share(db, token)
    node = ss.node_of(db, share)
    return {"share": ss.public_payload(request, share, node, db)}


@router.get("/public/shares/{token}/avatar")
def public_avatar(token: str, db: DbSession):
    """分享页头像：只有分享者打开「显示完整昵称」时才对外提供。"""
    share = ss.load_share(db, token)
    owner = db.get(User, share.user_id)
    if not owner or not ps.author_payload(db, share, owner)["avatar"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "头像不可见")
    path = ps.avatar_path(owner.avatar)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "头像不存在")
    return FileResponse(
        path,
        media_type=ps.avatar_media_type(owner.avatar),
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.post("/public/shares/{token}/unlock")
def unlock_share(
    token: str, body: ShareUnlockIn, request: Request, response: Response, db: DbSession
) -> dict:
    share = ss.load_share(db, token)
    node = ss.node_of(db, share)
    if ss.check_access(request, share, body.code):
        ss.set_access_cookie(response, share)
    payload = ss.public_payload(request, share, node, db)
    # Cookie 是刚写到响应上的，本次请求的 request.cookies 里还没有，直接标记为已解锁
    payload["unlocked"] = True
    return {"share": payload, "unlocked": True}


@router.get("/public/shares/{token}/list")
def public_list(
    token: str,
    request: Request,
    response: Response,
    db: DbSession,
    code: str | None = Query(default=None),
    folder_id: int | None = Query(default=None, alias="folderId"),
) -> dict:
    share = ss.load_share(db, token)
    if ss.check_access(request, share, code):
        ss.set_access_cookie(response, share)

    root = ss.node_of(db, share)
    if root.type != "folder":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该分享不是文件夹")

    folder = ss.node_of(db, share, folder_id) if folder_id else root
    if folder.type != "folder":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "目标不是文件夹")

    items = ns.list_children(db, share.user_id, folder.id)
    return {
        "folder": {
            "id": folder.id,
            "name": folder.name,
            "parentId": None if folder.id == root.id else folder.parent_id,
            "isRoot": folder.id == root.id,
        },
        "items": items,
        "total": len(items),
    }


@router.get("/public/shares/{token}/download")
def public_download(
    token: str,
    request: Request,
    db: DbSession,
    code: str | None = Query(default=None),
    node_id: int | None = Query(default=None, alias="nodeId"),
):
    share = ss.load_share(db, token)
    fresh = ss.check_access(request, share, code)
    node = ss.node_of(db, share, node_id)
    if node.type != "file" or not node.blob_sha256:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件夹不能直接下载")

    # 浏览器下载通常是一次无 Range 的 GET；Range 请求（视频拖动）不计次
    if "range" not in request.headers:
        share.download_count += 1
        log_action(
            db,
            "share_download",
            share.user_id,
            {"shareId": share.id, "token": share.token[:8], "name": node.name},
            request,
        )
        db.commit()

    resp = storage.file_response(
        storage.blob_path(node.blob_sha256), node.name, node.mime, request
    )
    if fresh:
        ss.set_access_cookie(resp, share)
    return resp


@router.get("/public/shares/{token}/preview")
def public_preview(
    token: str,
    request: Request,
    db: DbSession,
    code: str | None = Query(default=None),
    node_id: int | None = Query(default=None, alias="nodeId"),
):
    share = ss.load_share(db, token)
    fresh = ss.check_access(request, share, code)
    node = ss.node_of(db, share, node_id)
    if node.type != "file" or not node.blob_sha256:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件夹不支持在线预览")

    kind = ns.preview_kind(node)
    if kind == "none":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "该类型暂不支持在线预览")

    resp = storage.file_response(
        storage.blob_path(node.blob_sha256),
        node.name,
        node.mime,
        request,
        inline=True,
        csp="" if kind == "pdf" else None,
    )
    if fresh:
        ss.set_access_cookie(resp, share)
    return resp


@router.get("/public/shares/{token}/text")
def public_text(
    token: str,
    request: Request,
    response: Response,
    db: DbSession,
    code: str | None = Query(default=None),
    node_id: int | None = Query(default=None, alias="nodeId"),
    max_bytes: int = Query(default=storage.TEXT_PREVIEW_BYTES, ge=1024, le=4_000_000),
) -> dict:
    share = ss.load_share(db, token)
    if ss.check_access(request, share, code):
        ss.set_access_cookie(response, share)
    node = ss.node_of(db, share, node_id)
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
