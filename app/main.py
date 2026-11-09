"""CloudNETP 应用装配。"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import STATIC_DIR, settings
from .deps import require_admin
from .models import User
from . import db_failover

logger = logging.getLogger("uvicorn.error").getChild("cloudnetp")


def _prepare_dirs() -> None:
    for path in (
        settings.STORAGE_DIR,
        settings.blob_dir,
        settings.tmp_dir,
        settings.thumb_dir,
        settings.chunk_dir,
        settings.live2d_model_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)


def _startup_maintenance() -> None:
    """清理过期的上传会话分片与超期回收站条目。"""
    from . import node_service, upload_service
    from .settings_store import get_int

    with db_failover.current_sessionmaker()() as db:
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
    active_mode = db_failover.startup()
    logger.info(
        "建表检查完成 %.1fs | 数据库模式 %s",
        time.perf_counter() - started,
        active_mode,
    )
    if settings.SECRET_KEY_IS_EPHEMERAL:
        logger.warning("SECRET_KEY 未配置，使用临时密钥：重启后所有会话失效，请在 .env 中设置。")
    # 网易云 Cookie 读进内存：取歌词的路径就不用每次查库；失败时自动退回 .env
    from . import netease_auth

    netease_auth.hydrate()
    # 歌词来源（后台可能配了歌词库密钥/地址）也读进内存，同样失败就先用 .env
    from . import lyrics_config

    lyrics_config.hydrate()
    _startup_maintenance()
    # 自动更新看门狗：默认关闭，管理员在后台开启后每 6 小时检查一次
    from . import update_service

    update_service.start_auto_watcher()
    logger.info(
        "监听配置 %s://%s:%s（.env）| 若与 uvicorn 打印的地址不一致，说明启动方式没走 run.py / main.py",
        settings.scheme,
        settings.HOST,
        settings.PORT,
    )
    if settings.wildcard_host:
        lan = settings.lan_url
        logger.info(
            "浏览器访问 %s%s —— %s 只是「监听所有网卡」，不是能打开的地址",
            settings.browse_url,
            f"，局域网内的设备用 {lan}" if lan else "",
            settings.HOST or "0.0.0.0",
        )
    else:
        logger.info("浏览器访问 %s", settings.browse_url)
    logger.info(
        "CloudNETP %s 启动完成（共 %.1fs）| DB=%s",
        settings.VERSION,
        time.perf_counter() - started,
        db_failover.active_db_label(),
    )
    yield


app = FastAPI(
    title="CloudNETP",
    version=settings.VERSION,
    lifespan=lifespan,
    # 默认的 /docs、/redoc、/openapi.json 会公开列出所有接口（含 /api/admin/*），
    # 这里全关掉，改成下面的自定义路由，只放给管理员看。
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/api/openapi.json", include_in_schema=False)
def openapi_spec(_: User = Depends(require_admin)) -> JSONResponse:
    """接口清单只对管理员开放（内网自用项目，不打算对外提供 API 文档）。"""
    return JSONResponse(app.openapi())


@app.get("/api/docs", include_in_schema=False)
def api_docs(_: User = Depends(require_admin)) -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url="/api/openapi.json",
        title=f"{settings.APP_NAME} API",
        swagger_favicon_url="/assets/favicon-32.png",
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.status_code},
        headers=getattr(exc, "headers", None),
    )


@app.middleware("http")
async def static_no_cache(request: Request, call_next):
    """静态资源必须回源验证（配合 ETag 走 304），避免浏览器启发式缓存吃旧文件。"""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") or path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/version")
def version() -> dict:
    return {"version": settings.VERSION, "app": settings.APP_NAME}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """根路径兜底：浏览器即使没读到 link 标签也会主动请求 /favicon.ico。"""
    return FileResponse(STATIC_DIR / "assets" / "favicon.ico", media_type="image/x-icon")


@app.get("/terms", include_in_schema=False)
def terms_page() -> FileResponse:
    """用户协议：登录/注册页的「已阅读并同意」指向这里，未登录也能打开。"""
    return FileResponse(STATIC_DIR / "terms.html", headers={"Cache-Control": "no-cache"})


@app.get("/privacy", include_in_schema=False)
def privacy_page() -> FileResponse:
    """隐私政策：同上，独立页面，不依赖登录态。"""
    return FileResponse(STATIC_DIR / "privacy.html", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def webmanifest() -> FileResponse:
    """PWA 清单。必须挂在根路径，start_url / scope 才能覆盖整站。"""
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    """Service Worker 必须在根路径，作用域才能是 /。永远不缓存。"""
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

# Live2D 模型：/live2d/model/<模型名>/xxx.model3.json，目录不存在时先建出来再挂载
settings.live2d_model_dir.mkdir(parents=True, exist_ok=True)
app.mount("/live2d", StaticFiles(directory=settings.LIVE2D_DIR), name="live2d")

from .routers import admin as admin_router  # noqa: E402
from .routers import ai as ai_router  # noqa: E402
from .routers import auth as auth_router  # noqa: E402
from .routers import dav as dav_router  # noqa: E402
from .routers import feedback as feedback_router  # noqa: E402
from .routers import live2d as live2d_router  # noqa: E402
from .routers import nodes as nodes_router  # noqa: E402
from .routers import shares as shares_router  # noqa: E402
from .routers import update as update_router  # noqa: E402
from .routers import uploads as uploads_router  # noqa: E402

app.include_router(auth_router.router)
app.include_router(nodes_router.router)
app.include_router(shares_router.router)
app.include_router(uploads_router.router)
app.include_router(admin_router.router)
app.include_router(dav_router.router)
app.include_router(live2d_router.router)
app.include_router(feedback_router.router)
app.include_router(update_router.router)
app.include_router(ai_router.router)


@app.get("/s/{token}", include_in_schema=False)
def share_page(token: str) -> FileResponse:
    """公开分享页（独立页面，不依赖登录态）。"""
    return FileResponse(STATIC_DIR / "share.html", headers={"Cache-Control": "no-cache"})
