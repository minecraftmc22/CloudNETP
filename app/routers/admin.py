"""管理后台路由：概览、用户管理、系统设置、审计日志、磁盘维护。

所有端点都强制管理员身份；写操作额外做 Origin 校验（与其它路由一致）。
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from .. import admin_service as admin
from .. import ai_defaults
from .. import dav_auth as da
from .. import db_failover
from .. import lyrics_config
from .. import lyrics_service
from .. import netease_auth as ng
from .. import node_service as ns
from .. import profile_service as ps
from .. import quick_service as qs
from .. import storage
from ..db import get_db
from ..deps import require_admin
from ..models import User
from ..schemas import (
    AdminAiDefaultIn,
    AdminDbReconnectIn,
    AdminGcIn,
    AdminLogPurgeIn,
    AdminLyricsIn,
    AdminQuickCleanupIn,
    AdminLyricsTestIn,
    AdminSettingsIn,
    AdminUserPatchIn,
)
from ..security import check_origin, hash_password
from ..settings_store import set_value
from ..user_service import log_action

router = APIRouter(prefix="/api/admin", tags=["admin"])

log = logging.getLogger("uvicorn.error")

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


# --------------------------------------------------------------------------- #
# 快传：概况与清理
# --------------------------------------------------------------------------- #
@router.get("/quick")
def quick_overview(db: DbSession, _: AdminUser) -> dict:
    return {"quick": qs.overview(db)}


@router.post("/quick/cleanup")
def quick_cleanup(
    body: AdminQuickCleanupIn, request: Request, db: DbSession, me: AdminUser
) -> dict:
    """立刻回收过期 / 撤销的取件码文件；只删快传自己存的文件，不动用户的网盘。"""
    check_origin(request)
    result = qs.cleanup_expired(db, retention_days=body.days)
    log_action(db, "admin_quick_cleanup", me.id, result, request)
    db.commit()
    return {"cleaned": result, "quick": qs.overview(db)}


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
# 数据库（容灾状态 / 刷新配置 / 手动重连）
# --------------------------------------------------------------------------- #
def _db_audit(request: Request, user_id: int, action: str, detail: dict) -> None:
    """库可能刚被换掉，请求里那条会话绑的是旧引擎，另开一个写审计。"""
    session = db_failover.current_sessionmaker()()
    try:
        log_action(session, action, user_id, detail, request)
        session.commit()
    except Exception as exc:  # 审计写失败不该让「库已经连上了」变成 500
        log.warning("写数据库审计日志失败：%s", exc)
    finally:
        session.close()


@router.get("/db")
def db_state(_: AdminUser) -> dict:
    """当前服务库、主库探活、容灾副本与看门狗参数（连接串已脱敏）。"""
    return {"db": db_failover.describe()}


@router.post("/db/refresh")
def db_refresh(request: Request, me: AdminUser) -> dict:
    """重读 .env 的数据库配置，重建连接池并按容灾规则落回可用的库。"""
    check_origin(request)
    result = db_failover.reload_config()
    _db_audit(
        request,
        me.id,
        "admin_db_refresh",
        {"changes": result["changes"], "landed": result["landed"]},
    )
    return {"result": result, "db": db_failover.describe()}


@router.post("/db/reconnect")
def db_reconnect(payload: AdminDbReconnectIn, request: Request, me: AdminUser) -> dict:
    """手动重连并切到指定库；连不上直接报错，保持当前库不动。"""
    check_origin(request)
    try:
        result = db_failover.reconnect(payload.target)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    _db_audit(request, me.id, "admin_db_reconnect", result)
    return {"result": result, "db": db_failover.describe()}


# --------------------------------------------------------------------------- #
# 网易云歌词账号（扫码登录，Cookie 只在服务端）
# --------------------------------------------------------------------------- #
@router.get("/netease")
def netease_status(db: DbSession, _: AdminUser) -> dict:
    """当前网易云连接状态。只回昵称/头像/时间，**不回 Cookie**。"""
    return {"account": ng.status(db)}


@router.post("/netease/qr")
def netease_qr(request: Request, _: AdminUser) -> dict:
    """申请一个扫码登录会话，返回二维码内容与本站会话 id。"""
    check_origin(request)
    try:
        return ng.qr_create()
    except ng.NeteaseError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.message) from None


# 注意顺序：带 .svg 的必须写在 /{session} 之前，否则会被当成本 session id
@router.get("/netease/qr/{session}.svg")
def netease_qr_svg(session: str, _: AdminUser) -> Response:
    try:
        svg = ng.qr_svg(session)
    except ng.NeteaseError as exc:
        raise HTTPException(exc.status or status.HTTP_404_NOT_FOUND, exc.message) from None
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/netease/qr/{session}")
def netease_qr_poll(session: str, request: Request, db: DbSession, me: AdminUser) -> dict:
    """轮询扫码状态；803 时把 Cookie 落库，响应里只带账号信息。"""
    try:
        result = ng.qr_poll(session)
    except ng.NeteaseError as exc:
        raise HTTPException(exc.status or status.HTTP_400_BAD_REQUEST, exc.message) from None

    if result.get("code") != 803:
        return result

    cookie = result.pop("cookie", "")
    try:
        profile = ng.fetch_profile(cookie)
    except ng.NeteaseError:
        # 拿不到账号信息不影响用：Cookie 本身就是可用的
        profile = {"nickname": result.get("nickname") or ""}
    ng.save_cookie(db, cookie, profile)
    log_action(db, "admin_netease_login", me.id, {"nickname": profile.get("nickname", "")}, request)
    db.commit()
    return {"code": 803, "account": ng.status(db, verify=False)}


@router.delete("/netease")
def netease_logout(request: Request, db: DbSession, me: AdminUser) -> dict:
    """退出网易云：清掉后台保存的 Cookie（.env 里配的不动）。"""
    check_origin(request)
    result = ng.clear_cookie(db)
    log_action(db, "admin_netease_logout", me.id, result, request)
    db.commit()
    return {"result": result, "account": ng.status(db, verify=False)}


# --------------------------------------------------------------------------- #
# 看板娘 AI 默认接口（普通用户看不到，只有管理员能读能改）
# --------------------------------------------------------------------------- #
@router.get("/ai")
def ai_default_status(_: AdminUser) -> dict:
    """AI 默认接口的生效配置：密钥只回掩码，原文不出服务端。"""
    return {"config": ai_defaults.snapshot()}


@router.put("/ai")
def ai_default_save(payload: AdminAiDefaultIn, request: Request, db: DbSession, me: AdminUser) -> dict:
    """保存默认接口（存 system_settings，立即生效）。"""
    check_origin(request)
    ai_defaults.save(db, payload.model_dump(exclude_none=True, by_alias=True))
    config = ai_defaults.snapshot()
    log_action(
        db,
        "admin_ai_default_save",
        me.id,
        {
            "enabled": config["enabled"],
            "baseUrl": config["baseUrl"],
            "hasKey": config["hasKey"],
            "model": config["model"],
        },
        request,
    )
    db.commit()
    return {"config": config}


@router.delete("/ai")
def ai_default_clear(request: Request, db: DbSession, me: AdminUser) -> dict:
    """清掉后台覆盖，全部回到 .env。"""
    check_origin(request)
    ai_defaults.clear(db)
    log_action(db, "admin_ai_default_clear", me.id, {}, request)
    db.commit()
    return {"config": ai_defaults.snapshot()}


# --------------------------------------------------------------------------- #
# 歌词来源（网易云 / MC22 歌词库）
# --------------------------------------------------------------------------- #
@router.get("/lyrics")
def lyrics_status(_: AdminUser) -> dict:
    """歌词来源的生效配置。密钥与 Cookie 只回掩码，不回原文。"""
    return {"config": lyrics_config.snapshot()}


@router.put("/lyrics")
def lyrics_save(payload: AdminLyricsIn, request: Request, db: DbSession, me: AdminUser) -> dict:
    """保存歌词来源配置（存 system_settings，立即生效）。"""
    check_origin(request)
    config = lyrics_config.save(
        db,
        providers=list(payload.providers) if payload.providers is not None else None,
        api_base=payload.api_base,
        api_key=payload.api_key,
        api_cookie=payload.api_cookie,
        clear_key=payload.clear_key,
        clear_cookie=payload.clear_cookie,
    )
    log_action(
        db,
        "admin_lyrics_save",
        me.id,
        {
            "providers": config["providers"],
            "apiBase": config["apiBase"],
            "hasKey": config["hasKey"],
            "hasCookie": config["hasCookie"],
        },
        request,
    )
    db.commit()
    return {"config": config}


@router.post("/lyrics/test")
def lyrics_test(payload: AdminLyricsTestIn, request: Request, _: AdminUser) -> dict:
    """用「还没保存」的地址/密钥试搜一首；成功返回前几条结果。"""
    check_origin(request)
    started = time.perf_counter()
    try:
        items = lyrics_service.lyriclib_search(
            payload.query or "晴天",
            5,
            base=(payload.api_base or "").strip() or None,
            key=(payload.api_key or "").strip() or None,
            cookie=(payload.api_cookie or "").strip() or None,
            use_cache=False,
        )
    except lyrics_service.LyricsApiError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message or "连接失败") from None
    elapsed = int((time.perf_counter() - started) * 1000)
    return {
        "ok": True,
        "elapsedMs": elapsed,
        "total": len(items),
        "items": [
            {k: item.get(k, "") for k in ("title", "artist", "album")} for item in items[:3]
        ],
    }


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
