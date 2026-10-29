"""集中配置：读取项目根目录 .env，零第三方依赖。"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # 真实环境变量优先，.env 只做兜底
        os.environ.setdefault(key, value)


_load_env_file(ENV_FILE)


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "").strip())
    except (TypeError, ValueError):
        return default


def _get_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    # ---- 应用 ----
    APP_NAME: str = _get("APP_NAME", "CloudNETP")
    VERSION: str = "0.1.4"
    DEBUG: bool = _get_bool("DEBUG", True)

    # ---- 安全 ----
    SECRET_KEY: str = _get("SECRET_KEY") or secrets.token_hex(32)
    SECRET_KEY_IS_EPHEMERAL: bool = not bool(_get("SECRET_KEY"))
    COOKIE_NAME: str = _get("COOKIE_NAME", "cloudnetp_session")
    COOKIE_SECURE: bool = _get_bool("COOKIE_SECURE", False)
    SESSION_TTL_SECONDS: int = _get_int("SESSION_TTL_SECONDS", 60 * 60 * 24 * 14)

    # ---- 数据库 ----
    # 单点切换：mysql+pymysql://user:pass@host:3306/db?charset=utf8mb4  <->  sqlite:///D:/.../dev.db
    DATABASE_URL: str = _get("DATABASE_URL") or f"sqlite:///{(BASE_DIR / 'dev.db').as_posix()}"
    DB_ECHO: bool = _get_bool("DB_ECHO", False)
    # 跨网络访问 MySQL 时，显式超时避免请求永久阻塞
    DB_CONNECT_TIMEOUT: int = _get_int("DB_CONNECT_TIMEOUT", 10)
    DB_TIMEOUT: int = _get_int("DB_TIMEOUT", 60)
    # 启动时是否执行建表（远程库延迟高时可在 .env 里关掉）
    DB_AUTO_CREATE: bool = _get_bool("DB_AUTO_CREATE", True)

    # ---- 存储 ----
    STORAGE_DIR: Path = Path(_get("STORAGE_DIR") or str(BASE_DIR / "storage")).resolve()
    THUMB_ENABLED: bool = _get_bool("THUMB_ENABLED", True)

    # ---- 配额与上传 ----
    DEFAULT_QUOTA_GB: int = _get_int("DEFAULT_QUOTA_GB", 10)
    MAX_FILE_SIZE_MB: int = _get_int("MAX_FILE_SIZE_MB", 4096)
    CHUNK_SIZE_MB: int = _get_int("CHUNK_SIZE_MB", 5)
    UPLOAD_SESSION_TTL_HOURS: int = _get_int("UPLOAD_SESSION_TTL_HOURS", 72)

    # ---- 注册开关 ----
    ALLOW_REGISTER: bool = _get_bool("ALLOW_REGISTER", True)

    @property
    def default_quota_bytes(self) -> int:
        return self.DEFAULT_QUOTA_GB * 1024 ** 3

    @property
    def max_file_size_bytes(self) -> int:
        return self.MAX_FILE_SIZE_MB * 1024 ** 2

    @property
    def chunk_size_bytes(self) -> int:
        return max(1, self.CHUNK_SIZE_MB) * 1024 ** 2

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def blob_dir(self) -> Path:
        return self.STORAGE_DIR / "blobs"

    @property
    def tmp_dir(self) -> Path:
        return self.STORAGE_DIR / "tmp"

    @property
    def chunk_dir(self) -> Path:
        """分片上传中转区：storage/chunks/<session_id>/<index>.part"""
        return self.STORAGE_DIR / "chunks"

    @property
    def thumb_dir(self) -> Path:
        return self.STORAGE_DIR / "thumbs"


settings = Settings()

STATIC_DIR = BASE_DIR / "static"
