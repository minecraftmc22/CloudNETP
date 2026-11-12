"""快传路由：扫码直传会话、站内快传、免登录取件码。

公开段（/api/public/quick/*）不带登录态：
- drop 用 token 当凭据，上传落到会话主人的目录、记主人的配额；
- pickup 用 6 位取件码当凭据，只允许下载这条记录里选中的文件。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from .. import node_service as ns
from .. import quick_service as qs
from .. import storage
from .. import upload_service as us
from ..db import get_db
from ..deps import get_current_user
from ..models import Node, User
from ..schemas import (
    QuickDropIn,
    QuickDropInitIn,
    QuickPickupIn,
    QuickPickupSaveIn,
    QuickReadIn,
    QuickReportIn,
    QuickSendIn,
    QuickTermsIn,
)
from ..security import check_origin, client_ip, normalize_name
from ..user_service import log_action

router = APIRouter(prefix="/api", tags=["quick"])

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

MAX_BODY = 32 * 1024 * 1024


def _nodes_of(db: Session, user: User, ids: list[int]) -> list[Node]:
    """按传入顺序取节点，顺带去重与越权校验。"""
    seen: set[int] = set()
    clean = [i for i in ids if not (i in seen or seen.add(i))]
    if not clean:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请先选择文件")
    if len(clean) > qs.MAX_ITEMS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"一次最多 {qs.MAX_ITEMS} 项")
    return [ns.get_node(db, user.id, node_id) for node_id in clean]


def _pickup_nodes(db: Session, user: User, ids: list[int]) -> list[Node]:
    """取件码的文件来源：既可以是「我的文件」，也可以是刚用快传通道传上来的文件。"""
    seen: set[int] = set()
    clean = [i for i in ids if not (i in seen or seen.add(i))]
    if not clean:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请先选择文件")
    if len(clean) > qs.MAX_ITEMS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"一次最多 {qs.MAX_ITEMS} 项")
    out: list[Node] = []
    for node_id in clean:
        node = db.get(Node, node_id)
        if node is None or node.user_id != user.id or node.deleted_at:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在或无权访问")
        out.append(node)
    return out


# --------------------------------------------------------------------------- #
# 使用规范（先同意再创建）
# --------------------------------------------------------------------------- #
@router.get("/quick/terms")
def read_terms(db: DbSession, user: CurrentUser) -> dict:
    return qs.terms_of(db, user.id)


@router.post("/quick/terms")
def accept_terms(body: QuickTermsIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    if not body.accepted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "需要勾选同意才能使用快传")
    state = qs.accept_terms(db, user)
    log_action(db, "quick_terms", user.id, {"accepted": True}, request)
    db.commit()
    return state


@router.get("/quick/limits")
def quick_limits(db: DbSession, user: CurrentUser) -> dict:
    """取件码的可选范围（管理员设的上限），前端据此渲染下拉框。"""
    return {"limits": qs.limits(db)}


# --------------------------------------------------------------------------- #
# 扫码直传（需登录：创建 / 管理）
# --------------------------------------------------------------------------- #
@router.post("/quick/drops")
def create_drop(body: QuickDropIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    qt = qs.create_drop(db, user, parent_id=body.parent_id, minutes=body.minutes)
    log_action(
        db,
        "quick_drop",
        user.id,
        {"id": qt.id, "folder": qt.name, "minutes": body.minutes},
        request,
    )
    db.commit()
    db.refresh(qt)
    return {"drop": qs.payload(db, qt)}


@router.get("/quick/drops")
def list_drops(db: DbSession, user: CurrentUser) -> dict:
    items = qs.list_for_user(db, user.id, "drop")
    return {"items": items, "total": len(items)}


@router.delete("/quick/drops/{qt_id}")
def revoke_drop(qt_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    qt = qs.get_owned(db, user.id, qt_id, kind="drop")
    qs.revoke(qt)
    log_action(db, "quick_drop_revoke", user.id, {"id": qt.id}, request)
    db.commit()
    return {"revoked": True}


# --------------------------------------------------------------------------- #
# 站内快传（需登录）
# --------------------------------------------------------------------------- #
@router.post("/quick/sends")
def send_files(body: QuickSendIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    nodes = _nodes_of(db, user, body.node_ids)
    qt = qs.send_files(
        db, user, nodes, to_username=body.to_username, message=body.message
    )
    log_action(
        db,
        "quick_send",
        user.id,
        {"id": qt.id, "to": body.to_username, "count": qt.count, "size": qt.size},
        request,
    )
    db.commit()
    db.refresh(qt)
    peer = db.get(User, qt.peer_id)
    data = qs.payload(db, qt, with_code=False)
    data["peer"] = {
        "id": peer.id if peer else None,
        "username": peer.username if peer else "",
        "displayName": (peer.display_name or peer.username) if peer else "",
    }
    return {"send": data}


@router.get("/quick/inbox")
def inbox(db: DbSession, user: CurrentUser) -> dict:
    items = qs.send_list(db, user.id, incoming=True)
    return {"items": items, "unread": qs.unread_count(db, user.id)}


@router.get("/quick/outbox")
def outbox(db: DbSession, user: CurrentUser) -> dict:
    items = qs.send_list(db, user.id, incoming=False)
    return {"items": items, "total": len(items)}


@router.post("/quick/inbox/read")
def read_inbox(body: QuickReadIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    count = qs.mark_seen(db, user.id, body.ids)
    db.commit()
    return {"read": count, "unread": qs.unread_count(db, user.id)}


# --------------------------------------------------------------------------- #
# 取件码（需登录：创建 / 管理）
# --------------------------------------------------------------------------- #
@router.post("/quick/pickups")
def create_pickup(body: QuickPickupIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    if body.items:
        entries = []
        for item in body.items:
            node = _pickup_nodes(db, user, [item.node_id])[0]
            entries.append({"node": node, "days": item.days, "maxUses": item.max_uses})
    else:
        nodes = _pickup_nodes(db, user, body.node_ids)
        entries = [
            {"node": node, "days": body.days, "maxUses": body.max_uses} for node in nodes
        ]
    qt = qs.create_pickup(db, user, entries, days=body.days, max_uses=body.max_uses)
    log_action(
        db,
        "quick_pickup",
        user.id,
        {"id": qt.id, "count": qt.count, "size": qt.size},
        request,
    )
    db.commit()
    db.refresh(qt)
    return {"pickup": qs.payload(db, qt)}


@router.get("/quick/pickups")
def list_pickups(db: DbSession, user: CurrentUser) -> dict:
    items = qs.list_for_user(db, user.id, "pickup")
    return {"items": items, "total": len(items)}


@router.get("/quick/pickups/code/{code}")
def pickup_info(code: str, db: DbSession, user: CurrentUser) -> dict:
    """登录用户输入取件码：不占匿名限流，也不会因为反复查看把次数用掉。"""
    qt = qs.find_pickup(db, code)
    return {"pickup": qs.pickup_public(db, qt), "own": qt.owner_id == user.id}


@router.post("/quick/pickups/code/{code}/save")
def pickup_save(
    code: str, body: QuickPickupSaveIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    qt = qs.find_pickup(db, code)
    nodes = qs.pickup_save(db, user, qt, body.parent_id)
    log_action(
        db,
        "quick_pickup_save",
        user.id,
        {"pickup": qt.id, "count": len(nodes), "to": body.parent_id},
        request,
    )
    db.commit()
    return {"saved": len(nodes), "nodes": [ns.node_payload(n) for n in nodes]}


@router.delete("/quick/pickups/{qt_id}")
def revoke_pickup(qt_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    qt = qs.get_owned(db, user.id, qt_id, kind="pickup")
    qs.revoke(qt)
    log_action(db, "quick_pickup_revoke", user.id, {"id": qt.id}, request)
    db.commit()
    return {"revoked": True}


# --------------------------------------------------------------------------- #
# 扫码直传（上传侧一律要求登录，且必须是会话发起人）
# --------------------------------------------------------------------------- #
# 取件码可以匿名下载，上传不行：链接一旦外泄，谁都能往你的目录里塞东西，
# 还会吃掉你的配额。所以扫码只是省掉「找目录」这一步，不省登录。
@router.get("/public/quick/drop/{token}")
def drop_meta(token: str, db: DbSession, user: CurrentUser) -> dict:
    qt = qs.load_drop_for(db, token, user)
    return {"drop": qs.drop_public(db, qt)}


@router.post("/public/quick/drop/{token}/init")
def drop_init(
    token: str, body: QuickDropInitIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    qt = qs.load_drop_for(db, token, user)
    owner = db.get(User, qt.owner_id)
    if owner is None or owner.banned:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "会话主人已不可用")
    # 客户端声明的哈希先拦一道（省掉白传一轮分片）；真正的判定在 complete 用服务端算的哈希
    qs.assert_upload_allowed(db, body.sha256)
    result = us.init_upload(
        db,
        owner,
        name=normalize_name(body.name),
        size=body.size,
        parent_id=qt.target_id,
        sha256=body.sha256,
        mime=body.mime,
        chunk_size=body.chunk_size,
    )
    if result.get("instant") and result.get("node"):
        node = db.get(Node, int(result["node"]["id"]))
        if node is not None:
            qs.note_drop_upload(db, qt, node)
            log_action(
                db,
                "quick_upload",
                owner.id,
                {"drop": qt.id, "name": node.name, "size": node.size, "dedup": True},
                request,
            )
    db.commit()
    return result


@router.put("/public/quick/drop/{token}/{session_id}/{index}")
async def drop_chunk(
    token: str,
    session_id: str,
    index: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    check_origin(request)
    qt = qs.load_drop_for(db, token, user)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "分片过大")
    if index < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "分片序号不合法")
    body = await request.body()
    if not body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "分片内容为空")
    result = us.save_chunk(db, qt.owner_id, session_id, index, body)
    db.commit()
    return result


@router.post("/public/quick/drop/{token}/{session_id}/complete")
def drop_complete(
    token: str,
    session_id: str,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    sha256: str | None = Query(default=None, min_length=64, max_length=64),
) -> dict:
    check_origin(request)
    qt = qs.load_drop_for(db, token, user)
    owner = db.get(User, qt.owner_id)
    if owner is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "会话主人已不可用")

    def guard(real_sha: str, _size: int) -> None:
        qs.assert_upload_allowed(db, real_sha)

    try:
        result = us.complete_upload(db, owner, session_id, sha256, guard=guard)
    except HTTPException as exc:
        # 命中黑名单：这次上传直接作废，分片不留在盘上占空间
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            try:
                us.abort_upload(db, owner.id, session_id)
            except HTTPException:
                pass
            db.commit()
        raise
    node = db.get(Node, int(result["node"]["id"]))
    if node is not None:
        qs.note_drop_upload(db, qt, node)
        log_action(
            db,
            "quick_upload",
            owner.id,
            {"drop": qt.id, "name": node.name, "size": node.size, "dedup": result["deduplicated"]},
            request,
        )
    db.commit()
    return result


# --------------------------------------------------------------------------- #
# 公开：取件码
# --------------------------------------------------------------------------- #
@router.get("/public/quick/pickup/{code}")
def pickup_meta(code: str, request: Request, db: DbSession) -> dict:
    ip = client_ip(request)
    qs.pickup_guard(ip)
    try:
        qt = qs.find_pickup(db, code)
    except HTTPException:
        qs.pickup_miss(ip)
        raise
    qs.pickup_hit(ip)
    return {"pickup": qs.pickup_public(db, qt)}


@router.get("/public/quick/pickup/{code}/download")
def pickup_download(
    code: str,
    request: Request,
    db: DbSession,
    node_id: int | None = Query(default=None, alias="nodeId"),
):
    ip = client_ip(request)
    qs.pickup_guard(ip)
    try:
        qt = qs.find_pickup(db, code)
    except HTTPException:
        qs.pickup_miss(ip)
        raise
    qs.pickup_hit(ip)
    node = qs.pickup_node(db, qt, node_id)

    # Range 请求（视频拖动、断点续传）不计次
    if "range" not in request.headers:
        qs.consume_pickup(db, qt, node)
        log_action(
            db,
            "quick_pickup_download",
            qt.owner_id,
            {"pickup": qt.id, "name": node.name},
            request,
        )
        db.commit()

    return storage.file_response(
        storage.blob_path(node.blob_sha256), node.name, node.mime, request
    )


@router.post("/public/quick/pickup/{code}/report")
def pickup_report(
    code: str, body: QuickReportIn, request: Request, db: DbSession
) -> dict:
    """取件页举报单个文件：匿名可提交，进管理后台的举报审核队列。"""
    ip = client_ip(request)
    qs.pickup_guard(ip)
    try:
        qt = qs.find_pickup(db, code)
    except HTTPException:
        qs.pickup_miss(ip)
        raise
    qs.pickup_hit(ip)

    node_id = body.node_id
    node = qs.pickup_node(db, qt, node_id)
    report = qs.report_pickup(db, qt, node, reason=body.reason, ip=ip)
    log_action(
        db,
        "quick_report",
        None,
        {"pickup": qt.id, "name": node.name, "reportId": report.id},
        request,
    )
    db.commit()
    return {"reported": True, "id": report.id}
