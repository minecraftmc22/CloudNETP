"""数据库引擎与会话。同步引擎 + def 路由（FastAPI 自动投放线程池）。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlalchemy import create_engine, event, inspect
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
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """建表（幂等）。生产库也可直接执行 sql/schema.sql。"""
    from . import models  # noqa: F401  确保模型已注册

    if not settings.DB_AUTO_CREATE:
        return

    expected = set(Base.metadata.tables)
    try:
        # 远程库一次往返拿全表名；表齐全就跳过 create_all（否则每张表都要往返，几十秒）
        with engine.connect() as conn:
            existing = set(inspect(conn).get_table_names())
        if expected <= existing:
            return
    except Exception:  # 检查失败就走正常 create_all
        pass
    Base.metadata.create_all(bind=engine)
