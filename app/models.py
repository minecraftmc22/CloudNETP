"""ORM 模型。字段全部使用标准 SQL 类型，MySQL / SQLite 双兼容。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

# SQLite 只有 INTEGER PRIMARY KEY 才是 rowid 别名，BIGINT 不会自增
AutoPK = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    """统一存 naive UTC，避开 MySQL DATETIME 与时区方言差异。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # 展示资料（与登录名解耦）：昵称可改可重复，头像只存文件名
    display_name: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bio: Mapped[str | None] = mapped_column(String(200), nullable=True)
    avatar: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # WebDAV Digest 用的 HA1 = MD5(lower(username):realm:password)
    # 只在注册 / 改密 / 登录时写入，老账号为 NULL 时需先登录一次补齐
    dav_ha1: Mapped[str | None] = mapped_column(String(32), nullable=True)
    role: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # 0 普通 1 管理员
    quota_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=10 * 1024**3)
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    banned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (CheckConstraint("role IN (0, 1)", name="ck_users_role"),)


class UserSetting(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    theme_mode: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=2)  # 0 亮 1 暗 2 跟随
    accent_color: Mapped[str] = mapped_column(String(16), nullable=False, default="#c084fc")
    wallpaper: Mapped[str] = mapped_column(String(64), nullable=False, default="sakura")
    show_mascot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    show_petals: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    trash_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    view_mode: Mapped[str] = mapped_column(String(8), nullable=False, default="grid")  # grid / list
    nav_collapsed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 分享页是否显示分享者完整昵称（关闭只显示前两位）
    share_show_name: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Blob(Base):
    """内容寻址存储：同 sha256 只落一份盘，天然去重与秒传。"""

    __tablename__ = "blobs"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ref_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class Node(Base):
    """统一节点树：文件夹与文件同表，移动/改名/软删都是单行 UPDATE。"""

    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # NULL = 用户根目录
    type: Mapped[str] = mapped_column(String(6), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    blob_sha256: Mapped[str | None] = mapped_column(ForeignKey("blobs.sha256"), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    mime: Mapped[str | None] = mapped_column(String(127), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    restore_parent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        CheckConstraint("type IN ('folder', 'file')", name="ck_nodes_type"),
        Index("idx_node_parent", "user_id", "parent_id", "deleted_at"),
        Index("idx_node_search", "user_id", "deleted_at", "name"),
        Index("idx_node_trash", "user_id", "deleted_at"),
        Index("idx_node_blob", "blob_sha256"),
    )


class UploadSession(Base):
    __tablename__ = "upload_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime: Mapped[str | None] = mapped_column(String(127), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    target_node_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="uploading")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("status IN ('uploading', 'completed', 'aborted')", name="ck_sess_status"),
        Index("idx_sess_user", "user_id", "status"),
    )


class UploadChunk(Base):
    __tablename__ = "upload_chunks"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("upload_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class Share(Base):
    __tablename__ = "shares"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    node_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token: Mapped[str] = mapped_column(String(43), unique=True, nullable=False, index=True)
    extract_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    max_downloads: Mapped[int | None] = mapped_column(Integer, nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("idx_share_user", "user_id", "created_at"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        Index("idx_log_user", "user_id", "created_at"),
        Index("idx_log_action", "action", "created_at"),
    )


class SystemSetting(Base):
    __tablename__ = "system_settings"

    k: Mapped[str] = mapped_column(String(64), primary_key=True)
    v: Mapped[str] = mapped_column(Text, nullable=False)
