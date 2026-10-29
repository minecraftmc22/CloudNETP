"""用户相关公共逻辑：设置初始化、序列化、审计日志。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from .models import AuditLog, User, UserSetting, utcnow
from .security import client_ip


def ensure_settings(db: Session, user_id: int) -> UserSetting:
    row = db.get(UserSetting, user_id)
    if row is None:
        row = UserSetting(user_id=user_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def iso(dt) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def user_payload(db: Session, user: User) -> dict[str, Any]:
    s = ensure_settings(db, user.id)
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "quotaBytes": user.quota_bytes,
        "usedBytes": user.used_bytes,
        "banned": bool(user.banned),
        "createdAt": iso(user.created_at),
        "lastLoginAt": iso(user.last_login_at),
        "settings": {
            "themeMode": s.theme_mode,
            "accentColor": s.accent_color,
            "wallpaper": s.wallpaper,
            "showMascot": bool(s.show_mascot),
            "showPetals": bool(s.show_petals),
            "viewMode": s.view_mode,
            "navCollapsed": bool(s.nav_collapsed),
        },
    }


def log_action(
    db: Session,
    action: str,
    user_id: int | None = None,
    detail: dict | str | None = None,
    request: Request | None = None,
) -> None:
    if isinstance(detail, dict):
        detail_text = json.dumps(detail, ensure_ascii=False)
    else:
        detail_text = detail
    db.add(
        AuditLog(
            user_id=user_id,
            action=action[:32],
            detail=detail_text,
            ip=client_ip(request) if request else None,
            created_at=utcnow(),
        )
    )
