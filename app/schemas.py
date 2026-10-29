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


class SettingsIn(CamelModel):
    theme_mode: Literal[0, 1, 2] | None = None
    accent_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    wallpaper: str | None = Field(default=None, max_length=64)
    show_mascot: bool | None = None
    show_petals: bool | None = None
    view_mode: Literal["grid", "list"] | None = None
    nav_collapsed: bool | None = None


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

