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

    # nodeIds 优先；单文件分享仍走 nodeId，两者都兼容
    raw_ids = body.node_ids or ([body.node_id] if body.node_id else [])
    if not raw_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请选择要分享的文件")
    seen: set[int] = set()
    ids: list[int] = []
    for node_id in raw_ids:
        if node_id not in seen:
            seen.add(node_id)
            ids.append(node_id)
    if len(ids) > 100:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "一次最多分享 100 个项目")

    nodes = [ns.get_node(db, user.id, node_id) for node_id in ids]

    problem = ss.code_problem(body.extract_code)
    if problem:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)

    share = ss.create_share(
        db,
        user,
        nodes[0],
        days=body.expires_days,
        code=body.extract_code,
        max_downloads=body.max_downloads,
        nodes=nodes,
    )
    log_action(
        db,
        "share_create",
        user.id,
        {"id": share.id, "node": nodes[0].name, "count": len(nodes)},
        request,
    )
    db.commit()
    db.refresh(share)
    return {"share": ss.share_payload(db, share, nodes[0], {n.id: n for n in nodes})}


@router.get("/shares")
def list_shares(
    db: DbSession,
    user: CurrentUser,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100, alias="pageSize"),
) -> dict:
    return ss.list_page(db, user.id, page=page, page_size=page_size)


@router.delete("/shares/{share_id}")
def delete_share(share_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    """还在有效期的 = 取消分享；已经失效的 = 把记录删掉。"""
    check_origin(request)
    share = db.get(Share, share_id)
    if not share or share.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分享不存在")
    if ss.is_finished(db, share):
        ss.delete_record(db, share)
        log_action(db, "share_delete", user.id, {"id": share_id}, request)
        db.commit()
        return {"deleted": True}
    share.revoked = True
    log_action(db, "share_revoke", user.id, {"id": share.id}, request)
    db.commit()
    return {"revoked": True}


@router.post("/shares/{share_id}/renew")
def renew_share(share_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    """重新分享：按原来的文件与时长生成一条新链接（新 token）。"""
    check_origin(request)
    share = db.get(Share, share_id)
    if not share or share.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分享不存在")
    fresh, code = ss.renew(db, user, share)
    log_action(
        db,
        "share_renew",
        user.id,
        {"from": share.id, "id": fresh.id, "code": bool(code)},
        request,
    )
    db.commit()
    db.refresh(fresh)
    node = db.get(Node, fresh.node_id)
    data = ss.share_payload(db, fresh, node, {node.id: node} if node else {})
    if code:
        data["extractCode"] = code
    return {"share": data}


# --------------------------------------------------------------------------- #
# 公开访问（无需登录）
# --------------------------------------------------------------------------- #
@router.get("/public/shares/{token}")
def public_meta(token: str, request: Request, db: DbSession) -> dict:
    share = ss.load_share(db, token)
    node = ss.root_node(db, share)
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
    node = ss.root_node(db, share)
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

    ids = ss.root_ids(share)

    # 合集分享的「虚拟根」：列出被选中的每一项（可再点进文件夹）
    if folder_id is None and len(ids) > 1:
        rows = db.scalars(
            select(Node).where(
                Node.id.in_(ids),
                Node.user_id == share.user_id,
                Node.deleted_at.is_(None),
            )
        ).all()
        order = {rid: idx for idx, rid in enumerate(ids)}
        rows.sort(key=lambda n: order.get(n.id, len(order)))
        counts = ns.child_counts(db, share.user_id, [n.id for n in rows if n.type == "folder"])
        items = ss.filter_blocked(db, [ns.node_payload(n, counts.get(n.id)) for n in rows])
        return {
            "folder": {
                "id": None,
                "name": f"{len(ids)} 个项目",
                "parentId": None,
                "isRoot": True,
            },
            "items": items,
            "total": len(items),
        }

    root = ss.root_node(db, share)
    folder = ss.node_of(db, share, folder_id) if folder_id else None
    if folder is None:
        # 单根文件夹分享：不带 folderId 就是进根目录；根是文件则本接口不适用
        if root.type != "folder":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "该分享不是文件夹")
        folder = root
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
    node = ss.root_node(db, share)
    ip = client_ip(request)
    ss.report_guard(ip, token)
    ss.report_hit(ip, token)

    reason = (body.reason or "").strip()
    # 合集/文件夹分享举报到整条链接，不定位到单个文件哈希（避免误拉黑同哈希的其它文件）
    multi = ss.is_multi(share)
    whole = multi or node.type == "folder"
    # 合集要按所有根的实际内容求和，否则后台只会显示第一项的大小
    if multi:
        total = 0
        for rid in ss.root_ids(share):
            root = db.get(Node, rid)
            if not root or root.deleted_at:
                continue
            if root.type == "file":
                total += int(root.size or 0)
            else:
                total += int(
                    db.scalar(
                        select(func.coalesce(func.sum(Node.size), 0)).where(
                            Node.id.in_(ns.subtree_ids(db, share.user_id, root.id)),
                            Node.type == "file",
                        )
                    )
                    or 0
                )
    else:
        total = int(node.size or 0)
    report = ShareReport(
        share_id=share.id,
        token=share.token,
        blob_sha256=None if whole else node.blob_sha256,
        first_name=f"{len(ss.root_ids(share))} 个项目" if multi else node.name,
        size=total,
        is_folder=whole,
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
    """转存到我的网盘：文件复用同一 blob，不复制物理数据。

    合集分享可以一次转存勾选的任意多项（nodeIds），全部校验通过才动手，
    避免「存了一半再报空间不足」。
    """
    check_origin(request)
    share = ss.load_share(db, token)

    raw_ids = body.node_ids or []
    if not raw_ids:
        raw_ids = [body.node_id] if body.node_id is not None else [None]
    seen: set[int] = set()
    if raw_ids == [None]:
        # 没指定具体项：整条分享都转（单文件/单文件夹就是它本身，合集则是全部根）
        targets = [ss.node_of(db, share, i) for i in ss.root_ids(share)]
    else:
        clean = [i for i in raw_ids if not (i in seen or seen.add(i))]
        targets = [ss.node_of(db, share, i) for i in clean]

    total_add = 0
    for node in targets:
        if node.type == "file" and node.blob_sha256 and ss.is_blocked(db, node.blob_sha256):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "该文件已被举报确认，禁止转存")
        # 配额预检：要转存的子树总字节数装得下才动手
        if node.type == "folder":
            ids = ns.subtree_ids(db, share.user_id, node.id)
            total_add += int(
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
            total_add += int(node.size or 0)
    if user.used_bytes + total_add > user.quota_bytes:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"网盘空间不足：还需 {format_bytes(total_add)}，请先清理或扩容",
        )

    clones: list[Node] = []
    added_total = 0
    for node in targets:
        clone, added = ns.copy_subtree(db, user, node, body.parent_id)
        clones.append(clone)
        added_total += added
    ns.recalc_usage(db, user)
    log_action(
        db,
        "share_save",
        user.id,
        {
            "shareId": share.id,
            "name": targets[0].name if len(targets) == 1 else f"{len(targets)} 项",
            "size": added_total,
        },
        request,
    )
    db.commit()
    first = clones[0]
    return {
        "saved": True,
        "count": len(clones),
        "nodeId": first.id,
        "name": first.name if len(clones) == 1 else f"{len(clones)} 个项目",
        "size": added_total,
        "nodeIds": [c.id for c in clones],
    }
