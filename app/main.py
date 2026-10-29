"""CloudNETP 应用装配。"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import STATIC_DIR, settings
from .db import SessionLocal, init_db

logger = logging.getLogger("uvicorn.error").getChild("cloudnetp")


def _prepare_dirs() -> None:
    for path in (
        settings.STORAGE_DIR,
        settings.blob_dir,
        settings.tmp_dir,
        settings.thumb_dir,
        settings.chunk_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)


def _startup_maintenance() -> None:
    """清理过期的上传会话分片与超期回收站条目。"""
    from . import node_service, upload_service
    from .settings_store import get_int

    with SessionLocal() as db:
        try:
            stale = upload_service.gc_stale_sessions(db)
            db.commit()
            if stale:
                logger.info("清理过期上传会话 %s 个", stale)
        except Exception:  # pragma: no cover - 维护任务不应阻断启动
            db.rollback()
            logger.exception("清理上传会话失败")
        try:
            days = get_int(db, "trash_retention_days")
            removed = node_service.purge_expired(db, days)
            db.commit()
            if removed:
                logger.info("回收站保留期 %s 天，清理 %s 个条目", days, removed)
        except Exception:  # pragma: no cover
            db.rollback()
            logger.exception("清理回收站失败")


@asynccontextmanager
async def lifespan(app: FastAPI):
    started = time.perf_counter()
    _prepare_dirs()
    init_db()
    logger.info("建表检查完成 %.1fs", time.perf_counter() - started)
    if settings.SECRET_KEY_IS_EPHEMERAL:
        logger.warning("SECRET_KEY 未配置，使用临时密钥：重启后所有会话失效，请在 .env 中设置。")
    _startup_maintenance()
    logger.info(
        "CloudNETP %s 启动完成（共 %.1fs）| DB=%s",
        settings.VERSION,
        time.perf_counter() - started,
        settings.DATABASE_URL.split("@")[-1],
    )
    yield


app = FastAPI(title="CloudNETP", version=settings.VERSION, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.status_code},
        headers=getattr(exc, "headers", None),
    )


@app.get("/api/version")
def version() -> dict:
    return {"version": settings.VERSION, "app": settings.APP_NAME}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "assets" / "favicon.svg", media_type="image/svg+xml")


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

from .routers import auth as auth_router  # noqa: E402
from .routers import nodes as nodes_router  # noqa: E402
from .routers import uploads as uploads_router  # noqa: E402

app.include_router(auth_router.router)
app.include_router(nodes_router.router)
app.include_router(uploads_router.router)
