"""请求/响应模型。

约定：JSON 一律 camelCase（前端习惯），Python 内部一律 snake_case。
通过 alias_generator + populate_by_name 双向兼容，避免手写映射。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RegisterIn(CamelModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class LoginIn(CamelModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class PasswordIn(CamelModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class PasswordOnlyIn(CamelModel):
    password: str = Field(min_length=1, max_length=128)


class SettingsIn(CamelModel):
    theme_mode: Literal[0, 1, 2] | None = None
    accent_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    wallpaper: str | None = Field(default=None, max_length=64)
    show_mascot: bool | None = None
    show_petals: bool | None = None
    view_mode: Literal["grid", "list"] | None = None
    nav_collapsed: bool | None = None
    share_show_name: bool | None = None
    # 歌词播放器样式（fontSize/hotColor/showTrans），整体覆盖
    lyrics_style: dict | None = None


class ProfileIn(CamelModel):
    """昵称与简介。字段为 None 表示不改；传空串表示清空（昵称回落成用户名）。"""

    display_name: str | None = Field(default=None, max_length=64)
    bio: str | None = Field(default=None, max_length=400)


class TrashSwitchIn(CamelModel):
    """回收站开关。关闭时必须在服务端核验验证码（不放进 SettingsIn，防止被绕过）。"""

    enabled: bool
    code: str = Field(default="", max_length=16)


# --------------------------------------------------------------------------- #
# 文件与目录
# --------------------------------------------------------------------------- #
class FolderIn(CamelModel):
    parent_id: int | None = None
    name: str = Field(min_length=1, max_length=255)


class RenameIn(CamelModel):
    name: str = Field(min_length=1, max_length=255)


class MoveIn(CamelModel):
    parent_id: int | None = None


# --------------------------------------------------------------------------- #
# 歌词偏好
# --------------------------------------------------------------------------- #
class LyricsPrefIn(CamelModel):
    """每首歌的歌词来源偏好。mode 决定哪些字段有效。"""

    mode: Literal["auto", "song", "custom", "none"]
    song_id: int | None = None
    song_title: str | None = Field(default=None, max_length=200)
    song_artist: str | None = Field(default=None, max_length=200)
    custom_lyrics: str | None = Field(default=None, max_length=200_000)


class PurgeIn(CamelModel):
    ids: list[int] = Field(default_factory=list, max_length=500)


# --------------------------------------------------------------------------- #
# 分片上传
# --------------------------------------------------------------------------- #
class UploadInitIn(CamelModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    parent_id: int | None = None
    sha256: str | None = Field(default=None, max_length=64)
    mime: str | None = Field(default=None, max_length=127)
    chunk_size: int | None = Field(default=None, ge=64 * 1024, le=32 * 1024 * 1024)



# --------------------------------------------------------------------------- #
# 分享链接
# --------------------------------------------------------------------------- #
class ShareIn(CamelModel):
    node_id: int
    expires_days: int | None = Field(default=None, ge=1, le=3650)
    extract_code: str | None = Field(default=None, max_length=16)
    max_downloads: int | None = Field(default=None, ge=1, le=1_000_000)


class ShareUnlockIn(CamelModel):
    code: str = Field(min_length=1, max_length=16)


# --------------------------------------------------------------------------- #
# 管理后台
# --------------------------------------------------------------------------- #
class AdminUserPatchIn(CamelModel):
    quota_bytes: int | None = Field(default=None, ge=0, le=1024**5)
    banned: bool | None = None
    role: Literal[0, 1] | None = None


class AdminSettingsIn(CamelModel):
    site_name: str | None = Field(default=None, min_length=1, max_length=64)
    site_announcement: str | None = Field(default=None, max_length=500)
    allow_register: bool | None = None
    default_quota_bytes: int | None = Field(default=None, ge=0, le=1024**5)
    max_file_size_bytes: int | None = Field(default=None, ge=0, le=1024**5)
    chunk_size_bytes: int | None = Field(default=None, ge=64 * 1024, le=32 * 1024 * 1024)
    trash_retention_days: int | None = Field(default=None, ge=0, le=3650)


class AdminLogPurgeIn(CamelModel):
    days: int = Field(ge=1, le=3650)


class AdminGcIn(CamelModel):
    apply: bool = False
