"""分享链接路由：创建 / 列表 / 取消 + 公开访问（提取码、浏览、预览、下载）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import node_service as ns
from .. import profile_service as ps
from .. import share_service as ss
from .. import storage
from ..db import get_db
from ..deps import get_current_user
from ..models import Node, Share, ShareReport, User, utcnow
from ..schemas import ShareIn, ShareReportIn, ShareSaveIn, ShareUnlockIn
from ..security import check_origin, client_ip
from ..user_service import log_action

router = APIRouter(prefix="/api", tags=["shares"])

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


def format_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


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
    items = ss.filter_blocked(db, items)
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

    # 与登录态预览保持一致：Office 类降级成 application/octet-stream 并禁用缓存，
    # 避免浏览器把旧版本带插件 MIME 的响应继续从缓存里复用。
    media = ns.preview_mime(kind, node.mime)
    resp = storage.file_response(
        storage.blob_path(node.blob_sha256),
        node.name,
        media,
        request,
        inline=True,
        cache="no-store" if media == "application/octet-stream" else "private, max-age=0",
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
    if ns.preview_kind(node) not in ("text", "markdown"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "该文件不是文本类型")

    try:
        return storage.text_payload(storage.blob_path(node.blob_sha256), max_bytes)
    except ValueError:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "这是二进制文件，无法按文本预览"
        ) from None


# --------------------------------------------------------------------------- #
# 举报 / 转存（公开分享页）
# --------------------------------------------------------------------------- #
@router.post("/public/shares/{token}/report")
def report_share(
    token: str, body: ShareReportIn, request: Request, db: DbSession
) -> dict:
    """匿名举报分享：记录分享链接、文件哈希与首次被举报时的文件名。"""
    share = ss.load_share(db, token)
    node = ss.node_of(db, share)
    ip = client_ip(request)
    ss.report_guard(ip, token)
    ss.report_hit(ip, token)

    reason = (body.reason or "").strip()
    report = ShareReport(
        share_id=share.id,
        token=share.token,
        blob_sha256=node.blob_sha256 if node.type == "file" else None,
        first_name=node.name,
        size=int(node.size or 0),
        is_folder=node.type == "folder",
        reason=reason,
        reporter_ip=ip,
    )
    db.add(report)
    db.flush()
    log_action(db, "share_report", None, {"shareId": share.id, "name": node.name}, request)
    db.commit()
    return {"reported": True, "id": report.id}


@router.post("/public/shares/{token}/save")
def save_share(
    token: str, body: ShareSaveIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    """转存到我的网盘：文件复用同一 blob，不复制物理数据。"""
    check_origin(request)
    share = ss.load_share(db, token)
    node = ss.node_of(db, share, body.node_id)
    if node.type == "file" and node.blob_sha256 and ss.is_blocked(db, node.blob_sha256):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该文件已被举报确认，禁止转存")

    # 配额预检：要转存的子树总字节数装得下才动手
    if node.type == "folder":
        ids = ns.subtree_ids(db, share.user_id, node.id)
        to_add = int(
            db.scalar(
                select(func.coalesce(func.sum(Node.size), 0)).where(
                    Node.user_id == share.user_id,
                    Node.id.in_(ids),
                    Node.type == "file",
                )
            )
            or 0
        )
    else:
        to_add = int(node.size or 0)
    if user.used_bytes + to_add > user.quota_bytes:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"网盘空间不足：还需 {format_bytes(to_add)}，请先清理或扩容",
        )

    clone, added = ns.copy_subtree(db, user, node, body.parent_id)
    ns.recalc_usage(db, user)
    log_action(
        db,
        "share_save",
        user.id,
        {"shareId": share.id, "name": node.name, "size": added},
        request,
    )
    db.commit()
    return {"saved": True, "nodeId": clone.id, "name": clone.name, "size": added}
