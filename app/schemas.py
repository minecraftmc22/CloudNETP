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
    username: str = Field(min_length=4, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    # 邮箱可选：还没接邮件服务，先存下来，绑不绑都不影响使用
    email: str | None = Field(default=None, max_length=254)
    captcha_id: str = Field(default="", max_length=64)
    captcha: str = Field(default="", max_length=16)


class LoginIn(CamelModel):
    # 账号：用户名或邮箱，服务端都能认
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    captcha_id: str = Field(default="", max_length=64)
    captcha: str = Field(default="", max_length=16)


class EmailCodeIn(CamelModel):
    """申请邮箱验证码。scene: register / bind / reset"""

    scene: Literal["register", "bind", "reset"]
    target: str = Field(min_length=1, max_length=254)
    # 前端生成的设备标识，仅用于「同一设备」限流，不涉及隐私
    device: str = Field(default="", max_length=64)


class EmailVerifyIn(CamelModel):
    scene: Literal["register", "bind", "reset"]
    target: str = Field(min_length=1, max_length=254)
    code: str = Field(min_length=1, max_length=16)


class ForgotStartIn(CamelModel):
    # 用户名或邮箱
    account: str = Field(min_length=1, max_length=254)


class ForgotCheckIn(CamelModel):
    """用用户名重置密码时，需要填对绑定的邮箱。"""

    account: str = Field(min_length=1, max_length=254)
    email: str = Field(min_length=1, max_length=254)


class ResetPasswordIn(CamelModel):
    token: str = Field(min_length=8, max_length=512)
    password: str = Field(min_length=8, max_length=128)


class EmailBindIn(CamelModel):
    email: str = Field(min_length=3, max_length=254)


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
    # 各模块背景不透明度（nav/titlebar/panel/dialog → 0-100），整体覆盖
    surface_opacity: dict | None = None


class ProfileIn(CamelModel):
    """昵称与简介。字段为 None 表示不改；传空串表示清空（昵称回落成用户名）。"""

    display_name: str | None = Field(default=None, max_length=64)
    bio: str | None = Field(default=None, max_length=400)


class TrashSwitchIn(CamelModel):
    """回收站开关。关闭时必须在服务端核验验证码（不放进 SettingsIn，防止被绕过）。"""

    enabled: bool
    code: str = Field(default="", max_length=16)


# --------------------------------------------------------------------------- #
# 看板娘 AI 对话
# --------------------------------------------------------------------------- #
class AiConfigIn(CamelModel):
    """AI 接口配置。key 缺省 = 不改；空串 = 清空；"__keep__" 也按不改处理。"""

    provider: Literal["", "openai", "deepseek", "custom"] | None = None
    base_url: str | None = Field(default=None, max_length=300)
    model: str | None = Field(default=None, max_length=120)
    key: str | None = Field(default=None, max_length=400)
    tools: bool | None = None
    vision: bool | None = None
    thinking: bool | None = None
    # auto = 按地址自动判断；openai / anthropic 为手动指定（给非标准地址用）
    protocol: Literal["auto", "openai", "anthropic"] | None = None
    max_input: int | None = Field(default=None, ge=0, le=2_000_000)
    max_output: int | None = Field(default=None, ge=0, le=2_000_000)


class AiProbeIn(CamelModel):
    """测试连接：允许先不保存，用页面上填的地址/密钥试一次。"""

    base_url: str = Field(default="", max_length=300)
    key: str = Field(default="", max_length=400)
    protocol: Literal["auto", "openai", "anthropic"] = "auto"


class AiMessageIn(CamelModel):
    role: Literal["user", "assistant"] = "user"
    content: str = Field(default="", max_length=20000)
    # 图片输入：前端压好尺寸的 data URL（data:image/webp;base64,...）
    images: list[str] = Field(default_factory=list)


class AiChatIn(CamelModel):
    messages: list[AiMessageIn] = Field(default_factory=list, max_length=60)
    # 管理员维护的系统提示词只读预览用不到，这里留给将来扩展
    stream: bool = True


class AiPromptIn(CamelModel):
    prompt: str = Field(default="", max_length=8000)


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
    node_id: int | None = None
    # 合集分享：一次选中多项；与 nodeId 二选一，同时给时以 nodeIds 为准
    node_ids: list[int] | None = Field(default=None, max_length=100)
    expires_days: int | None = Field(default=None, ge=1, le=3650)
    extract_code: str | None = Field(default=None, max_length=16)
    max_downloads: int | None = Field(default=None, ge=1, le=1_000_000)


class ShareUnlockIn(CamelModel):
    code: str = Field(min_length=1, max_length=16)


class ShareReportIn(CamelModel):
    reason: str | None = Field(default=None, max_length=200)


class ShareSaveIn(CamelModel):
    # 文件夹分享里要转存的子文件；单文件分享留空
    node_id: int | None = None
    # 合集分享里勾选的多项；与 node_id 二选一
    node_ids: list[int] | None = Field(default=None, max_length=100)
    # 转存目标文件夹，缺省存到自己根目录
    parent_id: int | None = None


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
    # 找回密码兜底联系方式（留空则页面不展示）
    contact_email: str | None = Field(default=None, max_length=254)
    contact_qq: str | None = Field(default=None, max_length=32)
    # 自动检查 GitHub 新版本（只提示，不执行更新）
    auto_update_check: bool | None = None
    # 发现新版本后自动拉取并重启
    auto_update: bool | None = None


class AdminLogPurgeIn(CamelModel):
    days: int = Field(ge=1, le=3650)


class FeedbackIn(CamelModel):
    kind: Literal["suggestion", "bug", "other"] = "suggestion"
    title: str = Field(default="", max_length=120)
    content: str = Field(min_length=1, max_length=2000)
    contact: str = Field(default="", max_length=200)


class FeedbackStatusIn(CamelModel):
    status: Literal[0, 1]


class UpdateApplyIn(CamelModel):
    restart: bool = True


class AdminGcIn(CamelModel):
    apply: bool = False
