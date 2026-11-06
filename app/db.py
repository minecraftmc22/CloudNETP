"""数据库引擎与会话。同步引擎 + def 路由（FastAPI 自动投放线程池）。"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from sqlalchemy import String, create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

_connect_args: dict[str, Any] = {}
_engine_kwargs: dict[str, Any] = {"echo": settings.DB_ECHO, "future": True}

if settings.is_sqlite:
    _connect_args["check_same_thread"] = False
else:
    # 远程 MySQL（跨网络/内网穿透）必须显式超时，否则网络抖动会让请求永久卡在 socket 读上
    _connect_args.update(
        connect_timeout=settings.DB_CONNECT_TIMEOUT,
        read_timeout=settings.DB_TIMEOUT,
        write_timeout=settings.DB_TIMEOUT,
    )
    _engine_kwargs.update(
        pool_pre_ping=True,
        # 远端建连成本很高（SSL 握手 3~10s），尽量复用长连接
        pool_recycle=3600,
        pool_timeout=30,
        pool_size=10,
        max_overflow=20,
    )

engine = create_engine(settings.DATABASE_URL, connect_args=_connect_args, **_engine_kwargs)

if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - 运行时副作用
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """每个请求一个会话。绑定哪个引擎由 db_failover 决定（主库 / SQLite 回退）。"""
    from .db_failover import current_sessionmaker

    db = current_sessionmaker()()
    try:
        yield db
    finally:
        db.close()


def ensure_schema(target_engine: Any) -> None:
    """对指定引擎建表（幂等）。生产库也可直接执行 sql/schema.sql。"""
    from . import models  # noqa: F401  确保模型已注册

    if not settings.DB_AUTO_CREATE:
        return

    expected = set(Base.metadata.tables)
    try:
        # 远程库一次往返拿全表名；表齐全就跳过 create_all（否则每张表都要往返，几十秒）
        with target_engine.connect() as conn:
            existing = set(inspect(conn).get_table_names())
        if expected <= existing:
            _sync_columns(target_engine)
            return
    except Exception:  # 检查失败就走正常 create_all
        pass
    Base.metadata.create_all(bind=target_engine)
    _sync_columns(target_engine)


def init_db() -> None:
    """兼容旧入口：只初始化主库。完整容灾初始化走 db_failover.startup()。"""
    ensure_schema(engine)


# 已上线表后续新增的列：{表名: {列名: DDL}}。create_all 不会给老表补列，这里兜底。
_COLUMN_PATCHES: dict[str, dict[str, str]] = {
    "user_settings": {
        "trash_enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "share_show_name": "BOOLEAN NOT NULL DEFAULT 0",
        "lyrics_style": "JSON NULL",
        "background_image": "VARCHAR(64) NULL",
        "surface_opacity": "JSON NULL",
        # 看板娘 AI 配置（老库补列后为 NULL，读取处统一按空对象处理）
        "ai_config": "JSON NULL",
    },
    "users": {
        "display_name": "VARCHAR(32) NULL",
        "bio": "VARCHAR(200) NULL",
        "avatar": "VARCHAR(64) NULL",
        "dav_ha1": "VARCHAR(32) NULL",
        "email": "VARCHAR(254) NULL",
        "email_verified": "BOOLEAN NOT NULL DEFAULT 0",
    },
    "shares": {
        # 合集分享：根节点 id 的 JSON 数组；NULL = 单节点分享
        "node_ids": "TEXT NULL",
    },
}

# 需要改类型/长度的老列：{表名: {列名: 新定义}}
_COLUMN_MODIFIES: dict[str, dict[str, str]] = {
    # 提取码改为存 scrypt 哈希（原先 VARCHAR(8) 只够存明文 4 位）
    "shares": {"extract_code": "VARCHAR(255) NULL"},
}


def _sync_columns(target_engine: Any) -> None:
    """轻量迁移：缺列就 ALTER 补上，已存在则跳过；长度不够的列改定义。"""
    log = logging.getLogger("uvicorn.error")
    try:
        with target_engine.connect() as conn:
            insp = inspect(conn)
            tables = set(insp.get_table_names())
            for table, columns in _COLUMN_PATCHES.items():
                if table not in tables:
                    continue
                existing = {col["name"] for col in insp.get_columns(table)}
                for name, ddl in columns.items():
                    if name in existing:
                        continue
                    with target_engine.begin() as tx:
                        tx.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    log.info("数据库补列：%s.%s", table, name)

            # MODIFY COLUMN 是 MySQL 语法；SQLite 的 TEXT 不限长度，改列无意义
            if target_engine.dialect.name != "mysql":
                return
            for table, columns in _COLUMN_MODIFIES.items():
                if table not in tables:
                    continue
                info = {col["name"]: col for col in insp.get_columns(table)}
                for name, ddl in columns.items():
                    col = info.get(name)
                    if col is None:
                        continue
                    # 已经是宽列就跳过，避免每次启动都 MODIFY
                    if isinstance(col.get("type"), String) and col["type"].length not in (None, 8):
                        continue
                    if not isinstance(col.get("type"), String):
                        continue
                    with target_engine.begin() as tx:
                        tx.exec_driver_sql(f"ALTER TABLE {table} MODIFY COLUMN {name} {ddl}")
                    log.info("数据库改列：%s.%s → %s", table, name, ddl)
    except Exception as exc:  # 补列失败不阻断启动，但要在日志里暴露
        log.warning("数据库补列跳过：%s", exc)
