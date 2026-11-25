"""第三方登录（Google / GitHub OAuth）的客户端配置。

为什么单独一层（与 `mail_config` 同一套思路）：
- Client ID / Secret 以前只能写 `.env`，改一次要重启进程；现在后台保存即生效，
  登录页的按钮立刻出现或消失。
- Client Secret 属于凭据：加密存放、接口只回掩码。沿用 `secret_box`，
  purpose 固定为 ``oauth``（参与 KDF 盐，上线后不能改）。

优先级：**后台保存的 > .env > 内置默认**。后台把 Client ID 留空 = 继续听 `.env`。

命名约定：`.env` 用 UPPER_SNAKE（`GOOGLE_CLIENT_ID`），JSON / HTTP 用 camelCase
（`googleClientId`），一一对应不缩写。

`enabled` 是管理员的意思表示，`ready` 才是「真的能跳转」——两者都要满足：
开关打开但凭据没填全时，登录页不显示按钮，后台卡片显示「未配置」。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse

from sqlalchemy.orm import Session

from . import secret_box, settings_store
from .config import settings

log = logging.getLogger("uvicorn.error").getChild("cloudnetp.oauth")

# system_settings 里的键：存一整个 JSON，没记录就表示「全部跟随 .env」
STORE_KEY = "oauth_config"

# purpose 参与派生盐，一旦有数据落库就不能再改
PURPOSE = "oauth"

PROVIDERS = ("google", "github")

# 普通文本字段：传空串 = 删掉后台覆盖，回落 .env
TEXT_FIELDS = ("googleClientId", "githubClientId")
# 密文字段：空串或 __keep__ = 不改，要清空用 clearGoogleSecret / clearGithubSecret / clearProxy。
# 代理也按密文处理：URL 里可能带账号密码，落库加密、接口只回掩码
SECRET_FIELDS = ("googleClientSecret", "githubClientSecret", "proxy")
BOOL_FIELDS = ("googleEnabled", "githubEnabled", "autoRegister")

_lock = threading.Lock()
_overrides: dict = {}
_loaded = False
# 读库失败后先等一会儿再试：登录页每次都要问「有没有开第三方登录」
_retry_at = 0.0


def _now() -> float:
    return time.time()


def _decrypt_override(raw: dict) -> dict:
    """把库里的原始覆盖拆成明文（密钥解密）。只保留认识的键，脏数据不进内存。"""
    data: dict = {}
    for field in BOOL_FIELDS:
        if field in raw:
            data[field] = bool(raw[field])
    for field in TEXT_FIELDS:
        if field in raw:
            data[field] = str(raw[field]).strip()
    for field in SECRET_FIELDS:
        if raw.get(field):
            plain = secret_box.decrypt(str(raw[field]), PURPOSE)
            if plain:
                data[field] = plain
    return data


def hydrate(force: bool = False) -> None:
    """启动时把后台覆盖读进内存；失败就先用 .env，过一会儿再试。"""
    global _loaded, _retry_at

    with _lock:
        if _loaded and not force:
            return
        if not force and _retry_at > _now():
            return
    try:
        from . import db_failover

        with db_failover.current_sessionmaker()() as db:
            raw = _read_store(db)
    except Exception:  # pragma: no cover - 数据库还没就绪时不该阻断启动
        log.warning("读取第三方登录配置失败，本次先用 .env 配置", exc_info=True)
        with _lock:
            _retry_at = _now() + 30
        return
    with _lock:
        _overrides.clear()
        _overrides.update(_decrypt_override(raw))
        _loaded = True
        _retry_at = 0.0


def reload(db: Session) -> None:
    """按库里的值重读内存（保存后调用）。"""
    global _loaded, _retry_at
    data = _decrypt_override(_read_store(db))
    with _lock:
        _overrides.clear()
        _overrides.update(data)
        _loaded = True
        _retry_at = 0.0


def reset() -> None:
    """清空内存缓存（测试用）。"""
    global _loaded, _retry_at
    with _lock:
        _overrides.clear()
        _loaded = False
        _retry_at = 0.0


def _read_store(db: Session) -> dict:
    raw = settings_store.get_value(db, STORE_KEY) or ""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# 生效值
# --------------------------------------------------------------------------- #
def _env_auto_register() -> bool:
    raw = (settings.OAUTH_AUTO_REGISTER or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    # 自动：配了任一 provider 就允许第三方首次登录直接建号
    return bool(
        settings.GOOGLE_CLIENT_ID
        or settings.GITHUB_CLIENT_ID
        or settings.GOOGLE_CLIENT_SECRET
        or settings.GITHUB_CLIENT_SECRET
    )


def env_config() -> dict:
    google_id = (settings.GOOGLE_CLIENT_ID or "").strip()
    google_secret = (settings.GOOGLE_CLIENT_SECRET or "").strip()
    github_id = (settings.GITHUB_CLIENT_ID or "").strip()
    github_secret = (settings.GITHUB_CLIENT_SECRET or "").strip()
    return {
        # .env 里配全了就算「想开」：老部署不用再动别的开关
        "googleEnabled": bool(google_id and google_secret),
        "googleClientId": google_id,
        "googleClientSecret": google_secret,
        "githubEnabled": bool(github_id and github_secret),
        "githubClientId": github_id,
        "githubClientSecret": github_secret,
        "autoRegister": _env_auto_register(),
        "proxy": (settings.OAUTH_PROXY or "").strip(),
    }


def resolved() -> dict:
    """生效配置（含明文凭据，只许服务端内部用）。"""
    cfg = env_config()
    hydrate()
    with _lock:
        for field, value in _overrides.items():
            cfg[field] = value
    for field in BOOL_FIELDS:
        cfg[field] = bool(cfg.get(field))
    for field in TEXT_FIELDS + SECRET_FIELDS:
        cfg[field] = str(cfg.get(field) or "").strip()
    return cfg


def provider_ready(cfg: dict, provider: str) -> bool:
    """够不够跳一次授权：开关打开 + ID 与 Secret 都在。"""
    if provider not in PROVIDERS:
        return False
    if not cfg.get(f"{provider}Enabled"):
        return False
    return bool(cfg.get(f"{provider}ClientId") and cfg.get(f"{provider}ClientSecret"))


def ready_providers(cfg: dict | None = None) -> list[str]:
    """登录页要显示哪些按钮（顺序固定，Google 在前）。"""
    cfg = cfg or resolved()
    return [p for p in PROVIDERS if provider_ready(cfg, p)]


def callback_url(provider: str, base: str | None = None) -> str:
    """必须与 Google / GitHub 后台填的回调地址逐字符一致。

    `base` 不传时用 `SITE_URL`（配置了就是权威值）；`/start`、`/callback` 会传
    请求自己的 base 进来——没配 `SITE_URL` 时跟随用户实际访问的域名，
    免得后台显示/真实跳转都落在 `localhost:8000` 这类绑死地址上。
    """
    prefix = (base or settings.public_base).rstrip("/")
    return f"{prefix}/api/auth/oauth/{provider}/callback"


# --------------------------------------------------------------------------- #
# 后台卡片（永远不含凭据原文）
# --------------------------------------------------------------------------- #
def mask_proxy(url: str) -> str:
    """代理地址掩码：藏住密码，但保留协议和主机，管理员得知道自己连的是哪台。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlsplit(raw)
    if parsed.password:
        host_part = parsed.netloc.split("@", 1)[1]
        user = parsed.username or ""
        return f"{parsed.scheme}://{user}:***@{host_part}"
    if "@" in parsed.netloc:
        return raw  # 只有用户名没有密码，不算敏感
    return raw


