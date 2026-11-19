"""通行密钥（WebAuthn）的设置页接口。

只做「已登录用户管理自己的凭据」：security 概览、注册 options、落库、删除。
登录第二步的断言接口在 `routers/auth.py`（那边持有待验证票据的处理逻辑）。
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import email_code, mail_config, passkey_service, twofa, webauthn_chal
from ..db import get_db
from ..deps import get_current_user
from ..models import Passkey, User, utcnow
from ..schemas import PasskeyAuthIn, PasskeyCreateIn, PasswordOnlyIn
from ..security import check_origin, normalize_email, verify_password
from ..user_service import log_action

router = APIRouter(prefix="/api", tags=["passkeys"])

CREDENTIAL_ID_MAX = 255


def _payload(db: Session, user: User) -> dict:
    rows = db.scalars(
        select(Passkey).where(Passkey.user_id == user.id).order_by(Passkey.id.asc())
    ).all()
    return {
        "twoFactorEnabled": bool(user.two_factor_enabled),
        "email": user.email or "",
        "emailVerified": bool(user.email_verified),
        "emailFactorAvailable": twofa.email_factor_ready(user),
        "mailReady": mail_config.ready(),
        # 服务端装了 webauthn 才谈得上添加；前端还要再看 isSecureContext
        "passkeySupported": passkey_service.available(),
        "passkeys": [
            {
                "id": row.id,
                "label": row.label or "",
                "createdAt": row.created_at.isoformat(timespec="seconds") + "Z"
                if row.created_at
                else None,
                "lastUsedAt": row.last_used_at.isoformat(timespec="seconds") + "Z"
                if row.last_used_at
                else None,
                "backedUp": bool(row.backed_up),
            }
            for row in rows
        ],
    }


def _confirm_identity(db: Session, user: User, password: str, email_code_text: str) -> str:
    """添加通行密钥前的身份确认：密码与邮箱验证码二选一。

    为什么两个都允许：没绑邮箱的账号只能靠密码；只有通行密钥、忘了密码的人
    还能靠邮箱进来补一台设备。返回用了哪种方式，写进审计。
    """
    password = (password or "").strip()
    email_code_text = (email_code_text or "").strip()

    if password and email_code_text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "密码与邮箱验证码只需选一种验证")
    if password:
        if not verify_password(password, user.password_hash):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "账户密码不正确")
        return "password"
    if email_code_text:
        if not twofa.email_factor_ready(user):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "请改用账户密码验证")
        passed, reason = email_code.verify("password", normalize_email(user.email), email_code_text)
        if not passed:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, reason)
        return "email"
    raise HTTPException(status.HTTP_400_BAD_REQUEST, "请先用密码或邮箱验证码验证身份")


@router.get("/me/security")
def security_overview(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    """设置页「两步验证」「通行密钥」两张卡片需要的全部状态。"""
    return _payload(db, user)


@router.post("/me/passkeys/options")
def passkey_options(
    payload: PasskeyAuthIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """确认身份后下发注册选项（challenge 存在服务端，前端只拿到 challenge 值）。"""
    check_origin(request)
    passkey_service.require()
    rp_id, origin = passkey_service.rp_and_origin(request)
    passkey_service.ensure_secure(rp_id, origin)

    _confirm_identity(db, user, payload.password, payload.email_code)

    existing = db.scalars(select(Passkey.credential_id).where(Passkey.user_id == user.id)).all()
    challenge = secrets.token_bytes(32)
    nonce = webauthn_chal.put("reg", user.id, challenge)
    options = passkey_service.registration_options(rp_id, user, list(existing), challenge)
    return {"options": options, "nonce": nonce}


@router.post("/me/passkeys", status_code=status.HTTP_201_CREATED)
def create_passkey(
    payload: PasskeyCreateIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """校验注册响应并落库。"""
    check_origin(request)
    passkey_service.require()
    rp_id, origin = passkey_service.rp_and_origin(request)
    passkey_service.ensure_secure(rp_id, origin)

    challenge = webauthn_chal.pop("reg", user.id, payload.nonce)
    if challenge is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证已过期，请重新添加")

    try:
        verified = passkey_service.verify_registration(
            rp_id, origin, challenge, payload.credential
        )
    except HTTPException:
        raise
    except Exception:  # 库内部异常种类很多，统一按「这个凭据不被接受」处理
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "无法验证这个通行密钥，请重试") from None

    credential_id = verified["credential_id"]
    if len(credential_id) > CREDENTIAL_ID_MAX:
        # 截断会让后续断言永远失败，宁可明确拒绝
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "该认证器生成的凭据过长，请换一个设备或密钥"
        )
    if db.scalar(select(Passkey.id).where(Passkey.credential_id == credential_id)):
        raise HTTPException(status.HTTP_409_CONFLICT, "这个设备已经添加过了")

    label = (payload.label or "").strip()[:40]
    row = Passkey(
        user_id=user.id,
        credential_id=credential_id,
        public_key=verified["public_key"],
        sign_count=verified["sign_count"],
        transports=verified["transports"] or None,
        aaguid=verified["aaguid"] or None,
        label=label,
        backed_up=verified["backed_up"],
        created_at=utcnow(),
    )
    db.add(row)
    log_action(db, "passkey_add", user.id, {"label": label, "backedUp": row.backed_up}, request)
    db.commit()
    db.refresh(row)

    data = _payload(db, user)
    data["added"] = row.id
    return data


@router.delete("/me/passkeys/{passkey_id}")
def delete_passkey(
    passkey_id: int,
    payload: PasswordOnlyIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """移除通行密钥。删除最后一个因子时拦住，避免两步验证把自己锁死。"""
    check_origin(request)
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "账户密码不正确")

    row = db.scalar(
        select(Passkey).where(Passkey.id == passkey_id, Passkey.user_id == user.id)
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "该通行密钥不存在")

    if user.two_factor_enabled and twofa.factors_left(db, user, removing_passkeys=1) == 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "这是最后一种两步验证方式，请先关闭两步验证再移除",
        )

    label = row.label or ""
    db.delete(row)
    log_action(db, "passkey_remove", user.id, {"label": label}, request)
    db.commit()
    return _payload(db, user)
