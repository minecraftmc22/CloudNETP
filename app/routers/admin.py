"""管理后台路由：概览、用户管理、系统设置、审计日志、磁盘维护。

所有端点都强制管理员身份；写操作额外做 Origin 校验（与其它路由一致）。
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from .. import admin_service as admin
from .. import dav_auth as da
from .. import node_service as ns
from .. import profile_service as ps
from .. import storage
from ..db import get_db
from ..deps import require_admin
from ..models import User
from ..schemas import AdminGcIn, AdminLogPurgeIn, AdminSettingsIn, AdminUserPatchIn
from ..security import check_origin, hash_password
from ..settings_store import set_value
from ..user_service import log_action

router = APIRouter(prefix="/api/admin", tags=["admin"])

DbSession = Annotated[Session, Depends(get_db)]
AdminUser = Annotated[User, Depends(require_admin)]

# 去掉容易看错的 0/O/1/l/I，管理员抄写临时密码时少踩坑
_TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _temp_password(length: int = 12) -> str:
    """生成临时密码，并保证它本身就满足 password_problem 的规则。

    随机拼 12 位几乎不可能缺数字或小写，但「几乎」不等于「一定」：
    真抽到全大写时，用户拿临时密码去改密码会被自己的规则拦住。
    """
    chars = [secrets.choice(_TEMP_ALPHABET) for _ in range(length)]
    if not any(ch.isdigit() for ch in chars):
        chars[0] = secrets.choice("23456789")
    if not any(ch.islower() for ch in chars):
        chars[1 if length > 1 else 0] = secrets.choice("abcdefghjkmnpqrstuvwxyz")
    rng = secrets.SystemRandom()
    rng.shuffle(chars)
    return "".join(chars)


def _load_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    return user


# --------------------------------------------------------------------------- #
# 概览
# --------------------------------------------------------------------------- #
@router.get("/overview")
def overview(db: DbSession, _: AdminUser) -> dict:
    return admin.overview(db)


# --------------------------------------------------------------------------- #
# 用户
# --------------------------------------------------------------------------- #
@router.get("/users")
def list_users(
    db: DbSession,
    _: AdminUser,
    query: str = Query(default="", max_length=64),
    state: str = Query(default="all", pattern="^(all|active|banned|admin)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="pageSize"),
) -> dict:
    return admin.list_users(db, query=query, state=state, page=page, page_size=page_size)


@router.patch("/users/{user_id}")
def patch_user(
    user_id: int,
    payload: AdminUserPatchIn,
    request: Request,
    db: DbSession,
    me: AdminUser,
) -> dict:
    check_origin(request)
    target = _load_user(db, user_id)
    changed: list[str] = []

    if payload.quota_bytes is not None and int(target.quota_bytes) != payload.quota_bytes:
        old = int(target.quota_bytes)
        target.quota_bytes = payload.quota_bytes
        log_action(
            db,
            "admin_user_quota",
            me.id,
            {"target": target.username, "from": old, "to": payload.quota_bytes},
            request,
        )
        changed.append("quota")

    if payload.banned is not None and bool(target.banned) != payload.banned:
        if target.id == me.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能封禁自己")
        if payload.banned and target.role == 1 and admin.admin_count(db) <= 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "至少需要保留一名可用管理员")
        target.banned = payload.banned
        if payload.banned:
            target.token_version += 1  # 让已登录的会话立即失效
        log_action(
            db,
            "admin_user_ban" if payload.banned else "admin_user_unban",
            me.id,
            {"target": target.username},
            request,
        )
        changed.append("banned")

    if payload.role is not None and int(target.role) != payload.role:
        if target.id == me.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能修改自己的角色")
        if target.role == 1 and admin.admin_count(db) <= 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "至少需要保留一名管理员")
        target.role = payload.role
        log_action(
            db,
            "admin_user_role",
            me.id,
            {"target": target.username, "role": payload.role},
            request,
        )
        changed.append("role")

    if not changed:
        return {"user": admin.user_row(db, target)}
    db.commit()
    db.refresh(target)
    return {"user": admin.user_row(db, target)}


@router.post("/users/{user_id}/password")
def reset_password(
    user_id: int, request: Request, db: DbSession, me: AdminUser
) -> dict:
    """重置为一次性随机密码：只在这里明文返回一次，库里仍只存哈希。"""
    check_origin(request)
    target = _load_user(db, user_id)
    temp = _temp_password()
    target.password_hash = hash_password(temp)
    target.dav_ha1 = da.ha1(target.username, temp)  # WebDAV 用 Digest，得同步
    target.token_version += 1  # 踢掉该用户所有已登录设备
    log_action(db, "admin_user_password", me.id, {"target": target.username}, request)
    db.commit()
    return {"password": temp, "username": target.username}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, request: Request, db: DbSession, me: AdminUser) -> dict:
    check_origin(request)
    target = _load_user(db, user_id)
    if target.id == me.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能删除自己")
    if target.role == 1 and admin.admin_count(db) <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "至少需要保留一名管理员")

    name = target.username
    avatar = target.avatar
    result = admin.purge_user(db, target)
    ps.drop_avatar(avatar)  # 数据库行没了，头像文件也别留在盘上
    log_action(
        db,
        "admin_user_delete",
        me.id,
        {"target": name, "id": user_id, **result},
        request,
    )
    db.commit()
    return {"deleted": True, **result}


# --------------------------------------------------------------------------- #
# 系统设置
# --------------------------------------------------------------------------- #
@router.get("/settings")
def get_settings(db: DbSession, _: AdminUser) -> dict:
    return {"settings": admin.settings_payload(db)}


@router.patch("/settings")
def update_settings(
    payload: AdminSettingsIn, request: Request, db: DbSession, me: AdminUser
) -> dict:
    check_origin(request)
    patch = payload.model_dump(exclude_none=True)
    if not patch:
        return {"settings": admin.settings_payload(db)}

    for key, value in patch.items():
        text = ("1" if value else "0") if isinstance(value, bool) else str(value)
        set_value(db, key, text)
    log_action(db, "admin_settings_update", me.id, patch, request)
    db.commit()
    return {"settings": admin.settings_payload(db)}


# --------------------------------------------------------------------------- #
# 审计日志
# --------------------------------------------------------------------------- #
@router.get("/logs")
def list_logs(
    db: DbSession,
    _: AdminUser,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100, alias="pageSize"),
    action: str = Query(default="", max_length=32),
    user_id: int | None = Query(default=None, alias="userId"),
    query: str = Query(default="", max_length=64),
) -> dict:
    return admin.list_logs(
        db, page=page, page_size=page_size, action=action, user_id=user_id, query=query
    )


@router.get("/logs/actions")
def log_actions(db: DbSession, _: AdminUser) -> dict:
    return {"items": admin.action_catalog(db)}


@router.post("/logs/purge")
def purge_logs(payload: AdminLogPurgeIn, request: Request, db: DbSession, me: AdminUser) -> dict:
    check_origin(request)
    removed = admin.purge_logs(db, payload.days)
    log_action(db, "admin_logs_purge", me.id, {"days": payload.days, "removed": removed}, request)
    db.commit()
    return {"removed": removed}


# --------------------------------------------------------------------------- #
# 分享举报审核
# --------------------------------------------------------------------------- #
@router.get("/reports")
def list_reports(
    db: DbSession,
    _: AdminUser,
    report_status: str = Query(default="pending", pattern="^(pending|approved|rejected|all)$", alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="pageSize"),
) -> dict:
    return admin.list_reports(db, status=report_status, page=page, page_size=page_size)


@router.get("/reports/{report_id}/files")
def report_files(report_id: int, db: DbSession, _: AdminUser) -> dict:
    """举报涉及的文件清单：管理员先看清是什么内容，再决定通过还是驳回。"""
    try:
        return admin.report_files(db, report_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None


@router.get("/reports/{report_id}/preview")
def report_preview(
    report_id: int,
    request: Request,
    db: DbSession,
    _: AdminUser,
    node_id: int | None = Query(default=None, alias="nodeId"),
    download: bool = Query(default=False),
):
    """被举报文件的内容流（支持 Range）。与分享是否撤销无关，管理员始终能看。"""
    try:
        report, node, sha = admin.report_file_for_preview(db, report_id, node_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    path = storage.blob_path(sha)
    name = (node.name if node else report.first_name) or "file"
    kind = ns.preview_kind(node, is_admin=True) if node else "none"
    media = ns.preview_mime(kind, node.mime if node else None) if kind != "none" else None
    # 浏览器不能内联显示的类型（压缩包、可执行文件等）直接按附件下载
    return storage.file_response(
        path,
        name,
        media,
        request,
        inline=(not download) and kind != "none",
        cache="no-store",
    )


@router.get("/reports/{report_id}/thumb")
def report_thumb(
    report_id: int,
    db: DbSession,
    _: AdminUser,
    node_id: int | None = Query(default=None, alias="nodeId"),
    w: int = Query(default=160, ge=64, le=640),
):
    """举报图片的缩略图，列表与详情里直接看到画面。"""
    try:
        path = admin.report_thumb_path(db, report_id, node_id, w)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    except Exception:  # Pillow 打不开的格式（HEIC 等）
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "这个格式没法生成缩略图") from None
    return storage.file_response(path, "thumb.webp", "image/webp", cache="private, max-age=86400")


@router.get("/reports/{report_id}/text")
def report_text(
    report_id: int,
    db: DbSession,
    _: AdminUser,
    node_id: int | None = Query(default=None, alias="nodeId"),
    max_bytes: int = Query(default=200_000, ge=1024, le=4_000_000, alias="maxBytes"),
) -> dict:
    try:
        report, node, sha = admin.report_file_for_preview(db, report_id, node_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    try:
        return storage.text_payload(storage.blob_path(sha), max_bytes)
    except ValueError:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "这是二进制文件，无法按文本预览"
        ) from None


@router.post("/reports/{report_id}/approve")
def approve_report(
    report_id: int, request: Request, db: DbSession, me: AdminUser
) -> dict:
    check_origin(request)
    try:
        result = admin.approve_report(db, report_id, me)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    log_action(db, "admin_report_approve", me.id, {"reportId": report_id, **result}, request)
    db.commit()
    return {"approved": True, **result}


@router.post("/reports/{report_id}/reject")
def reject_report(
    report_id: int, request: Request, db: DbSession, me: AdminUser
) -> dict:
    check_origin(request)
    try:
        admin.reject_report(db, report_id, me)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    log_action(db, "admin_report_reject", me.id, {"reportId": report_id}, request)
    db.commit()
    return {"rejected": True}


@router.post("/blacklist/{sha256}/unblock")
def unblock_blob(sha256: str, request: Request, db: DbSession, me: AdminUser) -> dict:
    check_origin(request)
    if len(sha256) != 64 or not all(c in "0123456789abcdef" for c in sha256):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "哈希格式不正确")
    removed = admin.unblock_blob(db, sha256, me)
    if removed:
        log_action(db, "admin_blob_unblock", me.id, {"sha256": sha256}, request)
        db.commit()
    return {"unblocked": removed}


# --------------------------------------------------------------------------- #
# 磁盘维护
# --------------------------------------------------------------------------- #
@router.post("/maintenance/blob-gc")
def blob_gc(payload: AdminGcIn, request: Request, db: DbSession, me: AdminUser) -> dict:
    check_origin(request)
    result = admin.blob_gc(db, apply=payload.apply)
    if payload.apply and (result["removed"] or result["tmpStale"]):
        log_action(db, "admin_blob_gc", me.id, result, request)
        db.commit()
    return result