def snapshot() -> dict:
    env = env_config()
    cfg = resolved()
    hydrate()
    with _lock:
        overrides = dict(_overrides)

    def block(provider: str) -> dict:
        pid = f"{provider}ClientId"
        psec = f"{provider}ClientSecret"
        return {
            "enabled": bool(cfg.get(f"{provider}Enabled")),
            "enabledSource": (
                "admin" if f"{provider}Enabled" in overrides else ("env" if env[f"{provider}Enabled"] else "default")
            ),
            "clientId": cfg[pid],
            "clientIdSource": "admin" if pid in overrides else ("env" if env[pid] else ""),
            # 密钥只回掩码，原文永远不出服务端
            "hasSecret": bool(cfg[psec]),
            "secretMask": secret_box.mask_secret(cfg[psec]),
            "secretSource": "admin" if psec in overrides else ("env" if env[psec] else ""),
            "ready": provider_ready(cfg, provider),
            "callbackUrl": callback_url(provider),
            # SITE_URL 没配时真实回调跟着请求域名走，前端要拿 origin 自己拼展示值
            "callbackPath": f"/api/auth/oauth/{provider}/callback",
        }

    providers = {p: block(p) for p in PROVIDERS}
    return {
        "providers": providers,
        "siteUrlFixed": bool(settings.SITE_URL),
        # 出站代理（服务器 → Google/GitHub）：只回掩码，原文不出服务端
        "hasProxy": bool(cfg.get("proxy")),
        "proxyMask": mask_proxy(cfg.get("proxy")),
        "proxySource": "admin" if "proxy" in _overrides else ("env" if env_config()["proxy"] else "default"),
        "autoRegister": bool(cfg.get("autoRegister")),
        "autoRegisterSource": (
            "admin" if "autoRegister" in overrides else ("env" if (settings.OAUTH_AUTO_REGISTER or "").strip() else "auto")
        ),
        # 登录页要的极简信息：只关心「哪些按钮该出现」
        "ready": [p for p in PROVIDERS if providers[p]["ready"]],
        "env": {
            "googleClientId": env["googleClientId"],
            "githubClientId": env["githubClientId"],
            "hasGoogleSecret": bool(env["googleClientSecret"]),
            "hasGithubSecret": bool(env["githubClientSecret"]),
        },
    }


