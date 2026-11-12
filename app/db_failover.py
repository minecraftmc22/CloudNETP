"""数据库容灾：主库不可用时自动降级到次库，恢复后自动回迁。

角色划分（主库 / 次库都可以是 MySQL 或 SQLite，次库可以「不使用」）：
- primary   —— 主库，连接串由「主库类型」决定（MySQL 用 mysql_url，SQLite 用本地库文件）
- secondary —— 次库，同上；选「不使用」就等于关掉整套容灾，只跑主库
- 镜像      —— 主库可用时，周期把数据复制进次库（副本）
- 降级      —— 主库探测失败，新请求全部改走次库，文件本体在磁盘不受影响
- 回迁      —— 主库恢复后，把次库（降级期间的最新真相）逐行覆盖回主库，成功才切回

设计取舍（重要）：
- 按「单实例部署」设计：切换期间没有其它进程在写主库，回迁才能按副本覆盖。
  多实例部署请把次库设为「不使用」。
- 回迁失败时保持次库模式继续服务，下一轮探测再试，不会丢服务。
- 切换瞬间正在飞行的请求可能失败一次，用户重试即可（新请求已经走新库）。
- 镜像是近实时的：主库突然挂掉时，副本最多落后 DB_MIRROR_INTERVAL 秒。
- audit_logs 只镜像最近 MIRROR_AUDIT_LIMIT 条，避免副本库无限膨胀；
  回迁时整表按主键 upsert（只增改、不删除），主库里的旧审计日志不会丢。

同步核心 mirror_data / promote_data 是跨方言纯函数（拉主键集合 → 分 INSERT/UPDATE
批量执行），所以两个方向对 MySQL / SQLite 都成立，也方便用两个 SQLite 库做测试。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from sqlalchemy import Engine, and_, bindparam, create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from . import db_roles
from .config import ENV_FILE, refresh_db_settings, settings

log = logging.getLogger("uvicorn.error").getChild("cloudnetp.db")

MODE_PRIMARY = "primary"
MODE_FALLBACK = "fallback"

# audit_logs 镜像条数上限（按 id 倒序取最新）
MIRROR_AUDIT_LIMIT = 5000

# 同步顺序即外键依赖顺序：users → user_settings → blobs → nodes → audio_prefs → ...
_SYNC_TABLES = (
    "users",
    "user_settings",
    "blobs",
    "nodes",
    "audio_prefs",
    "upload_sessions",
    "upload_chunks",
    "shares",
    "share_reports",
    "blocked_blobs",
    "system_settings",
    "audit_logs",
)

_lock = threading.Lock()
_mode = MODE_PRIMARY
_fallback_engine: Engine | None = None
_fallback_session: sessionmaker | None = None
_probe_engine: Engine | None = None
_last_mirror = 0.0
_last_switch_at = 0.0
_last_switch_reason = ""
# 管理员在后台手动切到 SQLite：暂停「主库一恢复就自动回切」，
# 否则他刚点完 30 秒就被看门狗改回去，会以为按钮没生效（重启进程即解除）
_hold_fallback = False
_watchdog: threading.Thread | None = None
_started = False
# 探活结果缓存：管理页轮询时不必每次都真连一次（默认 8 秒内复用）
_probe_cache: dict[str, Any] = {"at": 0.0, "ok": False, "ms": 0}

# fallback 库里的「可信副本」标记：镜像成功才写。回迁前靠它判断副本来历
MARK_KEY = "__failover_mirror_at"
# 「降级期间有写入」标记：切到 SQLite 时写，成功回迁后清。进程在降级期间重启时靠它
# 判断 fallback 里有没有比主库新的数据，避免启动镜像把降级期间的写入覆盖掉
DIRTY_KEY = "__failover_dirty"


# ---------------------------------------------------------------- 引擎与会话

def _make_secondary_engine() -> Engine | None:
    """次库引擎；选「不使用」时返回 None。SQLite 走 WAL + busy 超时，MySQL 走带超时的连接池。"""
    url = settings.secondary_db_url
    if not url:
        return None
    if url.startswith("sqlite"):
        path = settings.sqlite_spare_path
        path.parent.mkdir(parents=True, exist_ok=True)
    from .db import build_engine

    return build_engine(url)


def fallback_engine() -> Engine | None:
    """次库引擎（老名字 fallback）。没设置次库时返回 None。

    注意：调用方要先判断 None，别再假设「一定有本地副本」。
    """
    global _fallback_engine, _fallback_session
    if _fallback_engine is None and settings.secondary_db_url:
        _fallback_engine = _make_secondary_engine()
        if _fallback_engine is not None:
            _fallback_session = sessionmaker(
                bind=_fallback_engine, autoflush=False, expire_on_commit=False, class_=Session
            )
    return _fallback_engine


def secondary_kind() -> str:
    return settings.secondary_role


def mode() -> str:
    return _mode


def mode_label() -> str:
    """给 /api/meta 用的短标签。"""
    return MODE_FALLBACK if _mode == MODE_FALLBACK else MODE_PRIMARY


def _kind_label(kind: str) -> str:
    return "MySQL" if kind == "mysql" else "SQLite"


def active_db_label() -> str:
    """日志里展示的当前库标识。"""
    if _mode == MODE_FALLBACK:
        url = settings.secondary_db_url
        return url.split("@")[-1] if url else "-"
    url = settings.primary_db_url
    return settings.sqlite_main_path.as_posix() if url.startswith("sqlite") else url.split("@")[-1]


def current_sessionmaker() -> sessionmaker:
    """当前生效模式的会话工厂（get_db 每次调用取一次，天然支持热切换）。"""
    with _lock:
        if _mode == MODE_FALLBACK and _fallback_session is not None:
            return _fallback_session
    from .db import SessionLocal

    return SessionLocal


def _switch(to: str, reason: str) -> None:
    global _mode, _last_switch_at, _last_switch_reason
    with _lock:
        if _mode == to:
            return
        _mode = to
        _last_switch_at = time.time()
        _last_switch_reason = reason
    if to == MODE_FALLBACK:
        log.warning("数据库降级 → SQLite 回退（%s）。期间数据写入本地，主库恢复后自动回迁", reason)
        _mark_dirty()
    else:
        log.info("数据库切回 → 主库（%s）", reason)
        _clear_dirty()


def _switch_since() -> str | None:
    """最近一次切换的时间（本地时区 ISO），从没切过返回 None。"""
    if not _last_switch_at:
        return None
    return datetime.fromtimestamp(_last_switch_at).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 探测

def probe(eng: Engine | None = None) -> bool:
    """SELECT 1 探活。默认走探活专用小连接池（3 秒超时），不被主库长握手拖死。"""
    target = eng if eng is not None else probe_engine()
    try:
        with target.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def probe_now() -> tuple[bool, int]:
    """真探一次，返回 (是否可用, 耗时毫秒)，顺手把结果写进缓存。"""
    start = time.perf_counter()
    ok = probe()
    ms = int((time.perf_counter() - start) * 1000)
    _probe_cache.update({"at": time.time(), "ok": ok, "ms": ms})
    return ok, ms


def probe_cached(ttl: float = 8.0) -> tuple[bool, int]:
    """探活带缓存：管理页刷新时不必每点一次都排一次 3 秒超时。"""
    if time.time() - float(_probe_cache.get("at") or 0) < ttl:
        return bool(_probe_cache["ok"]), int(_probe_cache["ms"])
    return probe_now()


def _reset_probe_engine() -> None:
    """连接串换了之后，探活池也得重建，否则还在探旧地址。"""
    global _probe_engine
    old, _probe_engine = _probe_engine, None
    if old is not None:
        try:
            old.dispose()
        except Exception:
            pass


def probe_engine() -> Engine:
    """探活专用：独立连接池 + 短超时。主库是 SQLite 时直接用主引擎。"""
    global _probe_engine
    from .db import engine

    if settings.is_sqlite:
        return engine
    if _probe_engine is None:
        url = settings.primary_db_url
        timeout = max(1, min(3, settings.DB_CONNECT_TIMEOUT))
        if url.startswith("sqlite"):
            # 主库角色是 MySQL 但连接串是 SQLite（只可能出现在测试或半配置状态）：
            # 别把 MySQL 的 timeout 参数塞给 sqlite3，否则会直接报「不需要的关键字参数」
            _probe_engine = create_engine(
                url, future=True, connect_args={"check_same_thread": False, "timeout": timeout}
            )
        else:
            _probe_engine = create_engine(
                url,
                future=True,
                pool_size=1,
                max_overflow=0,
                pool_pre_ping=True,
                pool_recycle=1800,
                connect_args={
                    "connect_timeout": timeout,
                    "read_timeout": timeout + 2,
                    "write_timeout": timeout + 2,
                },
            )
    return _probe_engine


# ---------------------------------------------------------------- 同步核心

def _fetch_rows(src: Engine, table: str, limit_latest: int | None = None) -> list[dict[str, Any]]:
    """拉源表全量行（audit_logs 可只取最新 N 条）。"""
    from .db import Base

    md = Base.metadata.tables[table]
    cols = [c.name for c in md.columns]
    q = select(*[md.c[name] for name in cols])
    if limit_latest is not None:
        pk = md.primary_key.columns[0]
        q = q.order_by(pk.desc()).limit(limit_latest)
    with src.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(q)]
    rows.reverse()  # 回到升序，插入顺序自然
    if table == "system_settings":
        # 内部标记键（__ 开头）不外传，别把容灾元数据写进业务配置
        rows = [r for r in rows if not str(r.get("k", "")).startswith("__")]
    return rows


def _upsert_rows(dst: Engine, table: str, rows: list[dict[str, Any]], insert_only: bool = False) -> int:
    """按主键 upsert：目标里已有的行 UPDATE、缺失的行 INSERT。跨方言。

    insert_only=True 时只补缺失行、绝不覆盖已存在的行（用于「来源不明的副本」回迁，
    宁可少迁也不能拿一份空库/临时库把主库覆盖掉）。
    """
    if not rows:
        return 0
    from .db import Base

    md = Base.metadata.tables[table]
    pk_cols = [c.name for c in md.primary_key]
    with dst.begin() as conn:
        existing: set[tuple] = set()
        pk_sel = select(*[md.c[name] for name in pk_cols])
        for r in conn.execute(pk_sel):
            existing.add(tuple(r))
        to_insert: list[dict[str, Any]] = []
        to_update: list[dict[str, Any]] = []
        for row in rows:
            key = tuple(row[name] for name in pk_cols)
            (to_update if key in existing else to_insert).append(row)
        if to_insert:
            conn.execute(md.insert(), to_insert)
        if to_update and not insert_only:
            # WHERE pk = :_pk_x 的 executemany 批量更新；_pk_* 只用于 WHERE，不进 SET
            stmt = md.update().where(
                and_(*[md.c[name] == bindparam(f"_pk_{i}") for i, name in enumerate(pk_cols)])
            )
            params = [
                {**row, **{f"_pk_{i}": row[name] for i, name in enumerate(pk_cols)}}
                for row in to_update
            ]
            conn.execute(stmt, params)
    return len(to_insert) + (0 if insert_only else len(to_update))


def _sync_all(src: Engine, dst: Engine, insert_only: bool = False) -> int:
    """src → dst 全表同步，返回同步总行数。表顺序 = 外键依赖顺序。"""
    total = 0
    for table in _SYNC_TABLES:
        limit = MIRROR_AUDIT_LIMIT if table == "audit_logs" else None
        total += _upsert_rows(dst, table, _fetch_rows(src, table, limit), insert_only)
    return total


def mirror_data(src: Engine, dst: Engine) -> int:
    """主库 → SQLite 镜像（跨方言纯函数，便于测试）。"""
    return _sync_all(src, dst)


def promote_data(src: Engine, dst: Engine, insert_only: bool = False) -> int:
    """SQLite → 主库 回迁（跨方言纯函数，便于测试）。"""
    return _sync_all(src, dst, insert_only)


# ---------------------------------------------------------------- 运行期动作

def _mark_mirrored(rows: int) -> None:
    """在次库里记下「何时从主库镜像成功」，回迁前据此判断副本是否可信。"""
    value = f"{time.strftime('%Y-%m-%dT%H:%M:%S')}|{rows}|{active_db_label()}"
    _set_flag(MARK_KEY, value)


def mirrored_before() -> bool:
    """次库是否是「从主库镜像来的可信副本」。"""
    return _flag_exists(MARK_KEY)


def _flag_exists(key: str) -> bool:
    from .db import Base

    eng = fallback_engine()
    if eng is None:
        return False
    table = Base.metadata.tables["system_settings"]
    try:
        with eng.connect() as conn:
            row = conn.execute(select(table.c.v).where(table.c.k == key)).first()
        return row is not None
    except Exception:
        return False


def _set_flag(key: str, value: str) -> None:
    from .db import Base

    eng = fallback_engine()
    if eng is None:
        return
    table = Base.metadata.tables["system_settings"]
    try:
        with eng.begin() as conn:
            updated = conn.execute(table.update().where(table.c.k == key), {"v": value}).rowcount
            if not updated:
                conn.execute(table.insert(), [{"k": key, "v": value}])
    except Exception as exc:  # 标记失败不影响主流程
        log.warning("写容灾标记 %s 失败：%s", key, exc)


def _mark_dirty() -> None:
    """降级期间的数据可能有主库没有的写入，记一笔，进程重启后据此先回迁再镜像。"""
    _set_flag(DIRTY_KEY, time.strftime("%Y-%m-%dT%H:%M:%S"))


def _clear_dirty() -> None:
    from .db import Base

    eng = fallback_engine()
    if eng is None:
        return
    table = Base.metadata.tables["system_settings"]
    try:
        with eng.begin() as conn:
            conn.execute(table.delete().where(table.c.k == DIRTY_KEY))
    except Exception as exc:
        log.warning("清除降级标记失败：%s", exc)


def dirty() -> bool:
    """上次是否在降级模式下退出（次库里可能有未回迁的数据）。"""
    return _flag_exists(DIRTY_KEY)


def mirror_once(reason: str) -> int:
    """主库 → 次库 镜像一次。主库不可用会抛异常，由调用方决定是否降级。"""
    from .db import engine

    dst = fallback_engine()
    if dst is None:
        return 0
    n = mirror_data(engine, dst)
    _mark_mirrored(n)
    log.info("主库 → 次库 镜像完成（%s），同步 %s 行", reason, n)
    return n


def promote_once() -> int:
    """次库 → 主库 回迁一次。失败抛异常，调用方保持次库模式。"""
    from .db import engine

    src = fallback_engine()
    if src is None:
        return 0
    trusted = mirrored_before()
    if not trusted:
        log.warning(
            "fallback 库从未成功镜像过主库（可能是新建的空副本），本次只补缺失行、不覆盖主库已有数据"
        )
    n = promote_data(src, engine, insert_only=not trusted)
    log.info("次库 → 主库 回迁完成，同步 %s 行（可信副本=%s）", n, trusted)
    return n


def _promote_and_switch(reason: str) -> int:
    """回迁成功才切回主库，返回回迁行数。回迁失败把异常抛给调用方（继续保持次库）。"""
    promoted = promote_once()
    _switch(MODE_PRIMARY, reason)
    try:
        mirror_once("回切后立即")  # 让次库立刻成为最新副本
    except Exception as exc:
        log.warning("回切后立即镜像失败：%s", exc)
    return promoted


# ---------------------------------------------------------------- 管理页接口

def _mask_url(url: str) -> str:
    """连接串脱敏：密码换成 ****，其余保留，方便管理员核对是不是连的这台机器。"""
    if "@" not in url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.password:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    user = quote(parts.username or "", safe="")
    return urlunsplit((parts.scheme, f"{user}:****@{host}", parts.path, parts.query, parts.fragment))


def _fallback_bytes() -> int:
    """本地 SQLite 库实际占盘：WAL 模式下数据可能还在 -wal 里，一起算上。"""
    path = settings.sqlite_spare_path
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += path.with_name(path.name + suffix).stat().st_size
        except OSError:
            continue
    return total


def _stamp(ts: float) -> str | None:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


def describe() -> dict[str, Any]:
    """管理页「数据库」卡片的状态快照，不含任何明文密钥。"""
    from .db import engine

    reachable, latency = probe_cached()
    primary_sqlite = settings.primary_role == "sqlite"
    in_fallback = _mode == MODE_FALLBACK
    sqlite_path = settings.sqlite_spare_path
    roles = settings.roles
    secondary_url = settings.secondary_db_url
    secondary_kind_ = settings.secondary_role

    return {
        "mode": _mode,
        # 前端自己拼「主库 / 次库 · MySQL」这类文案，后端只给结构化数据，方便多语言
        "active": {
            "role": _mode,
            "kind": settings.secondary_role if in_fallback else settings.primary_role,
        },
        "switchedAt": _stamp(_last_switch_at),
        "switchReason": _last_switch_reason,
        "roles": {
            "primary": roles["primary"],
            "secondary": roles["secondary"],
            "source": roles["source"],
            "mysqlConfigured": bool(settings.mysql_url),
            "savedFile": str(db_roles.path_for(settings.STORAGE_DIR)),
        },
        "primary": {
            "url": _mask_url(settings.primary_db_url),
            "dialect": engine.dialect.name,
            "isSqlite": primary_sqlite,
            # 主库选 SQLite 时把文件位置也报出来，管理员好确认数据落在哪
            "path": settings.sqlite_main_path.as_posix() if primary_sqlite else "",
            "reachable": reachable,
            "latencyMs": latency,
            "connectTimeout": settings.DB_CONNECT_TIMEOUT,
            "queryTimeout": settings.DB_TIMEOUT,
        },
        "fallback": {
            "enabled": bool(secondary_url),
            "kind": secondary_kind_,
            "url": _mask_url(secondary_url) if secondary_url else "",
            # 只有本地 SQLite 才有「文件路径 / 占盘」这些概念
            "path": str(sqlite_path) if secondary_kind_ == "sqlite" else "",
            "exists": sqlite_path.exists() if secondary_kind_ == "sqlite" else False,
            "bytes": _fallback_bytes() if secondary_kind_ == "sqlite" else 0,
            "mirrored": mirrored_before() if secondary_url else False,
            "dirty": dirty() if secondary_url else False,
        },
        "watchdog": {
            # 线程可能还活着但间隔被改成 0（手动刷新配置），对外一律算「没在自动切换」
            "running": bool(_watchdog and _watchdog.is_alive()) and settings.DB_PROBE_INTERVAL > 0,
            "probeInterval": settings.DB_PROBE_INTERVAL,
            "mirrorInterval": settings.DB_MIRROR_INTERVAL,
            "held": _hold_fallback,
            "lastMirrorAt": _stamp(_last_mirror),
        },
        "configFile": str(ENV_FILE),
    }


def _rebuild_fallback_engine() -> None:
    """丢掉次库连接池重建（类型或路径可能刚被改过）。"""
    global _fallback_engine, _fallback_session
    old = _fallback_engine
    _fallback_engine, _fallback_session = None, None
    if old is not None:
        try:
            old.dispose()
        except Exception:
            pass
    eng = fallback_engine()
    if eng is not None:
        from .db import ensure_schema

        ensure_schema(eng)


def _mask_value(key: str, value: Any) -> str:
    """配置变更项脱敏：连接串去密码，密码只报「有/无」。"""
    if key == "DATABASE_URL":
        return _mask_url(str(value))
    if key == "DB_PASSWORD":
        return "****" if value else ""
    return str(value)


def _ensure_primary_schema() -> None:
    """换了主库地址时结构可能不齐，补建一次（失败只告警，不阻断）。"""
    from .db import engine, ensure_schema

    try:
        ensure_schema(engine)
    except Exception as exc:
        log.warning("主库表结构检查失败：%s", exc)


def reload_config() -> dict[str, Any]:
    """重读 .env 的数据库配置 → 重建连接池 → 探活 → 落回可用的库。

    返回变更项与落地结果，前端据此提示（例如「连接串已更换，已切回主库」）。
    与「手动重连」的区别：连不上不报错，按容灾规则落回次库，保证站点先活着。
    """
    from . import db as db_module

    changes = refresh_db_settings()
    url_changed = db_module.rebuild_engine()
    _rebuild_fallback_engine()
    _reset_probe_engine()
    reachable, latency = probe_now()

    landed = _mode
    promoted = 0
    if reachable:
        _ensure_primary_schema()
        if _mode == MODE_FALLBACK and not _hold_fallback:
            try:
                promoted = _promote_and_switch("刷新配置后主库可用")
                landed = MODE_PRIMARY
            except Exception as exc:
                log.warning("刷新配置后回迁失败，继续用次库：%s", exc)
                landed = MODE_FALLBACK
    elif settings.secondary_enabled and _mode == MODE_PRIMARY:
        _switch(MODE_FALLBACK, "刷新配置时主库不可用")
        landed = MODE_FALLBACK
    elif not settings.secondary_enabled and _mode == MODE_FALLBACK:
        # 次库被关掉了却还跑在次库上：只能回到主库（主库不可用就只能等它恢复）
        log.warning("次库已取消设置，但主库当前不可用，暂时无法切回")
        landed = MODE_FALLBACK

    # 间隔从 0 改成正数时，把启动时就跳过的看门狗补起来（已在跑则直接返回）
    if settings.DB_PROBE_INTERVAL > 0:
        start_watchdog()

    return {
        "changes": {
            key: {"from": _mask_value(key, old), "to": _mask_value(key, new)}
            for key, (old, new) in changes.items()
        },
        "urlChanged": url_changed,
        "reachable": reachable,
        "latencyMs": latency,
        "landed": landed,
        "migratedRows": promoted,
        "roles": settings.roles,
    }


def apply_roles(primary: str, secondary: str) -> dict[str, Any]:
    """管理页「主库 / 次库」选择：校验 → 迁移数据 → 切换 → 落盘。

    顺序是刻意的：先把目标库建起来并探活，探不通就不动任何东西；
    探通以后把**当前正在服务的库**的数据整体复制进新主库（换库不能把数据丢在原地），
    最后才把角色写进 roles.json 并切过去。任何一步失败都回滚到原来的角色。
    """
    global _hold_fallback

    from . import db as db_module

    primary, secondary = db_roles.normalize(primary, secondary)
    if not primary:
        raise ValueError("主库只能选 MySQL 或 SQLite")
    if not secondary:
        secondary = "none"
    if "mysql" in (primary, secondary) and not settings.mysql_url:
        raise RuntimeError(
            "还没配置 MySQL 连接信息：在 .env 里填 DATABASE_URL，或填好 DB_HOST / DB_USER / DB_PASSWORD / DB_NAME 再试"
        )

    old_roles = dict(settings.roles)
    old_primary_url = db_module.engine_url()
    old_secondary_url = settings.secondary_db_url
    old_fallback = fallback_engine()
    # 当前真正在服务的库（降级中就是次库），换主库时以它为数据源
    source_url = old_secondary_url if _mode == MODE_FALLBACK else old_primary_url
    source_engine = old_fallback if _mode == MODE_FALLBACK and old_fallback is not None else db_module.engine

    db_roles.write(settings.STORAGE_DIR, primary, secondary)
    new_primary_url = settings.primary_db_url
    new_secondary_url = settings.secondary_db_url

    try:
        if new_primary_url == old_primary_url:
            db_module.engine.dispose()  # 连接串没变，只丢一次连接池
        else:
            db_module.rebuild_engine()
        _reset_probe_engine()
        reachable, latency = probe_now()
        if not reachable:
            raise RuntimeError(
                f"{'MySQL' if primary == 'mysql' else 'SQLite'} 主库连不上，仍在使用当前库；检查连接信息与网络后重试"
            )
    except Exception:
        # 探不通就把角色退回去，别让「选了一个连不上的库」把站点卡死
        db_roles.write(settings.STORAGE_DIR, old_roles["primary"], old_roles["secondary"])
        db_module.rebuild_engine()
        _reset_probe_engine()
        raise

    moved = 0
    if source_url != new_primary_url:
        from .db import ensure_schema

        ensure_schema(db_module.engine)
        moved = _sync_all(source_engine, db_module.engine)
        log.info("换主库：%s → %s，迁移 %s 行", _mask_url(source_url), _mask_url(new_primary_url), moved)

    # 次库换了类型/地址：重建连接池并补结构
    if new_secondary_url != old_secondary_url:
        _rebuild_fallback_engine()

    _hold_fallback = False
    _switch(MODE_PRIMARY, f"管理页选择主库：{'MySQL' if primary == 'mysql' else 'SQLite'}")
    mirrored = 0
    if new_secondary_url:
        try:
            mirrored = mirror_once("换库后")
        except Exception as exc:  # 副本没镜像上不影响主库可用，下一轮会补
            log.warning("换库后镜像次库失败：%s", exc)

    return {
        "primary": primary,
        "secondary": secondary,
        "previous": {"primary": old_roles["primary"], "secondary": old_roles["secondary"]},
        "primaryUrlChanged": new_primary_url != old_primary_url,
        "secondaryUrlChanged": new_secondary_url != old_secondary_url,
        "latencyMs": latency,
        "migratedRows": moved,
        "mirroredRows": mirrored,
    }


def reconnect(target: str) -> dict[str, Any]:
    """管理页手动重连：按最新配置重建连接并切到指定库。

    失败抛 RuntimeError，当前服务库保持不变（宁可让管理员看到报错，也不能半路换库）。
    """
    global _hold_fallback
    if target not in (MODE_PRIMARY, MODE_FALLBACK):
        raise ValueError("未知的连接目标")

    from . import db as db_module

    if target == MODE_PRIMARY:
        # 顺手把 .env 改过的连接串生效，省得「改了配置还得重启」
        changed = db_module.rebuild_engine()
        _reset_probe_engine()
        reachable, latency = probe_now()
        if not reachable:
            raise RuntimeError(
                "主库连不上，仍在使用当前库。检查数据库地址、进程和网络后重试"
            )
        _ensure_primary_schema()
        promoted = _promote_and_switch("管理页手动重连主库") if _mode == MODE_FALLBACK else 0
        _hold_fallback = False
        return {
            "target": MODE_PRIMARY,
            "urlChanged": changed,
            "latencyMs": latency,
            "migratedRows": promoted,
        }

    if not settings.secondary_enabled:
        raise RuntimeError("还没有设置次库，先在「主库 / 次库」里选一个再切")
    _rebuild_fallback_engine()
    mirrored = 0
    if _mode == MODE_PRIMARY and probe():
        try:
            mirrored = mirror_once("手动切换前")
        except Exception as exc:
            raise RuntimeError(f"切换前镜像失败，仍在使用主库：{exc}") from None
    _switch(MODE_FALLBACK, "管理页手动切到次库")
    _hold_fallback = True  # 手动选择优先：不自动回切，等管理员点「重新连接主库」
    return {
        "target": MODE_FALLBACK,
        "urlChanged": False,
        "latencyMs": 0,
        "migratedRows": 0,
        "mirroredRows": mirrored,
    }


# ---------------------------------------------------------------- 看门狗

def _tick() -> None:
    global _last_mirror

    if _mode == MODE_PRIMARY:
        if not settings.secondary_enabled:
            return  # 没设置次库：既没地方降级也没东西可镜像，不必探活
        reachable, _ms = probe_now()
        if not reachable:
            _switch(MODE_FALLBACK, "主库探测失败")
            return
        if 0 < settings.DB_MIRROR_INTERVAL <= time.time() - _last_mirror:
            try:
                mirror_once("周期")
                _last_mirror = time.time()
            except Exception as exc:
                # 镜像失败不代表主库挂了（可能是瞬时抖动），下轮再试
                log.warning("主库 → 次库 镜像失败，下轮重试：%s", exc)
    else:
        if _hold_fallback:
            return  # 管理员手动停在次库：不自动回切，重启进程后恢复自动
        if not settings.secondary_enabled:
            # 次库被取消了，但现在正跑在它上面：只能等主库恢复再切回
            return
        reachable, _ms = probe_now()
        if not reachable:
            return
        # 主库回来了：先回迁，成功才切回；失败保持次库，下轮重试
        try:
            promote_once()
        except Exception as exc:
            log.warning("回迁主库失败，继续使用次库，下轮重试：%s", exc)
            return
        _switch(MODE_PRIMARY, "主库恢复，回迁完成")
        _last_mirror = time.time()
        try:
            mirror_once("回切后立即")  # 让次库立刻成为最新副本
        except Exception as exc:  # pragma: no cover - 刚探测成功又失败，罕见
            log.warning("回切后立即镜像失败：%s", exc)


def _watchdog_loop() -> None:  # pragma: no cover - 线程主循环
    while True:
        time.sleep(max(5, settings.DB_PROBE_INTERVAL))
        if settings.DB_PROBE_INTERVAL <= 0:
            # 运行中把间隔改成 0（刷新配置）：线程留着空转，改回正数就自动恢复工作
            continue
        try:
            _tick()
        except Exception:
            log.exception("数据库看门狗异常")


def start_watchdog() -> None:
    global _watchdog
    if settings.DB_PROBE_INTERVAL <= 0:
        log.info("DB_PROBE_INTERVAL=0，运行期不自动切换，仅启动时选库")
        return
    if _watchdog and _watchdog.is_alive():
        return
    _watchdog = threading.Thread(target=_watchdog_loop, name="db-watchdog", daemon=True)
    _watchdog.start()
    log.info("数据库看门狗已启动：每 %ss 探测主库，镜像间隔 %ss", settings.DB_PROBE_INTERVAL, settings.DB_MIRROR_INTERVAL)


# ---------------------------------------------------------------- 启动

def startup() -> str:
    """lifespan 调用：准备主/次库结构、选定初始模式、起看门狗、镜像首份副本。

    返回初始模式（primary / fallback）。
    主库不可用且没设置次库时抛原异常，保持与旧版一致的行为。
    """
    global _last_mirror, _started
    from .db import ensure_schema, engine

    roles = settings.roles
    log.info(
        "数据库角色：主库 %s%s（来源 %s）",
        _kind_label(roles["primary"]),
        "" if roles["secondary"] == "none" else f"，次库 {_kind_label(roles['secondary'])}",
        {"admin": "管理后台", "env": ".env", "default": "默认推断"}.get(roles["source"], roles["source"]),
    )

    # 启动重试：远程库常见瞬时抖动（Tailscale 中继切换），一次失败就降级太激进
    primary_error: Exception | None = None
    attempts = 3
    for i in range(attempts):
        try:
            ensure_schema(engine)
            primary_error = None
            break
        except Exception as exc:
            primary_error = exc
            log.warning("主库连接失败（%s/%s）：%s", i + 1, attempts, exc)
            if i + 1 < attempts:
                time.sleep(1.5)

    secondary_enabled = settings.secondary_enabled
    secondary = fallback_engine() if secondary_enabled else None
    if secondary is not None:
        try:
            ensure_schema(secondary)
        except Exception as exc:
            log.warning("次库表结构检查失败（先不启用容灾）：%s", exc)
            secondary = None
            secondary_enabled = False

    # 上次进程在降级期间退出：次库里可能有比主库新的数据。
    # 必须先把它们回迁主库，否则接下来的「首次镜像」会把它们覆盖掉。
    if primary_error is None and secondary_enabled and dirty():
        try:
            promote_once()
            _clear_dirty()
            log.info("检测到上次降级期间的写入，已回迁主库")
        except Exception as exc:
            log.error("降级数据回迁失败，为避免覆盖先停留在次库模式：%s", exc)
            primary_error = exc

    _started = True
    if primary_error is not None:
        if not secondary_enabled:
            raise primary_error
        _switch(MODE_FALLBACK, "启动时主库不可用")
        if not mirrored_before():
            log.warning(
                "当前次库从未与主库同步过，看到的是它自己的数据；主库恢复后只补缺失数据，不会覆盖主库"
            )
    elif secondary_enabled:
        # 后台线程镜像首份副本，不拖慢启动
        def _first_mirror() -> None:  # pragma: no cover - 线程
            global _last_mirror
            try:
                mirror_once("首次")
                _last_mirror = time.time()
            except Exception as exc:
                log.warning("首次镜像失败（不影响服务，下轮重试）：%s", exc)

        threading.Thread(target=_first_mirror, name="db-first-mirror", daemon=True).start()

    start_watchdog()
    return _mode
