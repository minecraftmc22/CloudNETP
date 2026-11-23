"""ORM 模型。字段全部使用标准 SQL 类型，MySQL / SQLite 双兼容。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
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
    # 绑定邮箱：可用于登录、找回密码；未验证时只做「联系方式」用
    email: Mapped[str | None] = mapped_column(String(254), nullable=True, index=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 展示资料（与登录名解耦）：昵称可改可重复，头像只存文件名
    display_name: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bio: Mapped[str | None] = mapped_column(String(200), nullable=True)
    avatar: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # WebDAV Digest 用的 HA1 = MD5(lower(username):realm:password)
    # 只在注册 / 改密 / 登录时写入，老账号为 NULL 时需先登录一次补齐
    dav_ha1: Mapped[str | None] = mapped_column(String(32), nullable=True)
    role: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # 0 普通 1 管理员
    # 超级管理员：第一个注册的账号自动获得，可在数据库里给多个账号打开。
    # 只有超管能调整成员角色；超管一定同时是管理员（role=1）。
    is_super: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quota_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=10 * 1024**3)
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    banned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 封禁详情：原因 / 何时封的 / 到期时间（NULL = 永久）/ 谁封的
    ban_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    banned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ban_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    banned_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 两步验证：开启后密码登录还要再过一次邮箱验证码或通行密钥。
    # 放 users 而不是 user_settings —— 登录阶段只加载 User，settings 行是懒创建的
    two_factor_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (CheckConstraint("role IN (0, 1)", name="ck_users_role"),)


class Passkey(Base):
    """WebAuthn 通行密钥。

    `credential_id` 存 base64url 文本并全局唯一（登录断言时按它反查用户）；
    `public_key` 存 COSE 原始字节。只做「第二因子」，不参与无密码登录。
    """

    __tablename__ = "passkeys"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    credential_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sign_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # "usb,nfc,internal" 这类提示，浏览器用来优先匹配认证器
    transports: Mapped[str | None] = mapped_column(String(64), nullable=True)
    aaguid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 用户自己起的备注（"我的笔记本"），留空时前端显示「通行密钥」
    label: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    backed_up: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LoginSession(Base):
    """登录设备（会话）：一行 = 一台设备上的一次登录。

    token 里带 `s`（sid）声明，``get_current_user`` 每次请求按 sid 查这一行：
    `revoked_at` 非空即已下线。单个设备的下线走这里，不再需要全局 bump
    `token_version`（那个仍然保留：改密码 / 被管理员踢时一步作废所有会话）。
    旧 token 没有 `s` 声明（升级前签发），继续按原逻辑放行直到自然过期。
    """

    __tablename__ = "login_sessions"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 随机会话标识：签名保证不可伪造，库里这一行保证可撤销
    sid: Mapped[str] = mapped_column(String(24), unique=True, nullable=False)
    # 解析 UA 得到的「Windows · Chrome」；原始 UA 截断留存备查
    device: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    ua: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    ip: Mapped[str] = mapped_column(String(45), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserSetting(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    theme_mode: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=2)  # 0 亮 1 暗 2 跟随
    accent_color: Mapped[str] = mapped_column(String(16), nullable=False, default="#c084fc")
    # 主题色样式（JSON：mode=solid|gradient，colors=[#rrggbb...]）；空对象 = 单色
    accent_style: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    wallpaper: Mapped[str] = mapped_column(String(64), nullable=False, default="sakura")
    # 自定义背景图只存文件名；wallpaper=="custom" 时才有意义
    background_image: Mapped[str | None] = mapped_column(String(64), nullable=True)
    show_mascot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    show_petals: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    trash_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 自己的 WebDAV 是否允许挂载（管理员另有总开关；关掉后本人 /dav 返回 403）
    webdav_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 界面语言（zh-CN / en-US）；空串 = 跟浏览器，前端自己兜底
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    view_mode: Mapped[str] = mapped_column(String(8), nullable=False, default="grid")  # grid / list
    nav_collapsed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 分享页是否显示分享者完整昵称（关闭只显示前两位）
    share_show_name: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 歌词播放器样式（JSON：fontSize/hotColor/showTrans），空对象=全部默认
    lyrics_style: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # 各模块背景不透明度（JSON：nav/titlebar/panel/dialog，0-100，缺省=主题默认）
    surface_opacity: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # 看板娘 AI 对话配置（JSON：provider/baseUrl/model/tools/vision/thinking/protocol/
    # maxInput/maxOutput，以及加密后的 key）；空对象 = 还没配置
    ai_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # 看板娘长期记忆（JSON 数组：[{text, source: auto|custom, createdAt}]）；空数组 = 没有
    ai_memories: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 同意《快传使用规范》的时间；NULL = 还没同意（创建快传前必须同意一次）
    quick_terms_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 允许别人用「快传 → 发给用户」把文件发过来（关掉后发送方会收到 403）
    quick_receive_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 收到就自动同意、直接存进「我的文件」，不用人工点同意
    quick_auto_accept: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 收到的文件存到哪个目录；NULL = 默认的 我的文件/快传的文件
    quick_inbox_folder: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class AudioPref(Base):
    """每首歌（音频节点）的歌词来源偏好。

    mode: auto=按优先级（音频自带→在线搜索→无） / song=指定在线歌曲 /
          custom=自定义歌词文本 / none=不显示歌词。
    """

    __tablename__ = "audio_prefs"

    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="auto")
    # 歌词来源（netease / lyriclib）：song_id 只在对应来源里有效
    provider: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    song_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    song_title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    song_artist: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    custom_lyrics: Mapped[str | None] = mapped_column(Text, nullable=True)


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
    # 快传专用文件：不占用户配额、不出现在「我的文件」/搜索/WebDAV 里，
    # 只由对应的取件码引用；过期后由清理任务连 blob 一起回收
    quick_owned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 站内快传的待接收副本：指向 quick_transfers.id。
    # 非空 = 还躺在暂存区，接收方只能在「快传」里预览；同意后清空并搬进「我的文件」
    pending_send_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

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
    # 快传上传：完成时建的节点标 quick_owned，不占配额
    quick_owned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
    # 单节点分享就是这一项；合集分享存第一项（保持旧逻辑可用），全集见 node_ids
    node_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 合集分享的根节点 id 数组（JSON）；NULL 表示单节点分享
    node_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    token: Mapped[str] = mapped_column(String(43), unique=True, nullable=False, index=True)
    extract_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    max_downloads: Mapped[int | None] = mapped_column(Integer, nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("idx_share_user", "user_id", "created_at"),)


class ShareReport(Base):
    """分享举报：审核对象是分享链接，同时记录文件哈希与首次被举报时的文件名。"""

    __tablename__ = "share_reports"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    share_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # 来自快传取件码的举报：记 quick_transfers.id，此时 share_id 为 0（没有分享记录）
    quick_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    token: Mapped[str] = mapped_column(String(43), nullable=False)
    # 单文件分享记录文件哈希；文件夹分享为 NULL（审核通过只撤销该分享）
    blob_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    first_name: Mapped[str] = mapped_column(String(255), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    is_folder: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    reporter_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reporter_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="pending", index=True)
    handled_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("idx_report_status", "status", "created_at"),)


class QuickPickupItem(Base):
    """取件码里的单个文件：各自有有效期与下载次数。

    历史上取件码是一组文件共用一个有效期/次数（quick_transfers.expires_at / max_uses），
    为了兼容，没有对应 item 行的老记录仍按那两列读。
    """

    __tablename__ = "quick_pickup_items"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    quick_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    node_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("idx_qpi_quick", "quick_id"),)


class BlockedBlob(Base):
    """被拉黑的文件哈希：禁止分享（创建/公开访问/转存），不删除文件本体。"""

    __tablename__ = "blocked_blobs"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    blocked_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class QuickTransfer(Base):
    """快传：一张表装三种形态，靠 kind 区分。

    - drop   扫码直传：token 是手机端的凭据，target_id 是上传落地目录，uses 记已收文件数
    - send   站内快传：owner_id 发送者、peer_id 接收者，node_ids 是接收方那边的待接收副本
    - pickup 免登录取件码：code 是 6 位口令，node_ids 是要交付的文件，uses 记下载次数
    """

    __tablename__ = "quick_transfers"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # send 的接收者 / drop 无 / pickup 无
    peer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    # drop 的目标目录
    target_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    token: Mapped[str] = mapped_column(String(43), unique=True, nullable=False, index=True)
    # pickup 的取件码（明文：它就是访问凭据本身，等同短口令，靠限流防枚举）
    code: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    # JSON 数组：pickup 的交付文件 / send 待接收或已存入的顶层节点
    node_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # send：接收方是否已读
    seen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # send 的接收状态：pending 待接收 / accepted 已存入 / rejected 已拒绝 / expired 已过期
    send_status: Mapped[str] = mapped_column(String(10), nullable=False, default="pending")
    # 接收方做出决定的时间（同意 / 拒绝）
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 发送方是否已经看到接收结果（被拒绝时前端提示一次）；默认 True = 无需提示
    result_seen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    uses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("kind IN ('drop', 'send', 'pickup')", name="ck_quick_kind"),
        Index("idx_quick_owner", "owner_id", "kind", "created_at"),
        Index("idx_quick_peer", "peer_id", "kind", "created_at"),
    )


class AiChatSession(Base):
    """一段被保存下来的看板娘对话（历史记录）。"""

    __tablename__ = "ai_chat_sessions"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # 标题 = 第一句提问的截断；换标题只在这张表改，消息本体不动
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("idx_ais_user", "user_id", "updated_at"),)


class AiChatMessage(Base):
    """历史对话里的一条消息。images 是前端压好尺寸的 data URL 的 JSON 数组。

    MySQL 的 TEXT 只有 64KB，一张 1024px 的 jpeg data URL 就能超，
    所以正文与图片列在 MySQL 上用 MEDIUMTEXT（SQLite 的 TEXT 本来就不限长）。
    """

    __tablename__ = "ai_chat_messages"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("ai_chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(9), nullable=False)
    content: Mapped[str] = mapped_column(
        Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False, default=""
    )
    # JSON 数组文本（["data:image/jpeg;base64,..."]）；空串 = 没带图
    images: Mapped[str | None] = mapped_column(
        Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="ck_aim_role"),
        Index("idx_aim_session", "session_id", "id"),
    )


class Feedback(Base):
    """用户意见反馈：建议 / 问题 / 其他，管理员在后台标记处理。"""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="suggestion")
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    contact: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # 0 待处理，1 已处理
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, index=True)
    handled_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("idx_feedback_status", "status", "created_at"),)


class BanAppeal(Base):
    """封禁申诉：被ban的用户提交，管理员在后台通过/驳回。"""

    __tablename__ = "ban_appeals"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    # pending / approved / rejected
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    # 申诉当时的封禁快照：解封后原因会被清空，这里留个底
    ban_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ban_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reply: Mapped[str | None] = mapped_column(String(200), nullable=True)
    handled_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("idx_appeal_user", "user_id", "created_at"),)


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


class Friendship(Base):
    """好友关系：一行代表一对用户，requester 发起、addressee 接收。

    status=pending 时是「待处理的好友申请」；accepted 就是好友。
    拒绝直接删行（不保留黑历史），这样同一对用户以后还能再加。
    """

    __tablename__ = "friendships"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    requester_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    addressee_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # pending / accepted
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    # 申请附言（可选，最多 120 字）
    message: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("requester_id", "addressee_id", name="uq_friend_pair"),
        Index("idx_friend_addressee", "addressee_id", "status"),
    )


class ChatConversation(Base):
    """会话：单聊与群聊同表，kind 区分。

    - direct：两人一对一。direct_key 存「小 id-大 id」，唯一索引保证同一对只建一次
    - group：群聊。title 是群名，owner_id 是群主（可以邀请/移除成员）

    单聊的标题、头像都不落库：取对方的昵称与头像，改了名这里自动跟着变。
    """

    __tablename__ = "chat_conversations"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False, default="direct")
    title: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 单聊去重键："12-15"（小的 id 在前）；群聊为 NULL（唯一索引允许多个 NULL）
    direct_key: Mapped[str | None] = mapped_column(String(40), nullable=True, unique=True)
    # 列表页最后一条消息的冗余，省一次 N+1 查询；消息被撤回时只改 body，不改指针
    last_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("kind IN ('direct', 'group')", name="ck_conv_kind"),
        Index("idx_conv_updated", "updated_at"),
    )


class ChatMember(Base):
    """会话成员。last_read_id 记到「哪条消息已读」，未读数按它之后的消息数算。"""

    __tablename__ = "chat_members"

    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(8), nullable=False, default="member")
    last_read_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    joined_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    # 「清空聊天记录」的边界：这个 id（含）之前的历史对我不可见，对方的聊天不受影响。
    # 用 id 而不是时间戳，避免同一毫秒内的两条消息被误伤。
    cleared_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # 「删除聊天」：只对我隐藏会话，别人照旧。对方再发新消息时自动恢复显示。
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 退群用软标记？不：直接删行。历史消息仍在，sender_id 由消息表自己记着
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'member')", name="ck_member_role"),
        Index("idx_member_user", "user_id"),
    )


class ChatMessage(Base):
    """一条聊天消息。

    kind：
    - text    正文在 body
    - image   图片消息，payload 存 {name,width,height,size}，走 /api/chat/media
    - sticker 表情包，payload 存 {name,stickerId}
    - file    网盘文件/文件夹，payload 存 {nodes:[{id,name,size,mime,icon}],count,size}
    - system  系统提示（建群、入群、退群），正文在 body

    撤回不删行：置 recalled_at 并清空 body/payload，前端显示「已撤回」。
    举报要留证据，所以举报时把当时的内容快照抄进 chat_reports.snapshot。
    """

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False
    )
    sender_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False, default="text")
    body: Mapped[str] = mapped_column(
        Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False, default=""
    )
    # JSON 文本；结构见类注释
    payload: Mapped[str | None] = mapped_column(
        Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=True
    )
    # 转发来源（原消息 id），只做溯源展示
    forwarded_from: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    recalled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('text', 'image', 'file', 'sticker', 'system')", name="ck_msg_kind"
        ),
        Index("idx_msg_conv", "conversation_id", "id"),
        Index("idx_msg_sender", "sender_id", "created_at"),
    )


class Sticker(Base):
    """个人表情包：每人最多 N 张（N 由后台设置，默认 100）。

    文件单独存在 storage/chat/ 下（不进 blob 表、不占配额）：表情包是几百 KB 的小图，
    走 blob 的内容寻址没有收益，反而让「删表情」要小心引用计数。
    """

    __tablename__ = "stickers"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    file: Mapped[str] = mapped_column(String(80), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 归一化后字节的 sha256：判断「这张表情是不是已经在我的表情里了」。
    # 老库补列后为 NULL，用到时按文件内容回填一次。
    hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class ChatBlock(Base):
    """拉黑：blocker 不再接收 blocked 的消息，也不接受对方的好友申请。

    单独一张表而不是往 ``friendships`` 加字段：拉黑时好友关系会被删掉，
    状态没地方存；而且「拉黑一个陌生人」在语义上也不需要先有好友关系。
    """

    __tablename__ = "chat_blocks"

    blocker_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    blocked_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("idx_block_blocked", "blocked_id"),)


class ChatReport(Base):
    """聊天消息举报：审核对象是一条消息，snapshot 保留举报时看到的原文。"""

    __tablename__ = "chat_reports"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    reporter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 被举报的发送者
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # 举报那一刻的消息内容（kind + 正文 + 文件名摘要），撤回也洗不掉
    snapshot: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # pending / approved / rejected
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="pending", index=True)
    handled_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_creport_status"),
        Index("idx_creport_status", "status", "created_at"),
    )


class Notice(Base):
    """消息中心：一条通知只属于一个用户（管理员类通知在事件发生时按人展开）。

    设计取舍：不做「公告 + 已读回执」两表，直接按收件人落行。管理类事件量很小
    （举报/申诉/反馈），展开写反而让「已读」语义简单、查询不用 join。
    """

    __tablename__ = "notices"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # report / appeal / feedback / friend_request / friend_accept / system
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="system")
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # 前端路由，例如 #/admin/reports、#/friends
    link: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("idx_notice_user", "user_id", "id"),)
