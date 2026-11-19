"""通行密钥（WebAuthn / Passkey）的服务端封装。

设计要点：
- **懒加载依赖**：`webauthn` 是新加的第三方包，万一装不上（离线部署、老镜像），
  不能让整个站点起不来。`available()` 为假时只有通行密钥相关接口回 503，
  其余功能（含邮箱两步验证）照常工作。
- **RP ID / origin**：优先 `SITE_URL`，没配就用请求的 Host / X-Forwarded-Proto。
  浏览器要求安全上下文（HTTPS 或 localhost），非安全上下文直接 400，
  别让用户看到「注册失败」却不知道是协议问题。
- 只做「解析 + 校验」，不碰数据库；落库与因子判断在 `routers/passkeys.py`。
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from .config import settings

log = logging.getLogger("uvicorn.error").getChild("cloudnetp.passkey")

try:  # pragma: no cover - 依赖缺失时走降级分支
    import webauthn as _lib
    from webauthn import (
        generate_authentication_options,
        generate_registration_options,
        options_to_json,
        verify_authentication_response,
        verify_registration_response,
    )
    from webauthn.helpers import bytes_to_base64url
    from webauthn.helpers.structs import (
        AttestationConveyancePreference,
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    _IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover
    _lib = None
    _IMPORT_ERROR = str(exc)
    log.warning("未安装 webauthn，通行密钥功能不可用：%s", _IMPORT_ERROR)

# challenge 有效期（毫秒）交给浏览器，与 webauthn_chal.TTL 对齐
TIMEOUT_MS = 120_000
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def available() -> bool:
    return _lib is not None


def require() -> None:
    if _lib is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "服务端未安装通行密钥依赖（webauthn），暂时无法使用",
        )


def b64e(raw: bytes) -> str:
    return bytes_to_base64url(raw)


def rp_and_origin(request: Request) -> tuple[str, str]:
    """推 rpId 与 origin。SITE_URL 优先，其次看请求头（反代要透传 Host）。"""
    base = settings.SITE_URL
    if not base:
        host = (request.headers.get("host") or "").strip()
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        if proto not in ("http", "https"):
            proto = "https" if request.url.scheme == "https" else "http"
        base = f"{proto}://{host}" if host else settings.public_base
    parts = urlsplit(base)
    rp_id = settings.WEBAUTHN_RP_ID or (parts.hostname or "localhost")
    origin = settings.WEBAUTHN_ORIGIN or f"{parts.scheme}://{parts.netloc}"
    return rp_id, origin


def ensure_secure(rp_id: str, origin: str) -> None:
    """WebAuthn 只在安全上下文可用：HTTPS，或 localhost。"""
    if origin.startswith("https://"):
        return
    if rp_id in _LOCAL_HOSTS or rp_id.endswith(".localhost"):
        return
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST, "通行密钥需要 HTTPS 访问（localhost 除外）"
    )


def _descriptors(credential_ids: list[str]) -> list:
    return [PublicKeyCredentialDescriptor(id=_to_bytes(cid)) for cid in credential_ids if cid]


def _to_bytes(text: str) -> bytes:
    """base64url 文本 → 原始字节（补 padding，容错 URL-safe 字符）。"""
    raw = (text or "").strip().replace("-", "+").replace("_", "/")
    raw += "=" * (-len(raw) % 4)
    import base64

    return base64.b64decode(raw)


def registration_options(
    rp_id: str, user, existing_ids: list[str], challenge: bytes
) -> dict:
    """生成 navigator.credentials.create 的 publicKey 选项。"""
    require()
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=settings.APP_NAME,
        user_id=str(user.id).encode(),
        user_name=user.username,
        user_display_name=(user.display_name or user.username),
        timeout=TIMEOUT_MS,
        challenge=challenge,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=_descriptors(existing_ids),
    )
    return json.loads(options_to_json(options))


def verify_registration(
    rp_id: str, origin: str, challenge: bytes, credential: dict
) -> dict:
    """校验注册响应，返回落库需要的字段。"""
    require()
    verified = verify_registration_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=rp_id,
        expected_origin=origin,
        # 第一因子已经是密码，这里不强制 UV（否则没 PIN 的安全密钥注册不了）
        require_user_verification=False,
    )
    aaguid = getattr(verified, "aaguid", "") or ""
    if isinstance(aaguid, (bytes, bytearray)):
        aaguid = aaguid.hex()
    response = (credential or {}).get("response") or {}
    transports = response.get("transports") or []
    if not isinstance(transports, list):
        transports = []
    return {
        "credential_id": b64e(verified.credential_id),
        "public_key": verified.credential_public_key,
        "sign_count": int(verified.sign_count or 0),
        "aaguid": str(aaguid)[:36],
        "backed_up": bool(getattr(verified, "credential_backed_up", False)),
        "transports": ",".join(str(t)[:16] for t in transports)[:64],
    }


def authentication_options(rp_id: str, allow_ids: list[str], challenge: bytes) -> dict:
    """生成 navigator.credentials.get 的 publicKey 选项。"""
    require()
    options = generate_authentication_options(
        rp_id=rp_id,
        timeout=TIMEOUT_MS,
        challenge=challenge,
        allow_credentials=_descriptors(allow_ids),
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return json.loads(options_to_json(options))


def verify_authentication(
    rp_id: str,
    origin: str,
    challenge: bytes,
    credential: dict,
    public_key: bytes,
    sign_count: int,
) -> dict:
    """校验登录断言。

    ``sign_count`` 为 0 的认证器很多（平台密钥几乎都恒返回 0），
    所以「计数回退」只在两侧都非 0 时才算异常，避免误杀正常用户。
    """
    require()
    verified = verify_authentication_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=rp_id,
        expected_origin=origin,
        credential_public_key=public_key,
        credential_current_sign_count=int(sign_count or 0),
        require_user_verification=False,
    )
    new_count = int(verified.new_sign_count or 0)
    clone = bool(sign_count) and bool(new_count) and new_count <= int(sign_count)
    return {"sign_count": new_count, "clone": clone}
