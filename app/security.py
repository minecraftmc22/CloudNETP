"""密码哈希、会话令牌、签名 URL、来源校验 —— 全部基于标准库。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from .config import settings

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


# --------------------------------------------------------------------------- #
# 密码
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=32,
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


def password_problem(password: str) -> str | None:
    if len(password) < 8:
        return "密码至少 8 位"
    if len(password) > 128:
        return "密码太长（上限 128 位）"
    if password.strip() != password:
        return "密码首尾不能是空格"
    if password.isdigit():
        return "密码不能是纯数字"
    return None


# --------------------------------------------------------------------------- #
# 会话令牌
# --------------------------------------------------------------------------- #
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload: bytes) -> str:
    return _b64e(hmac.new(settings.SECRET_KEY.encode(), payload, hashlib.sha256).digest())


def create_token(user_id: int, token_version: int, ttl: int | None = None) -> str:
    payload = {
        "u": user_id,
        "v": token_version,
        "exp": int(time.time()) + (ttl or settings.SESSION_TTL_SECONDS),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return f"{_b64e(raw)}.{_sign(raw)}"


def decode_token(token: str) -> dict | None:
    try:
        body_b64, sig = token.split(".")
        raw = _b64d(body_b64)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(_sign(raw), sig):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


# --------------------------------------------------------------------------- #
# 短期签名 URL（供 <video>/<audio>/分享下载这类无法带 header 的场景）
# --------------------------------------------------------------------------- #
def sign_resource(scope: str, resource_id: int, ttl: int = 300, extra: str = "") -> str:
    exp = int(time.time()) + ttl
    raw = f"{scope}:{resource_id}:{exp}:{extra}".encode()
    return f"{exp}.{_sign(raw)}"


def verify_resource(scope: str, resource_id: int, token: str, extra: str = "") -> bool:
    try:
        exp_s, sig = token.split(".")
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if exp < int(time.time()):
        return False
    raw = f"{scope}:{resource_id}:{exp}:{extra}".encode()
    return hmac.compare_digest(_sign(raw), sig)


# --------------------------------------------------------------------------- #
# 请求来源 / IP
# --------------------------------------------------------------------------- #
def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return (request.client.host if request.client else "")[:45]


def check_origin(request: Request) -> None:
    """写操作来源校验：比 CSRF token 简单，且对 fetch 天然友好。"""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        # 部分原生客户端（WebDAV/RaiDrive）不发 Origin，交给其它机制兜底
        return
    host = request.headers.get("host", "")
    origin_host = urlsplit(origin).netloc
    if not origin_host:
        return
    # 反向代理会改写 Host，SITE_URL 里配置的域名同样算合法来源
    allowed = {h for h in (host, settings.site_netloc, urlsplit(settings.public_base).netloc) if h}
    if origin_host not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "来源校验失败")


# --------------------------------------------------------------------------- #
# 名称校验
# --------------------------------------------------------------------------- #
_USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fa5.-]{2,32}$")
_ILLEGAL_NAME_CHARS = set('/\\:*?"<>|\0\n\r\t')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)
}


def username_problem(username: str) -> str | None:
    if not _USERNAME_RE.match(username):
        return "用户名 2-32 位，仅支持中英文、数字、下划线、点、连字符"
    return None


def normalize_name(name: str) -> str:
    """文件名只做展示，永不参与磁盘路径拼接；这里仍拒绝危险字符以便 WebDAV 兼容。"""
    name = (name or "").strip().strip(".")
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "名称不能为空")
    if len(name) > 255:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "名称过长（上限 255 字符）")
    if _ILLEGAL_NAME_CHARS & set(name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, '名称不能包含 \\ / : * ? " < > | 等字符')
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该名称是系统保留名")
    return name
