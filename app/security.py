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
    """密码规则：8-128 位、必须同时含数字与小写字母、只允许可见 ASCII。

    大写字母和符号是可选的（加了更安全，但不强制）。
    非 ASCII 一律拒绝——emoji、全角字符、带变体选择符的组合字符在不同输入法下
    长度和比对结果不一致，很容易出现「明明输对了却登不上」。
    """
    if len(password) < 8:
        return "密码至少 8 位"
    if len(password) > 128:
        return "密码太长（上限 128 位）"
    if password.strip() != password:
        return "密码首尾不能是空格"
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in password):
        return "密码只能用键盘上的英文、数字和符号，不支持中文、空格和 emoji"
    if not any(ch.isdigit() for ch in password):
        return "密码需要包含数字"
    if not any("a" <= ch <= "z" for ch in password):
        return "密码需要包含小写字母"
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
# 找回密码令牌
# --------------------------------------------------------------------------- #
def sign_reset(user_id: int, password_hash: str, ttl: int = 900) -> str:
    """签发一次性重置令牌。

    签名里带 password_hash 的尾巴：密码一改，旧令牌立刻失效，
    不需要额外存表，也不会出现「重置完还能再重置一次」。
    """
    exp = int(time.time()) + ttl
    raw = f"pwreset:{user_id}:{exp}:{password_hash[-16:]}".encode()
    return f"{user_id}.{exp}.{_sign(raw)}"


def verify_reset(token: str, password_hash: str) -> int | None:
    """校验令牌，返回 user_id；失败返回 None。"""
    try:
        uid_s, exp_s, sig = token.split(".")
        uid, exp = int(uid_s), int(exp_s)
    except (ValueError, AttributeError):
        return None
    if exp < int(time.time()):
        return None
    raw = f"pwreset:{uid}:{exp}:{password_hash[-16:]}".encode()
    if not hmac.compare_digest(_sign(raw), sig):
        return None
    return uid


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
_USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fa5.-]{4,32}$")
_ILLEGAL_NAME_CHARS = set('/\\:*?"<>|\0\n\r\t')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)
}


def username_problem(username: str) -> str | None:
    if len(username) < 4:
        return "用户名至少 4 个字符"
    if not _USERNAME_RE.match(username):
        return "用户名 4-32 位，仅支持中英文、数字、下划线、点、连字符"
    return None


# 只做「看起来像邮箱」的宽松校验：真正的有效性靠验证码证明
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(\.[A-Za-z0-9\-]{1,63})+$")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def email_problem(email: str) -> str | None:
    email = normalize_email(email)
    if not email:
        return None  # 邮箱是可选的
    if len(email) > 254:
        return "邮箱地址过长"
    if not _EMAIL_RE.match(email):
        return "邮箱格式不正确"
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