# --------------------------------------------------------------------------- #
# 保存
# --------------------------------------------------------------------------- #
def save(db: Session, patch: dict) -> dict:
    """保存后台配置，返回最新快照。

    - 普通文本字段传 ``""`` = 删掉该覆盖，回落 .env
    - 密钥传 ``""`` 或 ``"__keep__"`` = **不改**（前端只显示掩码，空输入必须
      理解成「不改」）；要清空走 ``clearGoogleSecret`` / ``clearGithubSecret``
    """
    current = dict(_read_store(db))
    clears = {
        "googleClientSecret": bool(patch.pop("clearGoogleSecret", False)),
        "githubClientSecret": bool(patch.pop("clearGithubSecret", False)),
        "proxy": bool(patch.pop("clearProxy", False)),
    }

    for field, value in patch.items():
        if value is None:
            continue
        if field in SECRET_FIELDS:
            if clears.get(field):
                current.pop(field, None)
                continue
            raw = str(value).strip()
            if raw and raw != "__keep__":
                current[field] = secret_box.encrypt(raw, PURPOSE)
            continue
        if field in BOOL_FIELDS:
            current[field] = bool(value)
            continue
        if field in TEXT_FIELDS:
            text = str(value).strip()
            if text:
                current[field] = text
            else:
                current.pop(field, None)

    # 清除标记可能单独出现（patch 里没有对应字段），循环外再兜一遍
    for field, do_clear in clears.items():
        if do_clear:
            current.pop(field, None)

    settings_store.set_value(db, STORE_KEY, json.dumps(current, ensure_ascii=False))
    db.commit()
    reload(db)
    return snapshot()


def clear(db: Session) -> dict:
    """清掉全部后台覆盖，回到 .env。"""
    settings_store.set_value(db, STORE_KEY, "")
    db.commit()
    reload(db)
    return snapshot()
