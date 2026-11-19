"""验证码邮件的发件配置（管理员在后台改，服务端热生效）。

为什么单独一层：
- SMTP / BillionMail 的地址与凭据以前只能写在 `.env`，改一次要重启进程；
  现在后台保存即生效，「绑定邮箱」立刻就能收到验证码。
- API Key 与 SMTP 密码属于凭据：加密存放、接口只回掩码。沿用 `secret_box`，
  purpose 固定为 ``mail``（参与 KDF 盐，上线后不能改）。

优先级：**后台保存的 > .env > 内置默认**。后台把普通字段留空 = 继续听 `.env`。

命名约定：`.env` 用 UPPER_SNAKE，JSON / HTTP 用 camelCase，Python 参数用
snake_case，三者一一对应不缩写（`MAIL_BM_TEMPLATE_PATH` ↔ `bmTemplatePath`）。

两套发件通道：
- ``billionmail``：自建发件网关，走它的 HTTP API。``template`` 模式下主题、正文、
  发件人都绑在 BillionMail 后台的「API」里，这边只传验证码变量；
  ``direct`` 模式才由我们提供 subject / html。
- ``smtp``：直接连邮箱服务器，465 走隐式 SSL、587 走 STARTTLS。
"""

from __future__ import annotations

import json
import logging
import threading
import time

from sqlalchemy.orm import Session

from . import secret_box, settings_store
from .config import settings

log = logging.getLogger("uvicorn.error").getChild("cloudnetp.mail")

# system_settings 里的键：存一整个 JSON，没记录就表示「全部跟随 .env」
STORE_KEY = "mail_config"

# purpose 参与派生盐，一旦有数据落库就不能再改
PURPOSE = "mail"

PROVIDERS = {"billionmail", "smtp"}
MODES = {"template", "direct"}
SECURITIES = {"ssl", "starttls", "none"}

# 普通文本字段：传空串 = 删掉后台覆盖，回落 .env
TEXT_FIELDS = (
    "fromName",
    "fromAddr",
    "bmBase",
    "bmTemplatePath",
    "bmDirectPath",
    "bmAddresser",
    "bmCodeVar",
    "smtpHost",
    "smtpUser",
)
# 密文字段：空串或 __keep__ = 不改，要清空用 clearBmKey / clearSmtpPass
SECRET_FIELDS = ("bmKey", "smtpPass")
BOOL_FIELDS = ("enabled",)
ENUM_FIELDS = {"provider": PROVIDERS, "bmMode": MODES, "smtpSecurity": SECURITIES}
# 数值字段统一 clamp 到这个区间，脏数据进不了内存
INT_FIELDS = {"smtpPort": (1, 65535), "codeResendSeconds": (10, 600)}

DEFAULT_CODE_RESEND = 60

DEFAULT_TEMPLATE_PATH = "/api/batch_mail/api/send"
DEFAULT_DIRECT_PATH = "/api/send"

