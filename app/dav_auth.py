"""WebDAV 的 Digest 认证（RFC 7616 的 MD5 子集）。

为什么需要它：
    Windows 的 WebDAV 重定向器（WebClient 服务）受一条策略约束——
    `BasicAuthLevel` 默认是 1，含义是「只允许在 HTTPS 上发送 Basic 凭据」。
    所以对 http:// 的 WebDAV，服务端返回 401 + Basic challenge 时，
    资源管理器不会重试，直接报 0x80070043（找不到网络名）。
    Digest 不受这条策略限制，Windows 会正常带凭据重发，于是开箱即用。

代价：
    Digest 校验需要 HA1 = MD5(username:realm:password)，从 scrypt 哈希推不出来，
    所以 users 表额外存一列 dav_ha1，只在注册 / 改密 / 网页登录 / Basic 登录时写入
    （后两者拿得到明文密码，可以给老账号自动补齐）。

安全说明：
    dav_ha1 等价于「本 realm 下的 MD5(user:realm:password)」，泄露后比 scrypt 弱，
    因此它只用于 WebDAV 认证，正式密码校验永远走 password_hash（scrypt）。
    realm 固定不能再改，改了等于所有 HA1 作废（会被当成未初始化而要求重新登录）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import time
from collections import OrderedDict

from .config import settings

REALM = "CloudNETP WebDAV"
NONCE_TTL = 12 * 3600  # nonce 有效期：Windows 会一直复用同一个，太短会频繁重来
OPAQUE = "cloudnetp-dav"

_MAX_NC = 4096  # 记多少个 (user, cnonce) 的计数器，防重放
_nc_seen: OrderedDict[tuple[str, str], int] = OrderedDict()

_PAIR = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))')


def ha1(username: str, password: str) -> str:
    """HA1 = MD5(lower(username):realm:password)。用户名统一小写，避免大小写不一致。"""
    raw = f"{username.strip().lower()}:{REALM}:{password}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _sign(ts: str) -> str:
    key = settings.SECRET_KEY.encode("utf-8")
    return hmac.new(key, ts.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def make_nonce() -> str:
    ts = str(int(time.time()))
    return base64.b64encode(f"{ts}:{_sign(ts)}".encode()).decode()


def _nonce_ok(nonce: str) -> bool:
    if not nonce:
        return False
    try:
        raw = base64.b64decode(nonce.encode(), validate=True).decode()
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return False
    ts, _, mac = raw.partition(":")
    if not ts.isdigit() or not mac:
        return False
    if not hmac.compare_digest(mac, _sign(ts)):
        return False
    return abs(time.time() - int(ts)) <= NONCE_TTL


def challenge() -> str:
    """401 时下发的 challenge。放在 Basic 之前，Windows 会优先选它。"""
    return (
        f'Digest realm="{REALM}", qop="auth", nonce="{make_nonce()}", '
        f'algorithm=MD5, charset=UTF-8, opaque="{OPAQUE}"'
    )


def parse(header: str) -> dict[str, str] | None:
    if not header or header[:6].lower() != "digest":
        return None
    rest = header[6:].strip()
    if rest.lower().startswith("digest "):  # 极少数客户端会重复写 scheme
        rest = rest[7:].strip()
    out: dict[str, str] = {}
    for match in _PAIR.finditer(rest):
        key = match.group(1).lower()
        out[key] = match.group(2) if match.group(2) is not None else match.group(3)
    return out or None


def _nc_ok(username: str, cnonce: str, nc: str) -> bool:
    """qop=auth 时客户端每次请求都应递增 nc，同一个 nc 复用视为重放。"""
    try:
        value = int(nc, 16)
    except (TypeError, ValueError):
        return False
    key = (username.lower(), cnonce or "")
    last = _nc_seen.get(key)
    if last is not None and value <= last:
        return False
    _nc_seen[key] = value
    _nc_seen.move_to_end(key)
    while len(_nc_seen) > _MAX_NC:
        _nc_seen.popitem(last=False)
    return True


def verify(
    header: str,
    method: str,
    uris: set[str],
    username: str,
    stored_ha1: str,
) -> tuple[bool, str]:
    """校验 Authorization 头。返回 (是否通过, 失败原因)。"""
    data = parse(header)
    if not data:
        return False, "认证头格式不正确"
    if data.get("realm") != REALM:
        return False, "realm 不匹配"
    if not _nonce_ok(data.get("nonce", "")):
        return False, "nonce 已失效，请重试"
    if data.get("opaque") and data.get("opaque") != OPAQUE:
        return False, "opaque 不匹配"

    uri = data.get("uri") or ""
    # uri 只参与摘要计算、不用于定位资源，所以这里放宽比对
    # （客户端可能发 /dav、/dav/ 或百分号编码形式）
    if uri not in uris:
        return False, "请求路径不匹配"

    qop = (data.get("qop") or "").lower()
    ha2 = _md5(f"{method.upper()}:{uri}")
    nonce = data["nonce"]
    if qop == "auth":
        if not data.get("nc") or not data.get("cnonce"):
            return False, "缺少 nc / cnonce"
        expect = _md5(f"{stored_ha1}:{nonce}:{data['nc']}:{data['cnonce']}:auth:{ha2}")
    elif qop in ("", "none"):
        expect = _md5(f"{stored_ha1}:{nonce}:{ha2}")
    else:
        return False, f"不支持的 qop：{qop}"

    given = (data.get("response") or "").lower()
    if not given or not hmac.compare_digest(expect, given):
        return False, "用户名或密码错误"
    if qop == "auth" and not _nc_ok(username, data.get("cnonce", ""), data.get("nc")):
        return False, "nc 重放"
    return True, ""
