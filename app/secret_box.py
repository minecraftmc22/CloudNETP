"""服务端凭据的对称加解密（Fernet，密钥由 SECRET_KEY + 用途派生）。

用途（purpose）参与 KDF 的盐：不同用途的密文互相解不开，
但同一个用途的密文在同一个 SECRET_KEY 下始终能解——所以 purpose 一旦上线就不能改。
AI 密钥用 "ai"，盐值与历史实现完全一致（`cloudnetp-ai:<SECRET_KEY>`），老数据照常可解。

密文前缀：
- `enc:` Fernet 密文
- `raw:` 加密库不可用时的明文兜底（别丢配置，至少能继续用）
"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading

from .config import settings

logger = logging.getLogger("uvicorn.error")

_FERNETS: dict[str, object] = {}
_lock = threading.Lock()


def _fernet(purpose: str):
    key = purpose or "default"
    cached = _FERNETS.get(key)
    if cached is not None:
        return cached
    from cryptography.fernet import Fernet

    with _lock:
        cached = _FERNETS.get(key)
        if cached is None:
            digest = hashlib.sha256(f"cloudnetp-{key}:{settings.SECRET_KEY}".encode()).digest()
            cached = Fernet(base64.urlsafe_b64encode(digest))
            _FERNETS[key] = cached
    return cached


def encrypt(raw: str, purpose: str = "ai") -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        return "enc:" + _fernet(purpose).encrypt(text.encode()).decode()
    except Exception:  # pragma: no cover - 加密库异常时退化成明文，别丢配置
        logger.warning("凭据加密失败（%s），改为明文保存", purpose, exc_info=True)
        return "raw:" + text


def decrypt(stored: str, purpose: str = "ai") -> str:
    text = (stored or "").strip()
    if not text:
        return ""
    if text.startswith("raw:"):
        return text[4:]
    if not text.startswith("enc:"):
        return text
    try:
        return _fernet(purpose).decrypt(text[4:].encode()).decode()
    except Exception:
        # SECRET_KEY 变了（例如临时密钥重启）：当作没配置，让管理员重新登录
        logger.warning("凭据解密失败（%s，SECRET_KEY 可能已变），请重新配置", purpose)
        return ""


def mask_secret(value: str, keep: int = 4) -> str:
    """给日志/审计用的掩码：只留尾部几位。"""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "…"
    return "…" + text[-keep:]
