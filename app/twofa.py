"""两步验证的「因子」判定，登录流程与设置页共用。

单独一层的理由：同一套判断要在四处用到（登录分支、开关、添加/删除通行密钥、
改密码降级），散着写迟早出现「这个接口认为有邮箱、那个接口认为没有」的错位。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import mail_config
from .models import Passkey, User


def email_factor_ready(user: User) -> bool:
    """邮箱能不能当第二因子：绑了 + 验过 + 服务端现在真能发信。"""
    return bool(user.email and user.email_verified and mail_config.ready())


def passkey_count(db: Session, user_id: int) -> int:
    return int(
        db.scalar(select(func.count()).select_from(Passkey).where(Passkey.user_id == user_id))
        or 0
    )


def login_methods(db: Session, user: User) -> list[str]:
    """登录第二步可用的方式，顺序即前端默认展示顺序（通行密钥优先）。"""
    methods: list[str] = []
    if passkey_count(db, user.id):
        methods.append("passkey")
    if email_factor_ready(user):
        methods.append("email")
    return methods


def factors_left(db: Session, user: User, removing_passkeys: int = 0) -> int:
    """删掉这些通行密钥之后还剩几种第二因子。

    用于「别把用户锁在门外」：两步验证开着、且这是最后一个因子时必须拦住。
    """
    remain = max(passkey_count(db, user.id) - max(removing_passkeys, 0), 0)
    return remain + (1 if email_factor_ready(user) else 0)