_lock = threading.Lock()
_overrides: dict = {}
_loaded = False
# 读库失败后先等一会儿再试：数据库没就绪时不要让每次「获取验证码」都去敲库
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
    for field, allowed in ENUM_FIELDS.items():
        if raw.get(field) in allowed:
            data[field] = raw[field]
    for field, (low, high) in INT_FIELDS.items():
        if field in raw:
            try:
                data[field] = max(low, min(high, int(raw[field])))
            except (TypeError, ValueError):
                pass
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
        log.warning("读取邮箱服务配置失败，本次先用 .env 配置", exc_info=True)
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
def _env_enabled() -> bool:
    raw = (settings.MAIL_ENABLED or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    # 自动：老部署在 .env 里把 SMTP 配全了就算开启，否则默认关闭
    return bool(settings.MAIL_HOST and settings.MAIL_USER and settings.MAIL_PASSWORD)


def _env_provider() -> str:
    raw = (settings.MAIL_PROVIDER or "").strip().lower()
    if raw in PROVIDERS:
        return raw
    return "smtp" if (settings.MAIL_HOST and settings.MAIL_USER) else "billionmail"


def env_config() -> dict:
    security = (settings.MAIL_SMTP_SECURITY or "").strip().lower()
    if security not in SECURITIES:
        security = "ssl" if settings.MAIL_SSL else "starttls"
    mode = (settings.MAIL_BM_MODE or "").strip().lower()
    return {
        "enabled": _env_enabled(),
        "provider": _env_provider(),
        "fromName": (settings.MAIL_FROM_NAME or "").strip() or "CloudNETP",
        "fromAddr": (settings.MAIL_FROM or "").strip(),
        "bmBase": (settings.MAIL_BM_BASE or "").strip().rstrip("/"),
        "bmTemplatePath": (settings.MAIL_BM_TEMPLATE_PATH or "").strip() or DEFAULT_TEMPLATE_PATH,
        "bmDirectPath": (settings.MAIL_BM_DIRECT_PATH or "").strip() or DEFAULT_DIRECT_PATH,
        "bmMode": mode if mode in MODES else "template",
        "bmKey": (settings.MAIL_BM_KEY or "").strip(),
        "bmAddresser": (settings.MAIL_BM_ADDRESSER or "").strip(),
        "bmCodeVar": (settings.MAIL_BM_CODE_VAR or "").strip() or "code",
        "smtpHost": (settings.MAIL_HOST or "").strip(),
        "smtpPort": int(settings.MAIL_PORT or 465),
        "smtpUser": (settings.MAIL_USER or "").strip(),
        "smtpPass": (settings.MAIL_PASSWORD or "").strip(),
        "smtpSecurity": security,
        # 发码冷却：后台 > .env，默认 60 秒
        "codeResendSeconds": int(settings.MAIL_CODE_RESEND or DEFAULT_CODE_RESEND),
    }


def resolved() -> dict:
    """生效配置（含明文凭据，只许服务端内部用）。"""
    cfg = env_config()
    hydrate()
    with _lock:
        for field, value in _overrides.items():
            cfg[field] = value

    if cfg.get("provider") not in PROVIDERS:
        cfg["provider"] = "billionmail"
    if cfg.get("bmMode") not in MODES:
        cfg["bmMode"] = "template"
    if cfg.get("smtpSecurity") not in SECURITIES:
        cfg["smtpSecurity"] = "ssl"
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["bmCodeVar"] = (cfg.get("bmCodeVar") or "").strip() or "code"
    try:
        cfg["smtpPort"] = max(1, min(65535, int(cfg.get("smtpPort") or 465)))
    except (TypeError, ValueError):
        cfg["smtpPort"] = 465
    try:
        low, high = INT_FIELDS["codeResendSeconds"]
        cfg["codeResendSeconds"] = max(
            low, min(high, int(cfg.get("codeResendSeconds") or DEFAULT_CODE_RESEND))
        )
    except (TypeError, ValueError):
        cfg["codeResendSeconds"] = DEFAULT_CODE_RESEND
    return cfg


def resend_seconds(cfg: dict | None = None) -> int:
    """发码冷却秒数（多次发码的最小间隔）。

    只读进程内缓存，**不需要 db 会话**——`email_code.issue()` 在发码热路径上调用，
    每发一封信都去敲库不值得。后台保存后会 `reload()` 覆盖内存，立刻生效。
    """
    cfg = cfg or resolved()
    try:
        return max(10, min(600, int(cfg.get("codeResendSeconds") or DEFAULT_CODE_RESEND)))
    except (TypeError, ValueError):
        return DEFAULT_CODE_RESEND


def ready(cfg: dict | None = None) -> bool:
    """够不够发一封信。

    只判「参数齐不齐」，**绝不联网**——`/api/meta` 每次都要问它，
    真去探活会把登录页拖慢十几秒。真正的连通性由后台的「发送测试」验证。
    """
    cfg = cfg or resolved()
    if not cfg.get("enabled"):
        return False
    if cfg.get("provider") == "billionmail":
        if not (cfg.get("bmBase") and cfg.get("bmKey")):
            return False
        if cfg.get("bmMode") == "direct":
            return bool(cfg.get("bmDirectPath") and cfg.get("fromAddr"))
        return bool(cfg.get("bmTemplatePath"))
    if not (cfg.get("smtpHost") and cfg.get("fromAddr")):
        return False
    # 要么账号密码都填，要么都不填（内网中继允许免认证）
    return bool(cfg.get("smtpUser")) == bool(cfg.get("smtpPass"))


# --------------------------------------------------------------------------- #
# 后台卡片（永远不含凭据原文）
# --------------------------------------------------------------------------- #
def snapshot() -> dict:
    """管理员卡片要的所有信息：生效值、来源、掩码、可用性。"""
    env = env_config()
    cfg = resolved()
    hydrate()
    with _lock:
        overrides = dict(_overrides)

    def src(field: str, env_value) -> str:
        if field in overrides:
            return "admin"
        if env_value not in ("", 0, False, None):
            return "env"
        return "default"

    explicit_enabled = bool((settings.MAIL_ENABLED or "").strip())
    explicit_provider = (settings.MAIL_PROVIDER or "").strip().lower() in PROVIDERS

    return {
        "enabled": cfg["enabled"],
        "enabledSource": "admin" if "enabled" in overrides else ("env" if explicit_enabled else "auto"),
        "provider": cfg["provider"],
        "providerSource": "admin" if "provider" in overrides else ("env" if explicit_provider else "auto"),
        "fromName": cfg["fromName"],
        "fromNameSource": src("fromName", env["fromName"]),
        "fromAddr": cfg["fromAddr"],
        "fromAddrSource": src("fromAddr", env["fromAddr"]),
        "billionmail": {
            "base": cfg["bmBase"],
            "baseSource": src("bmBase", env["bmBase"]),
            "templatePath": cfg["bmTemplatePath"],
            "templatePathSource": src("bmTemplatePath", "" if settings.MAIL_BM_TEMPLATE_PATH == DEFAULT_TEMPLATE_PATH else settings.MAIL_BM_TEMPLATE_PATH),
            "directPath": cfg["bmDirectPath"],
            "directPathSource": src("bmDirectPath", "" if settings.MAIL_BM_DIRECT_PATH == DEFAULT_DIRECT_PATH else settings.MAIL_BM_DIRECT_PATH),
            "mode": cfg["bmMode"],
            "modeSource": src("bmMode", "" if settings.MAIL_BM_MODE == "template" else settings.MAIL_BM_MODE),
            "addresser": cfg["bmAddresser"],
            "codeVar": cfg["bmCodeVar"],
            # 密钥只回掩码，原文永远不出服务端
            "hasKey": bool(cfg["bmKey"]),
            "keyMask": secret_box.mask_secret(cfg["bmKey"]),
            "keySource": "admin" if "bmKey" in overrides else ("env" if env["bmKey"] else ""),
        },
        "smtp": {
            "host": cfg["smtpHost"],
            "hostSource": src("smtpHost", env["smtpHost"]),
            "port": cfg["smtpPort"],
            "portSource": src("smtpPort", settings.MAIL_PORT if settings.MAIL_PORT != 465 else ""),
            "user": cfg["smtpUser"],
            "userSource": src("smtpUser", env["smtpUser"]),
            "security": cfg["smtpSecurity"],
            "securitySource": src("smtpSecurity", settings.MAIL_SMTP_SECURITY),
            "hasPassword": bool(cfg["smtpPass"]),
            "passwordMask": secret_box.mask_secret(cfg["smtpPass"]),
            "passwordSource": "admin" if "smtpPass" in overrides else ("env" if env["smtpPass"] else ""),
        },
        "ready": ready(cfg),
        # 验证码重发间隔：后台可改，前端只按这个值显示倒计时
        "codeResendSeconds": cfg["codeResendSeconds"],
        "codeResendSecondsSource": (
            "admin"
            if "codeResendSeconds" in overrides
            else ("env" if settings.MAIL_CODE_RESEND else "default")
        ),
        # 对照信息：.env 里有什么，方便管理员判断「留空」会落到哪
        "env": {
            "enabled": env["enabled"],
            "provider": env["provider"],
            "fromAddr": env["fromAddr"],
            "smtpHost": env["smtpHost"],
            "smtpUser": env["smtpUser"],
            "hasPassword": bool(env["smtpPass"]),
            "bmBase": env["bmBase"],
            "hasKey": bool(env["bmKey"]),
        },
    }


# --------------------------------------------------------------------------- #
# 保存
# --------------------------------------------------------------------------- #
def _check_base(value: str) -> str:
    if value and not value.startswith(("http://", "https://")):
        raise ValueError("BillionMail 地址需要以 http:// 或 https:// 开头")
    return value.rstrip("/")


def _clean_text(field: str, value) -> str:
    text = str(value).strip()
    if field == "bmBase":
        text = _check_base(text)
    return text


def save(db: Session, patch: dict) -> dict:
    """保存后台配置，返回最新快照。

    - 普通文本字段传 ``""`` = 删掉该覆盖，回落 .env
    - 密钥传 ``""`` 或 ``"__keep__"`` = **不改**（前端只显示掩码，空输入必须
      理解成「不改」）；要清空走 ``clearBmKey`` / ``clearSmtpPass``
    """
    current = dict(_read_store(db))
    clear_bm_key = bool(patch.pop("clearBmKey", False))
    clear_smtp_pass = bool(patch.pop("clearSmtpPass", False))

    for field, value in patch.items():
        if value is None:
            continue
        if field in SECRET_FIELDS:
            should_clear = clear_bm_key if field == "bmKey" else clear_smtp_pass
            if should_clear:
                current.pop(field, None)
                continue
            raw = str(value).strip()
            if raw and raw != "__keep__":
                current[field] = secret_box.encrypt(raw, PURPOSE)
            continue
        if field in BOOL_FIELDS:
            current[field] = bool(value)
            continue
        if field in ENUM_FIELDS:
            if value in ENUM_FIELDS[field]:
                current[field] = value
            continue
        if field in INT_FIELDS:
            low, high = INT_FIELDS[field]
            try:
                current[field] = max(low, min(high, int(value)))
            except (TypeError, ValueError):
                continue
            continue
        if field in TEXT_FIELDS:
            text = _clean_text(field, value)
            if text:
                current[field] = text
            else:
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


def merge_preview(patch: dict) -> dict:
    """给「发送测试」用：把还没保存的表单值盖到生效配置上。

    与 `save()` 的区别：文本留空**不擦掉**当前生效值（这里只是预览，
    不该把 .env 的地址当成「已删除」）；密钥留空同样表示沿用已保存的。
    """
    cfg = resolved()
    for field, value in patch.items():
        if value is None or field in ("clearBmKey", "clearSmtpPass"):
            continue
        if field in SECRET_FIELDS:
            raw = str(value).strip()
            if raw and raw != "__keep__":
                cfg[field] = raw
            continue
        if field in BOOL_FIELDS:
            cfg[field] = bool(value)
            continue
        if field in ENUM_FIELDS:
            if value in ENUM_FIELDS[field]:
                cfg[field] = value
            continue
        if field in INT_FIELDS:
            low, high = INT_FIELDS[field]
            try:
                cfg[field] = max(low, min(high, int(value)))
            except (TypeError, ValueError):
                pass
            continue
        if field in TEXT_FIELDS:
            text = _clean_text(field, value)
            if text:
                cfg[field] = text
    return cfg
