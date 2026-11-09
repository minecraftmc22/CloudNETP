"""歌词来源的运行时配置（管理员在后台改，服务端热生效）。

为什么单独一层：
- 来源、接口地址、密钥、Cookie 以前只能写在 `.env`，改一次要重启进程；
  现在后台改完立刻生效，播放器里「搜索指定」的分类也会跟着变。
- 密钥与 Cookie 属于凭据，必须加密存放、接口只回掩码。沿用 `secret_box`，
  purpose 固定为 ``lyrics``（参与 KDF 盐，上线后不能改）。

优先级：**后台保存的 > .env > 内置默认**。
后台把某个字段留空 = 该字段继续听 `.env` 的，老部署不用搬配置。
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy.orm import Session

from . import secret_box, settings_store
from .config import settings

log = logging.getLogger("uvicorn.error").getChild("cloudnetp.lyrics")

# system_settings 里的键（都不在 DEFAULTS 里，没有记录就表示「跟随 .env」）
STORE_PROVIDERS = "lyrics_providers"
STORE_API_BASE = "lyrics_api_base"
STORE_API_KEY = "lyrics_api_key"
STORE_API_COOKIE = "lyrics_api_cookie"

# purpose 参与派生盐，一旦有数据落库就不能再改
PURPOSE = "lyrics"

_lock = threading.Lock()
_state: dict[str, str] = {
    "providers": "",  # 逗号分隔的原始串，空 = 跟随 .env
    "api_base": "",
    "api_key": "",  # 解密后的明文，只在本进程内存里
    "api_cookie": "",
}
_loaded = False
# 读库失败后先等一会儿再试：数据库没就绪时不要让每次取歌词都去敲一遍库
_retry_at = 0.0


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def _read(db: Session, key: str) -> str:
    # 没有记录时 get_value 会退回 DEFAULTS；这几个键不在 DEFAULTS 里，得到空串
    return (settings_store.get_value(db, key) or "").strip()


def hydrate(force: bool = False) -> None:
    """启动时把后台配置读进内存；失败就先用 .env，过一会儿再试。"""
    global _loaded, _retry_at

    with _lock:
        if _loaded and not force:
            return
        if not force and _retry_at > _now():
            return
    try:
        from . import db_failover

        with db_failover.current_sessionmaker()() as db:
            providers = _read(db, STORE_PROVIDERS)
            base = _read(db, STORE_API_BASE)
            key = secret_box.decrypt(_read(db, STORE_API_KEY), PURPOSE)
            cookie = secret_box.decrypt(_read(db, STORE_API_COOKIE), PURPOSE)
    except Exception:  # pragma: no cover - 数据库还没就绪时不该阻断启动
        log.warning("读取歌词来源配置失败，本次先用 .env 配置", exc_info=True)
        with _lock:
            _retry_at = _now() + 30
        return
    with _lock:
        _state.update(
            providers=providers, api_base=base, api_key=key, api_cookie=cookie
        )
        _loaded = True
        _retry_at = 0.0


def _now() -> float:
    import time

    return time.time()


def reload(db: Session) -> None:
    """按库里的值重读内存（保存后调用）。"""
    providers = _read(db, STORE_PROVIDERS)
    base = _read(db, STORE_API_BASE)
    key = secret_box.decrypt(_read(db, STORE_API_KEY), PURPOSE)
    cookie = secret_box.decrypt(_read(db, STORE_API_COOKIE), PURPOSE)
    global _loaded, _retry_at
    with _lock:
        _state.update(
            providers=providers, api_base=base, api_key=key, api_cookie=cookie
        )
        _loaded = True
        _retry_at = 0.0


def reset() -> None:
    """清空内存缓存（测试用）。"""
    global _loaded, _retry_at
    with _lock:
        _state.update(providers="", api_base="", api_key="", api_cookie="")
        _loaded = False
        _retry_at = 0.0


def _get(field: str) -> str:
    hydrate()
    with _lock:
        return _state.get(field, "")


def providers_raw() -> str:
    """后台保存的来源串（空 = 跟随 .env）。"""
    return _get("providers")


def api_base() -> str:
    """生效的歌词库地址：后台 > .env > 内置默认。"""
    return _get("api_base") or (settings.LYRICS_API_BASE or "").rstrip("/")


def api_key() -> str:
    return _get("api_key") or (settings.LYRICS_API_KEY or "").strip()


def api_cookie() -> str:
    return _get("api_cookie") or (settings.LYRICS_API_COOKIE or "").strip()


# --------------------------------------------------------------------------- #
# 状态（给后台卡片，永远不含凭据原文）
# --------------------------------------------------------------------------- #
def snapshot() -> dict:
    """后台卡片要的所有信息：生效值、来源、掩码、可用性。"""
    from . import lyrics_service

    admin_key = _get("api_key")
    admin_cookie = _get("api_cookie")
    admin_base = _get("api_base")
    admin_providers = _get("providers")

    env_key = (settings.LYRICS_API_KEY or "").strip()
    env_cookie = (settings.LYRICS_API_COOKIE or "").strip()
    key = api_key()
    cookie = api_cookie()

    enabled = lyrics_service.enabled_providers()
    if admin_providers:
        providers_source = "admin"
    elif settings.LYRICS_PROVIDERS or settings.LYRICS_PROVIDER:
        providers_source = "env"
    else:
        # 两边都没写：配了歌词库密钥就两个都开，否则只有网易云
        providers_source = "auto"

    env_providers = (settings.LYRICS_PROVIDERS or settings.LYRICS_PROVIDER or "").strip()

    return {
        # 生效值
        "providers": enabled,
        "providersSource": providers_source,
        "apiBase": api_base(),
        "apiBaseSource": "admin" if admin_base else "env",
        # 只回掩码与来源，不回原文
        "hasKey": bool(key),
        "keyMask": secret_box.mask_secret(admin_key or env_key),
        "keySource": "admin" if admin_key else ("env" if env_key else ""),
        "hasCookie": bool(cookie),
        "cookieMask": secret_box.mask_secret(admin_cookie or env_cookie),
        "cookieSource": "admin" if admin_cookie else ("env" if env_cookie else ""),
        # 对照信息：.env 里有什么，方便管理员判断「留空」会落到哪
        "env": {
            "providers": env_providers,
            "apiBase": settings.LYRICS_API_BASE or "",
            "hasKey": bool(env_key),
            "hasCookie": bool(env_cookie),
        },
        "ready": {pid: lyrics_service.provider_ready(pid) for pid in lyrics_service.PROVIDERS},
    }


# --------------------------------------------------------------------------- #
# 保存
# --------------------------------------------------------------------------- #
def save(
    db: Session,
    *,
    providers: list[str] | None,
    api_base: str | None,
    api_key: str | None,
    api_cookie: str | None,
    clear_key: bool = False,
    clear_cookie: bool = False,
) -> dict:
    """保存后台配置；空值表示继续跟随 .env。

    - ``providers=None`` 不改；``providers=[]`` 清掉后台覆盖，回到 .env / 自动
    - ``api_key=""`` / ``api_cookie=""``：**保持原值**（前端只显示掩码，
      不回填原文，所以空输入必须理解成「不改」）；要清空用 ``clear_*``
    """
    if providers is not None:
        settings_store.set_value(db, STORE_PROVIDERS, ",".join(providers))

    if api_base is not None:
        settings_store.set_value(db, STORE_API_BASE, api_base.strip())

    if clear_key:
        settings_store.set_value(db, STORE_API_KEY, "")
    elif api_key:
        settings_store.set_value(db, STORE_API_KEY, secret_box.encrypt(api_key, PURPOSE))

    if clear_cookie:
        settings_store.set_value(db, STORE_API_COOKIE, "")
    elif api_cookie:
        settings_store.set_value(db, STORE_API_COOKIE, secret_box.encrypt(api_cookie, PURPOSE))

    db.commit()
    reload(db)

    # 地址/密钥/来源变了，之前的搜索结果与歌词缓存都不能再用
    from . import lyrics_service

    lyrics_service.reset_caches()
    return snapshot()
