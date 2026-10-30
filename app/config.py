"""集中配置：读取项目根目录 .env，零第三方依赖。"""

from __future__ import annotations

import os
import secrets
import socket
from pathlib import Path
from urllib.parse import urlsplit

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
    VERSION: str = "0.3.1"
    DEBUG: bool = _get_bool("DEBUG", True)

    # ---- 监听与对外地址 ----
    # 监听地址：0.0.0.0 = 允许局域网/公网访问，127.0.0.1 = 只允许本机
    HOST: str = _get("HOST", "0.0.0.0")
    PORT: int = _get_int("PORT", 8123)
    # 开发热重载（生产建议 false）
    RELOAD: bool = _get_bool("RELOAD", False)
    # 对外站点地址：给分享链接、WebDAV 地址、Cookie 域名用；反代/域名/HTTPS 时填，例如 https://pan.example.com
    SITE_URL: str = _get("SITE_URL").strip().rstrip("/")
    # 直接由 uvicorn 提供 HTTPS 时填证书；留空则走 HTTP（交给反向代理做 TLS）
    SSL_CERTFILE: str = _get("SSL_CERTFILE")
    SSL_KEYFILE: str = _get("SSL_KEYFILE")
    SSL_KEYFILE_PASSWORD: str = _get("SSL_KEYFILE_PASSWORD")
    # 信任反向代理的 X-Forwarded-* 头（Nginx/Caddy 转发时打开）
    TRUST_PROXY: bool = _get_bool("TRUST_PROXY", True)

    # ---- 预览/播放器插件 ----
    # 投屏需要 Chrome + Google Cast SDK（国内通常不可达），默认关闭
    PREVIEW_CHROMECAST: bool = _get_bool("PREVIEW_CHROMECAST", False)
    # VAST 贴片广告地址，留空即不挂广告插件
    PREVIEW_VAST_TAG: str = _get("PREVIEW_VAST_TAG")

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
    # ---- 数据库容灾 ----
    # 主库（DATABASE_URL，通常远程 MySQL）连不上时自动降级本地 SQLite 继续服务；
    # 平时定期把主库镜像进 SQLite，恢复后先把 SQLite 期间的变更回迁主库再切回。
    # 注意：按「单实例部署」设计，多实例同时写主库时不要开启。
    DB_FALLBACK_ENABLED: bool = _get_bool("DB_FALLBACK_ENABLED", True)
    # 看门狗探测主库间隔（秒）。0 = 只在启动时选库，运行中不切换
    DB_PROBE_INTERVAL: int = _get_int("DB_PROBE_INTERVAL", 30)
    # 主库 → SQLite 镜像间隔（秒）。0 = 不做周期镜像（启动时仍会镜像一次）
    DB_MIRROR_INTERVAL: int = _get_int("DB_MIRROR_INTERVAL", 300)

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

    @property
    def avatar_dir(self) -> Path:
        """用户头像：storage/avatars/<user_id>-<随机>.webp"""
        return self.STORAGE_DIR / "avatars"

    @property
    def fallback_db_path(self) -> Path:
        """容灾 SQLite：storage/db/fallback.db（随 storage 一起备份）。"""
        return self.STORAGE_DIR / "db" / "fallback.db"

    @property
    def fallback_db_url(self) -> str:
        return f"sqlite:///{self.fallback_db_path.as_posix()}"

    # ---- 地址 / TLS 派生值 ----
    @property
    def ssl_enabled(self) -> bool:
        return bool(self.SSL_CERTFILE and self.SSL_KEYFILE)

    @property
    def scheme(self) -> str:
        return "https" if self.ssl_enabled else "http"

    @property
    def site_netloc(self) -> str:
        """SITE_URL 的 host:port，用于写操作来源校验与 Cookie 域。"""
        return urlsplit(self.SITE_URL).netloc if self.SITE_URL else ""

    @property
    def wildcard_host(self) -> bool:
        """HOST 是否是「通配监听地址」：0.0.0.0 / :: / 空。

        这类地址只能用于 bind，**不能填进浏览器地址栏**（0.0.0.0 不是一个可路由的目的地址，
        Chrome 124+ 会直接拦掉），所以对外展示时要换成 localhost 或局域网 IP。
        """
        return self.HOST in {"", "0.0.0.0", "::", "::0"}

    @property
    def lan_ip(self) -> str:
        """本机局域网 IP，拿不到返回空串。只做 UDP connect 选路，不发包、不联网。"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.4)
                sock.connect(("223.5.5.5", 80))
                return sock.getsockname()[0]
        except OSError:
            return ""

    @property
    def browse_url(self) -> str:
        """本机浏览器可直接打开的地址：通配地址自动换成 localhost。"""
        host = "localhost" if self.wildcard_host else self.HOST
        return f"{self.scheme}://{host}:{self.PORT}"

    @property
    def lan_url(self) -> str:
        """局域网内其它设备访问的地址；没监听通配地址或取不到 IP 时为空串。"""
        if not self.wildcard_host:
            return ""
        ip = self.lan_ip
        return f"{self.scheme}://{ip}:{self.PORT}" if ip else ""

    @property
    def public_base(self) -> str:
        """对外可访问的站点根地址（无尾斜杠）。"""
        if self.SITE_URL:
            return self.SITE_URL
        host = "localhost" if self.wildcard_host else self.HOST
        return f"{self.scheme}://{host}:{self.PORT}"

    @property
    def cookie_secure(self) -> bool:
        """HTTPS 下自动给会话 Cookie 加 Secure，避免明文回传。"""
        return self.COOKIE_SECURE or self.ssl_enabled


settings = Settings()

STATIC_DIR = BASE_DIR / "static"
