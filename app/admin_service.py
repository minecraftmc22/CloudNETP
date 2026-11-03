"""管理后台服务层：统计、用户管理、审计查询、磁盘清理。

路由只做权限与参数校验，所有 SQL 集中在这里，方便后续加缓存或换分页方式。
"""

from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import and_, case, delete, func, or_, select
from sqlalchemy.orm import Session

from . import audit, db_failover, node_service as ns, storage
from .config import settings
from .models import (
    AuditLog,
    Blob,
    BlockedBlob,
    Node,
    Share,
    ShareReport,
    UploadSession,
    User,
    UserSetting,
    utcnow,
)
from .settings_store import all_values
from .user_service import iso

PAGE_SIZE_MAX = 100


# --------------------------------------------------------------------------- #
# 概览
# --------------------------------------------------------------------------- #
def overview(db: Session) -> dict:
    now = utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)

    # 一次往返拿齐用户维度指标：远程库每次往返都很贵，能合就合
    users_row = db.execute(
        select(
            func.count(User.id),
            func.coalesce(func.sum(case((User.banned.is_(True), 1), else_=0)), 0),
            func.coalesce(func.sum(case((User.role == 1, 1), else_=0)), 0),
            func.coalesce(func.sum(case((User.last_login_at >= today, 1), else_=0)), 0),
            func.coalesce(func.sum(case((User.created_at >= week_ago, 1), else_=0)), 0),
            func.coalesce(func.sum(User.used_bytes), 0),
            func.coalesce(func.max(User.quota_bytes), 0),
        )
    ).one()

    nodes_row = db.execute(
        select(
            func.coalesce(func.sum(case((Node.type == "file", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Node.type == "folder", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Node.deleted_at.is_not(None), 1), else_=0)), 0),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(Node.type == "file", Node.deleted_at.is_not(None)),
                            Node.size,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
        )
    ).one()

    shares_row = db.execute(
        select(
            func.count(Share.id),
            func.coalesce(func.sum(case((Share.revoked.is_(False), 1), else_=0)), 0),
        )
    ).one()

    blobs_row = db.execute(
        select(func.count(Blob.sha256), func.coalesce(func.sum(Blob.size), 0))
    ).one()

    top = db.execute(
        select(User.id, User.username, User.used_bytes, User.quota_bytes)
        .order_by(User.used_bytes.desc())
        .limit(5)
    ).all()

    recent = db.execute(
        select(AuditLog, User.username)
        .outerjoin(User, User.id == AuditLog.user_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(8)
    ).all()

    disk_used, disk_total = storage.disk_usage()

    return {
        "users": {
            "total": int(users_row[0] or 0),
            "banned": int(users_row[1] or 0),
            "admins": int(users_row[2] or 0),
            "activeToday": int(users_row[3] or 0),
            "newThisWeek": int(users_row[4] or 0),
            "usedBytes": int(users_row[5] or 0),
            "quotaBytes": int(users_row[6] or 0),
        },
        "nodes": {
            "files": int(nodes_row[0] or 0),
            "folders": int(nodes_row[1] or 0),
            "trashItems": int(nodes_row[2] or 0),
            "trashBytes": int(nodes_row[3] or 0),
        },
        "shares": {
            "total": int(shares_row[0] or 0),
            "active": int(shares_row[1] or 0),
        },
        "storage": {
            "blobs": int(blobs_row[0] or 0),
            "blobBytes": int(blobs_row[1] or 0),
            "diskUsed": disk_used,
            "diskTotal": disk_total,
            "path": str(settings.STORAGE_DIR),
        },
        "topUsers": [
            {
                "id": int(row[0]),
                "username": row[1],
                "usedBytes": int(row[2] or 0),
                "quotaBytes": int(row[3] or 0),
            }
            for row in top
        ],
        "recentLogs": [
            {
                "id": log.id,
                "action": log.action,
                "label": audit.describe(log.action)["label"],
                "tone": audit.describe(log.action)["tone"],
                "username": username,
                "detail": log.detail,
                "ip": log.ip,
                "createdAt": iso(log.created_at),
            }
            for log, username in recent
        ],
        "system": {
            "version": settings.VERSION,
            "appName": settings.APP_NAME,
            "database": "mysql" if settings.DATABASE_URL.startswith("mysql") else "sqlite",
            "dbMode": db_failover.mode_label(),
            "trashRetentionDays": _int_setting(db, "trash_retention_days"),
        },
    }


def _int_setting(db: Session, key: str) -> int:
    raw = all_values(db).get(key, "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# 用户
# --------------------------------------------------------------------------- #
def _clamp_page(page: int, page_size: int) -> tuple[int, int]:
    page = max(1, int(page or 1))
    page_size = min(max(1, int(page_size or 20)), PAGE_SIZE_MAX)
    return page, page_size


def list_users(
    db: Session, *, query: str = "", state: str = "all", page: int = 1, page_size: int = 20
) -> dict:
    page, page_size = _clamp_page(page, page_size)
    text = (query or "").strip()
    conds = []
    if text:
        if text.isdigit():
            conds.append(or_(User.username.like(f"%{text}%"), User.id == int(text)))
        else:
            conds.append(User.username.like(f"%{text}%"))
    if state == "banned":
        conds.append(User.banned.is_(True))
    elif state == "admin":
        conds.append(User.role == 1)
    elif state == "active":
        conds.append(User.banned.is_(False))

    total = int(db.scalar(select(func.count()).select_from(User).where(*conds)) or 0)
    rows = db.scalars(
        select(User)
        .where(*conds)
        .order_by(User.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    counts: dict[int, dict[str, int]] = {}
    shares: dict[int, int] = {}
    ids = [u.id for u in rows]
    if ids:
        for uid, files, trashed, live_bytes in db.execute(
            select(
                Node.user_id,
                func.coalesce(
                    func.sum(case((and_(Node.type == "file", Node.deleted_at.is_(None)), 1), else_=0)), 0
                ),
                func.coalesce(func.sum(case((Node.deleted_at.is_not(None), 1), else_=0)), 0),
                func.coalesce(
                    func.sum(
                        case(
                            (and_(Node.type == "file", Node.deleted_at.is_(None)), Node.size),
                            else_=0,
                        )
                    ),
                    0,
                ),
            )
            .where(Node.user_id.in_(ids))
            .group_by(Node.user_id)
        ).all():
            counts[int(uid)] = {
                "fileCount": int(files or 0),
                "trashCount": int(trashed or 0),
                "liveBytes": int(live_bytes or 0),
            }
        for uid, cnt in db.execute(
            select(Share.user_id, func.count())
            .where(Share.user_id.in_(ids), Share.revoked.is_(False))
            .group_by(Share.user_id)
        ).all():
            shares[int(uid)] = int(cnt)

    items = []
    for user in rows:
        stat = counts.get(user.id, {})
        items.append(
            {
                "id": user.id,
                "username": user.username,
                "role": user.role,
                "quotaBytes": int(user.quota_bytes),
                "usedBytes": int(user.used_bytes),
                "liveBytes": stat.get("liveBytes", 0),
                "fileCount": stat.get("fileCount", 0),
                "trashCount": stat.get("trashCount", 0),
                "shareCount": shares.get(user.id, 0),
                "banned": bool(user.banned),
                "createdAt": iso(user.created_at),
                "lastLoginAt": iso(user.last_login_at),
            }
        )
    return {"items": items, "total": total, "page": page, "pageSize": page_size}


def user_row(db: Session, user: User) -> dict:
    """单个用户的完整行数据（改配额后回传给前端，省一次列表请求）。"""
    files = int(
        db.scalar(
            select(func.count())
            .select_from(Node)
            .where(
                Node.user_id == user.id,
                Node.type == "file",
                Node.deleted_at.is_(None),
            )
        )
        or 0
    )
    trash = int(
        db.scalar(
            select(func.count())
            .select_from(Node)
            .where(Node.user_id == user.id, Node.deleted_at.is_not(None))
        )
        or 0
    )
    shares = int(
        db.scalar(
            select(func.count())
            .select_from(Share)
            .where(Share.user_id == user.id, Share.revoked.is_(False))
        )
        or 0
    )
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "quotaBytes": int(user.quota_bytes),
        "usedBytes": int(user.used_bytes),
        "liveBytes": int(user.used_bytes),
        "fileCount": files,
        "trashCount": trash,
        "shareCount": shares,
        "banned": bool(user.banned),
        "createdAt": iso(user.created_at),
        "lastLoginAt": iso(user.last_login_at),
    }


def settings_payload(db: Session) -> dict:
    values = all_values(db)

    def num(key: str) -> int:
        try:
            return int(values.get(key, "0"))
        except (TypeError, ValueError):
            return 0

    return {
        "siteName": values.get("site_name", settings.APP_NAME),
        "siteAnnouncement": values.get("site_announcement", ""),
        "allowRegister": str(values.get("allow_register", "1")).strip() in {"1", "true", "yes", "on"},
        "defaultQuotaBytes": num("default_quota_bytes"),
        "maxFileSizeBytes": num("max_file_size_bytes"),
        "chunkSizeBytes": num("chunk_size_bytes"),
        "trashRetentionDays": num("trash_retention_days"),
        # 找回密码兜底联系方式
        "contactEmail": values.get("contact_email", ""),
        "contactQq": values.get("contact_qq", ""),
        "runtime": {
            "version": settings.VERSION,
            "database": "mysql" if settings.DATABASE_URL.startswith("mysql") else "sqlite",
            "dbMode": db_failover.mode_label(),
            "storagePath": str(settings.STORAGE_DIR),
        },
    }


def admin_count(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(User).where(User.role == 1, User.banned.is_(False))
        )
        or 0
    )


def purge_user(db: Session, user: User) -> dict:
    """删除用户及其全部数据：节点、分享、上传会话、设置；blob 引用归零后自动清盘。"""
    uid = user.id
    shas = [
        s
        for s in db.scalars(
            select(Node.blob_sha256).where(Node.user_id == uid, Node.blob_sha256.is_not(None))
        ).all()
        if s
    ]
    node_count = int(
        db.scalar(select(func.count()).select_from(Node).where(Node.user_id == uid)) or 0
    )
    share_count = int(
        db.scalar(select(func.count()).select_from(Share).where(Share.user_id == uid)) or 0
    )

    # 分享先删：node_id 不是外键，留着会变成指向空气的记录
    db.execute(delete(Share).where(Share.user_id == uid))
    db.execute(delete(Node).where(Node.user_id == uid))
    for sha in set(shas):
        ns.release_blob(db, sha)
    db.execute(delete(UploadSession).where(UploadSession.user_id == uid))
    db.execute(delete(UserSetting).where(UserSetting.user_id == uid))
    db.delete(user)
    db.flush()
    return {
        "nodes": node_count,
        "shares": share_count,
        "blobs": len(set(shas)),
    }


# --------------------------------------------------------------------------- #
# 审计日志
# --------------------------------------------------------------------------- #
def list_logs(
    db: Session,
    *,
    page: int = 1,
    page_size: int = 30,
    action: str = "",
    user_id: int | None = None,
    query: str = "",
) -> dict:
    page, page_size = _clamp_page(page, page_size)
    conds = []
    if action:
        conds.append(AuditLog.action == action)
    if user_id:
        conds.append(AuditLog.user_id == user_id)
    text = (query or "").strip()
    if text:
        like = f"%{text}%"
        conds.append(
            or_(AuditLog.detail.like(like), AuditLog.action.like(like), User.username.like(like))
        )

    joined = select(AuditLog.id).outerjoin(User, User.id == AuditLog.user_id).where(*conds)
    total = int(db.scalar(select(func.count()).select_from(joined.subquery())) or 0)

    rows = db.execute(
        select(AuditLog, User.username)
        .outerjoin(User, User.id == AuditLog.user_id)
        .where(*conds)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    items = []
    for log, username in rows:
        meta = audit.describe(log.action)
        items.append(
            {
                "id": log.id,
                "action": log.action,
                "label": meta["label"],
                "tone": meta["tone"],
                "userId": log.user_id,
                "username": username,
                "detail": log.detail,
                "ip": log.ip,
                "createdAt": iso(log.created_at),
            }
        )
    return {"items": items, "total": total, "page": page, "pageSize": page_size}


def action_catalog(db: Session) -> list[dict]:
    counts = {
        action: int(cnt or 0)
        for action, cnt in db.execute(
            select(AuditLog.action, func.count()).group_by(AuditLog.action)
        ).all()
    }
    items = []
    for action, meta in audit.ACTIONS.items():
        items.append({"action": action, "count": counts.pop(action, 0), **meta})
    for action, cnt in counts.items():  # 历史遗留 / 未登记的 action 也列出来
        items.append({"action": action, "count": cnt, **audit.describe(action)})
    items.sort(key=lambda item: (-item["count"], item["action"]))
    return items


def purge_logs(db: Session, days: int) -> int:
    cutoff = utcnow() - timedelta(days=max(1, days))
    result = db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# 磁盘维护
# --------------------------------------------------------------------------- #
def blob_gc(db: Session, *, apply: bool = False, keep_hours: int = 1) -> dict:
    """找出 DB 里没有登记、且已放置一段时间的 blob 文件（上传中断的残骸）。

    默认只统计；apply=True 才真删，避免误删刚写入还没来得及登记的文件。
    """
    known = {sha for sha in db.scalars(select(Blob.sha256)).all()}
    disk_files = []
    if settings.blob_dir.is_dir():
        disk_files = [p for p in settings.blob_dir.rglob("*") if p.is_file()]

    now = time.time()
    orphans, old_orphans, orphan_bytes = [], [], 0
    for path in disk_files:
        if path.name in known:
            continue
        orphans.append(path)
        try:
            size = path.stat().st_size
            age = now - path.stat().st_mtime
        except OSError:
            continue
        orphan_bytes += size
        if age > keep_hours * 3600:
            old_orphans.append((path, size))

    tmp_removed = 0
    if settings.tmp_dir.is_dir():
        for path in settings.tmp_dir.glob("*.part"):
            try:
                stale = now - path.stat().st_mtime > 6 * 3600
            except OSError:
                continue
            if not stale:
                continue
            if apply:
                try:
                    path.unlink(missing_ok=True)
                    tmp_removed += 1
                except OSError:
                    continue
            else:
                tmp_removed += 1

    freed = 0
    removed = 0
    if apply:
        for path, size in old_orphans:
            try:
                path.unlink()
                removed += 1
                freed += size
            except OSError:
                continue

    return {
        "knownBlobs": len(known),
        "diskFiles": len(disk_files),
        "orphans": len(orphans),
        "orphanBytes": orphan_bytes,
        "removable": len(old_orphans),
        "removed": removed,
        "freedBytes": freed,
        "tmpStale": tmp_removed,
        "applied": apply,
    }


# --------------------------------------------------------------------------- #
# 分享举报审核
# --------------------------------------------------------------------------- #
def _iso_or_none(value):
    return value.isoformat() + "Z" if value else None


def list_reports(
    db: Session, *, status: str = "pending", page: int = 1, page_size: int = 20
) -> dict:
    page, page_size = _clamp_page(page, page_size)
    conds = []
    if status != "all":
        conds.append(ShareReport.status == status)
    total = db.scalar(select(func.count(ShareReport.id)).where(*conds)) or 0
    rows = db.scalars(
        select(ShareReport)
        .where(*conds)
        .order_by(ShareReport.created_at.desc(), ShareReport.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    if not rows:
        return {"items": [], "total": int(total), "page": page, "pageSize": page_size}

    # 批量补充：举报人昵称、处理人昵称、该哈希当前被拉黑状态、活跃分享引用数
    reporter_ids = {r.reporter_id for r in rows if r.reporter_id}
    handler_ids = {r.handled_by for r in rows if r.handled_by}
    users = {}
    if reporter_ids | handler_ids:
        for u in db.scalars(select(User).where(User.id.in_(reporter_ids | handler_ids))).all():
            users[u.id] = u.username
    shas = {r.blob_sha256 for r in rows if r.blob_sha256}
    blocked = set()
    if shas:
            blocked = set(db.scalars(select(BlockedBlob.sha256).where(BlockedBlob.sha256.in_(shas))).all())
    ref_counts = {}
    if shas:
        ref_rows = db.execute(
            select(Node.blob_sha256, func.count(Node.id))
            .where(Node.blob_sha256.in_(shas), Node.deleted_at.is_(None))
            .group_by(Node.blob_sha256)
        ).all()
        ref_counts = {sha: int(cnt) for sha, cnt in ref_rows}
    active_shares = {}
    share_ids = {r.share_id for r in rows}
    if share_ids:
        for sid, revoked, token in db.execute(
            select(Share.id, Share.revoked, Share.token).where(Share.id.in_(share_ids))
        ).all():
            active_shares[sid] = (not revoked, token)

    items = [
        {
            "id": r.id,
            "shareId": r.share_id,
            "token": r.token,
            "url": f"/s/{r.token}",
            "blobSha256": r.blob_sha256,
            "firstName": r.first_name,
            "size": int(r.size or 0),
            "isFolder": bool(r.is_folder),
            "reason": r.reason or "",
            "reporter": users.get(r.reporter_id),
            "reporterIp": r.reporter_ip,
            "status": r.status,
            "handledBy": users.get(r.handled_by),
            "handledAt": _iso_or_none(r.handled_at),
            "createdAt": _iso_or_none(r.created_at),
            "blocked": bool(r.blob_sha256 and r.blob_sha256 in blocked),
            "refCount": ref_counts.get(r.blob_sha256, 0) if r.blob_sha256 else 0,
            "shareActive": active_shares.get(r.share_id, (False, ""))[0],
        }
        for r in rows
    ]
    return {"items": items, "total": int(total), "page": page, "pageSize": page_size}


def approve_report(db: Session, report_id: int, admin: User) -> dict:
    """审核通过：单文件拉黑哈希（禁止分享/转存，不删文件）；文件夹撤销该分享。"""
    report = db.get(ShareReport, report_id)
    if not report:
        raise ValueError("举报不存在")
    if report.status != "pending":
        raise ValueError("该举报已处理")
    report.status = "approved"
    report.handled_by = admin.id
    report.handled_at = utcnow()

    blocked_now = False
    if report.blob_sha256:
        exists = db.get(BlockedBlob, report.blob_sha256)
        if not exists:
            db.add(
                BlockedBlob(
                    sha256=report.blob_sha256,
                    reason=report.reason or report.first_name,
                    blocked_by=admin.id,
                )
            )
        blocked_now = True
    # 对应分享立即失效
    share = db.get(Share, report.share_id)
    if share:
        share.revoked = True
    return {"blocked": blocked_now, "sha256": report.blob_sha256}


def reject_report(db: Session, report_id: int, admin: User) -> None:
    report = db.get(ShareReport, report_id)
    if not report:
        raise ValueError("举报不存在")
    if report.status != "pending":
        raise ValueError("该举报已处理")
    report.status = "rejected"
    report.handled_by = admin.id
    report.handled_at = utcnow()


def unblock_blob(db: Session, sha256: str, admin: User) -> bool:
    row = db.get(BlockedBlob, sha256)
    if not row:
        return False
    db.delete(row)
    return True
