"""存储维护：无人引用文件的回收、失效快传的回收，以及每日定时清理。

「无人引用」指 ``blobs`` 表里还有记录、但 ``nodes`` 表已经没有任何节点指向它的文件。
正常路径下 ``release_blob`` 会在引用归零时连记录带磁盘一起删掉，但历史数据、并发写入、
被拉黑后又被清理的文件都会留下这种「只有记录、没人引用」的条目：它们永远用不上，
却实实在在占着磁盘。

定时清理由后台线程驱动：每分钟醒一次，到管理员设定的整点、且当天还没跑过才执行。
配置存在 ``system_settings`` 里（``auto_cleanup_*``），所以多进程/重启都不丢。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import db_failover
from . import quick_service as qs
from . import storage
from .config import settings
from .models import Blob, BlockedBlob, Node
from .settings_store import get_flag, get_int, get_value, set_value

log = logging.getLogger("uvicorn.error").getChild("cloudnetp.cleanup")

# 刚写入、还来不及登记的 blob 不碰：上传是「先落盘、再 retain」，中间有个毫秒级窗口，
# 手动清理留 1 小时，自动任务更保守留 24 小时。
KEEP_HOURS = 1
AUTO_KEEP_HOURS = 24

_started = False
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 概况
# --------------------------------------------------------------------------- #
def _referenced_shas(db: Session) -> set[str]:
    """所有被节点引用的哈希（含回收站与快传副本）。

    **回收站里的节点也算引用**：用户把文件删进回收站只是打了 ``deleted_at``，
    文件随时可能被还原，物理文件必须留着。清理不能只看「活跃节点」，
    否则管理员点一次「清理无人引用的文件」就会把所有人回收站里的文件一起带走。
    """
    rows = db.scalars(
        select(Node.blob_sha256).where(Node.blob_sha256.is_not(None)).distinct()
    ).all()
    return {sha for sha in rows if sha}


def _active_shas(db: Session) -> set[str]:
    """还在目录里（没进回收站）的节点引用的哈希。"""
    rows = db.scalars(
        select(Node.blob_sha256)
        .where(Node.blob_sha256.is_not(None), Node.deleted_at.is_(None))
        .distinct()
    ).all()
    return {sha for sha in rows if sha}


def _unreferenced(db: Session) -> tuple[list[tuple[str, int, int]], int, int]:
    """返回（无人引用的 (sha, 大小, 引用计数) 列表，被黑名单保护的数量，回收站保护的数量）。

    只取三列、不加载 ORM 对象：blobs 表可能有几十万行，这一页每次打开都要算一遍，
    全量实例化对象太浪费；真正要删的时候按 sha 单条 DELETE 就行。

    黑名单里的文件留着：举报审核页还要预览原件，删了就没法复核了。
    """
    referenced = _referenced_shas(db)
    active = _active_shas(db)
    blocked = {sha for sha in db.scalars(select(BlockedBlob.sha256)).all()}
    rows: list[tuple[str, int, int]] = []
    protected = 0
    trash_kept = 0
    for sha, size, ref_count in db.execute(
        select(Blob.sha256, Blob.size, Blob.ref_count)
    ).all():
        if sha in referenced:
            # 只被回收站引用的单独计数，方便在后台说明「没被清掉是因为还在回收站」
            if sha not in active:
                trash_kept += 1
            continue
        if sha in blocked:
            protected += 1
            continue
        rows.append((sha, int(size or 0), int(ref_count or 0)))
    return rows, protected, trash_kept


def storage_snapshot(db: Session) -> dict:
    """存储页顶部用的数字：物理文件、逻辑占用、快传占用、无人引用规模。

    这里只听数据库的：真正的清理会再看文件 mtime，页面不为了几个数字去扫全盘。
    """
    blob_count, blob_bytes = db.execute(
        select(func.count(Blob.sha256), func.coalesce(func.sum(Blob.size), 0))
    ).one()

    files = db.execute(
        select(func.count(Node.id), func.coalesce(func.sum(Node.size), 0)).where(
            Node.type == "file"
        )
    ).one()

    trash_count, trash_bytes = db.execute(
        select(func.count(Node.id), func.coalesce(func.sum(Node.size), 0)).where(
            Node.type == "file", Node.deleted_at.is_not(None)
        )
    ).one()

    quick_count, quick_bytes = db.execute(
        select(func.count(Node.id), func.coalesce(func.sum(Node.size), 0)).where(
            Node.type == "file", Node.quick_owned.is_(True)
        )
    ).one()

    unref, protected, trash_kept = _unreferenced(db)
    disk_used, disk_total = storage.disk_usage()

    return {
        "blobs": int(blob_count or 0),
        "blobBytes": int(blob_bytes or 0),
        "diskUsed": disk_used,
        "diskTotal": disk_total,
        "path": str(settings.STORAGE_DIR),
        "files": int(files[0] or 0),
        "fileBytes": int(files[1] or 0),
        "trashItems": int(trash_count or 0),
        "trashBytes": int(trash_bytes or 0),
        "quickFiles": int(quick_count or 0),
        "quickBytes": int(quick_bytes or 0),
        "unreferenced": {
            "count": len(unref),
            "bytes": int(sum(row[1] for row in unref)),
            "blocked": protected,
            "trashKept": trash_kept,
        },
    }


# --------------------------------------------------------------------------- #
# 无人引用的文件
# --------------------------------------------------------------------------- #
def scan_unreferenced(
    db: Session, *, apply: bool = False, keep_hours: int = KEEP_HOURS
) -> dict:
    """回收 `blobs` 里无人引用的记录与物理文件。默认只统计，`apply=True` 才真删。"""
    keep_hours = max(0, int(keep_hours))
    rows, protected, trash_kept = _unreferenced(db)

    now = time.time()
    doomed: list[tuple[str, int]] = []
    skipped_new = 0
    drift = 0
    for sha, size, ref_count in rows:
        if ref_count > 0:
            # 计数漂移：记录说有人用，节点表里却找不到。清掉是安全的，但值得单独报数
            drift += 1
        age = None
        try:
            path = storage.blob_path(sha)
            if path.is_file():
                age = now - path.stat().st_mtime
        except (ValueError, OSError):
            age = None
        if age is None:
            # 文件已经不在盘上，这条记录本身就是垃圾，不受「新文件保护」限制
            doomed.append((sha, size))
            continue
        if age < keep_hours * 3600:
            skipped_new += 1
            continue
        doomed.append((sha, size))

    total_bytes = int(sum(size for _, size in doomed))
    removed = 0
    freed = 0
    kept_by_refs = 0
    if apply and doomed:
        for sha, size in doomed:
            # 扫描到落手之间可能有人刚上传同内容文件、或把回收站条目还原了：
            # 删之前再查一次节点引用，宁可不删也不能删错
            if db.scalar(select(Node.id).where(Node.blob_sha256 == sha).limit(1)) is not None:
                kept_by_refs += 1
                continue
            db.execute(delete(Blob).where(Blob.sha256 == sha))
            storage.delete_blob(sha)
            removed += 1
            freed += size
        db.flush()

    return {
        "scanned": len(rows) + protected,
        "referenced": len(_referenced_shas(db)),
        "candidates": len(doomed),
        "candidatesBytes": total_bytes,
        "removed": removed,
        "freedBytes": freed,
        "protected": protected,
        "trashKept": trash_kept,
        "keptByRefs": kept_by_refs,
        "drift": drift,
        "skippedNew": skipped_new,
        "keepHours": keep_hours,
        "applied": apply,
    }


# --------------------------------------------------------------------------- #
# 自动清理
# --------------------------------------------------------------------------- #
def config(db: Session) -> dict:
    raw = get_value(db, "auto_cleanup_last_result")
    last_result = None
    if raw:
        try:
            last_result = json.loads(raw)
        except (TypeError, ValueError):
            last_result = None
    return {
        "enabled": get_flag(db, "auto_cleanup_enabled"),
        "hour": max(0, min(23, get_int(db, "auto_cleanup_hour"))),
        "orphans": get_flag(db, "auto_cleanup_orphans"),
        "quick": get_flag(db, "auto_cleanup_quick"),
        "lastRun": get_value(db, "auto_cleanup_last_run"),
        "lastResult": last_result,
    }


def save(
    db: Session, *, enabled: bool, hour: int, orphans: bool, quick: bool
) -> dict:
    hour = max(0, min(23, int(hour)))
    set_value(db, "auto_cleanup_enabled", "1" if enabled else "0")
    set_value(db, "auto_cleanup_hour", str(hour))
    set_value(db, "auto_cleanup_orphans", "1" if orphans else "0")
    set_value(db, "auto_cleanup_quick", "1" if quick else "0")
    return config(db)


def run(db: Session, *, orphans: bool | None = None, quick: bool | None = None) -> dict:
    """执行一次清理并记下时间与结果。开关缺省取后台配置，手动触发也走这里。"""
    cfg = config(db)
    do_orphans = cfg["orphans"] if orphans is None else bool(orphans)
    do_quick = cfg["quick"] if quick is None else bool(quick)

    result: dict = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "orphans": None,
        "quick": None,
    }
    if do_orphans:
        result["orphans"] = scan_unreferenced(db, apply=True, keep_hours=AUTO_KEEP_HOURS)
    if do_quick:
        result["quick"] = qs.cleanup_expired(db)

    set_value(db, "auto_cleanup_last_run", result["at"])
    set_value(
        db,
        "auto_cleanup_last_result",
        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
    )
    return result


def start_scheduler() -> None:
    """后台线程：每分钟看一眼是不是到了设定的整点。"""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, name="cloudnetp-cleanup", daemon=True).start()


def _loop() -> None:
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001 - 后台任务不能把服务带崩
            log.exception("自动清理失败")
        time.sleep(60)


def tick() -> None:
    """到点且当天没跑过就执行一次。拆出来方便测试直接调。"""
    session = db_failover.current_sessionmaker()()
    try:
        cfg = config(session)
        if not cfg["enabled"]:
            return
        now = datetime.now()
        if now.hour != cfg["hour"]:
            return
        if (cfg["lastRun"] or "")[:10] == now.date().isoformat():
            return
        with _lock:
            result = run(session)
            session.commit()
        orphans = (result.get("orphans") or {}).get("removed", 0)
        quick = result.get("quick") or {}
        log.info(
            "自动清理完成：无人引用 %s 个、快传 %s 个文件",
            orphans,
            quick.get("files", 0),
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
