"""系统设置的键值存取（带默认值与类型转换）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .config import settings
from .models import SystemSetting

DEFAULTS: dict[str, str] = {
    "allow_register": "1" if settings.ALLOW_REGISTER else "0",
    "site_name": settings.APP_NAME,
    "site_announcement": "",
    "default_quota_bytes": str(settings.default_quota_bytes),
    "max_file_size_bytes": str(settings.max_file_size_bytes),
    "chunk_size_bytes": str(settings.chunk_size_bytes),
    "trash_retention_days": "30",
}


def get_value(db: Session, key: str) -> str:
    row = db.get(SystemSetting, key)
    if row is not None:
        return row.v
    return DEFAULTS.get(key, "")


def get_flag(db: Session, key: str) -> bool:
    return get_value(db, key).strip() in {"1", "true", "yes", "on"}


def get_int(db: Session, key: str) -> int:
    try:
        return int(get_value(db, key))
    except (TypeError, ValueError):
        return 0


def set_value(db: Session, key: str, value: str) -> None:
    row = db.get(SystemSetting, key)
    if row is None:
        db.add(SystemSetting(k=key, v=value))
    else:
        row.v = value


def all_values(db: Session) -> dict[str, str]:
    merged = dict(DEFAULTS)
    for row in db.query(SystemSetting).all():
        merged[row.k] = row.v
    return merged
