"""看板娘 AI 的默认接口配置（管理员在后台改，服务端热生效）。

为什么单独一层：
- 默认 API（地址 / 密钥 / 模型 / 高级配置）以前只能由每个用户自己填；
  现在管理员可以在 `.env` 里给一套默认值，用户开箱就能聊。
- 默认接口的地址与密钥属于凭据：普通用户的 `/api/ai/config` 永远看不到它们，
  只有管理后台的卡片能读到（密钥只回掩码）。沿用 `secret_box`，purpose 固定为
  ``ai_default``（参与 KDF 盐，上线后不能改）。

优先级：**后台保存的 > .env > 内置默认**。
后台把某个字符串字段留空 = 该字段继续听 `.env` 的。
"""

from __future__ import annotations

import json
import logging
import threading
import time

from sqlalchemy.orm import Session

from . import secret_box, settings_store
from .config import settings

log = logging.getLogger("uvicorn.error").getChild("cloudnetp.ai_defaults")

# system_settings 里的键：存一整个 JSON，没记录就表示「全部跟随 .env」
STORE_KEY = "ai_default_config"

# purpose 参与派生盐，一旦有数据落库就不能再改
PURPOSE = "ai_default"

PROTOCOLS = {"auto", "openai", "anthropic"}

_lock = threading.Lock()
_overrides: dict = {}
_loaded = False
# 读库失败后先等一会儿再试：数据库没就绪时不要让每次对话都去敲一遍库
_retry_at = 0.0


def _now() -> float:
    return time.time()


def _decrypt_override(raw: dict) -> dict:
    data = {}
    for field in ("enabled", "protocol", "vision", "thinking", "tools", "maxInput", "maxOutput"):
        if field in raw:
            data[field] = raw[field]
    for field in ("baseUrl", "model"):
        if raw.get(field):
            data[field] = str(raw[field]).strip()
    if raw.get("key"):
        plain = secret_box.decrypt(str(raw["key"]), PURPOSE)
        if plain:
            data["key"] = plain
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
        log.warning("读取 AI 默认接口配置失败，本次先用 .env 配置", exc_info=True)
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


def _get(field: str, default):
    hydrate()
    with _lock:
        return _overrides.get(field, default)


# --------------------------------------------------------------------------- #
# 生效值
# --------------------------------------------------------------------------- #
def env_config() -> dict:
    protocol = str(settings.AI_DEFAULT_PROTOCOL or "auto").lower()
    return {
        "enabled": bool(settings.AI_DEFAULT_ENABLED),
        "baseUrl": (settings.AI_DEFAULT_BASE_URL or "").strip().rstrip("/"),
        "key": (settings.AI_DEFAULT_API_KEY or "").strip(),
        "model": (settings.AI_DEFAULT_MODEL or "").strip(),
        "protocol": protocol if protocol in PROTOCOLS else "auto",
        "vision": bool(settings.AI_DEFAULT_VISION),
        "thinking": bool(settings.AI_DEFAULT_THINKING),
        "tools": bool(settings.AI_DEFAULT_TOOLS),
        "maxInput": max(0, int(settings.AI_DEFAULT_MAX_INPUT or 0)),
        "maxOutput": max(0, int(settings.AI_DEFAULT_MAX_OUTPUT or 0)),
    }


def resolved() -> dict:
    """生效的默认接口：后台覆盖 > .env。"""
    cfg = env_config()
    hydrate()
    with _lock:
        for field, value in _overrides.items():
            cfg[field] = value
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["protocol"] = cfg["protocol"] if cfg.get("protocol") in PROTOCOLS else "auto"
    return cfg


def ai_enabled() -> bool:
    """AI 总开关（只在 .env，改完要重启）。"""
    return bool(settings.AI_ENABLED)


def ready() -> bool:
    """默认接口是否真的能用：开关开着 + 地址、密钥、模型都齐。"""
    cfg = resolved()
    return bool(cfg["enabled"] and cfg["baseUrl"] and cfg["key"] and cfg["model"])


# --------------------------------------------------------------------------- #
# 后台卡片
# --------------------------------------------------------------------------- #
def snapshot() -> dict:
    """管理员卡片要的所有信息：生效值、来源、掩码、可用性。"""
    env = env_config()
    cfg = resolved()
    hydrate()
    with _lock:
        overrides = dict(_overrides)

    def source(field: str) -> str:
        return "admin" if field in overrides else "env"

    return {
        "aiEnabled": ai_enabled(),
        "enabled": cfg["enabled"],
        "enabledSource": source("enabled"),
        "baseUrl": cfg["baseUrl"],
        "baseUrlSource": source("baseUrl"),
        # 密钥只回掩码，原文永远不出服务端
        "hasKey": bool(cfg["key"]),
        "keyMask": secret_box.mask_secret(cfg["key"]),
        "keySource": source("key"),
        "model": cfg["model"],
        "modelSource": source("model"),
        "protocol": cfg["protocol"],
        "vision": cfg["vision"],
        "thinking": cfg["thinking"],
        "tools": cfg["tools"],
        "maxInput": cfg["maxInput"],
        "maxOutput": cfg["maxOutput"],
        "ready": ready(),
        # 对照信息：.env 里写了什么，方便管理员判断「清空覆盖」会落到哪
        "env": {
            "enabled": env["enabled"],
            "baseUrl": env["baseUrl"],
            "hasKey": bool(env["key"]),
            "model": env["model"],
        },
    }


# --------------------------------------------------------------------------- #
# 保存
# --------------------------------------------------------------------------- #
def save(db: Session, patch: dict) -> None:
    """保存后台覆盖。字符串传空串 = 清掉该字段的覆盖（回落到 .env）；key 传 __keep__ = 不改。"""
    current = _plain_overrides(db)
    for field, value in patch.items():
        if value is None:
            continue
        if field == "key":
            raw = str(value).strip()
            if raw == "__keep__":
                continue
            if not raw:
                current.pop("key", None)
            else:
                current["key"] = secret_box.encrypt(raw, PURPOSE)
            continue
        if field in ("baseUrl", "model"):
            text = str(value).strip()
            if text:
                current[field] = text
            else:
                current.pop(field, None)
            continue
        if field == "enabled" or field in ("vision", "thinking", "tools"):
            current[field] = bool(value)
            continue
        if field == "protocol":
            current[field] = value if value in PROTOCOLS else "auto"
            continue
        if field in ("maxInput", "maxOutput"):
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            current[field] = max(0, min(2_000_000, number))
    settings_store.set_value(db, STORE_KEY, json.dumps(current, ensure_ascii=False))
    db.commit()
    reload(db)


def clear(db: Session) -> None:
    """清掉全部后台覆盖，回到 .env。"""
    settings_store.set_value(db, STORE_KEY, "")
    db.commit()
    reload(db)


def _plain_overrides(db: Session) -> dict:
    """库里现有的原始覆盖（key 保持密文，便于原样写回）。"""
    return dict(_read_store(db))
