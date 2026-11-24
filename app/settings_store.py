"""系统设置的键值存取（带默认值与类型转换）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .config import settings
from .models import SystemSetting

# 生成取件码后自动复制的分享文案。占位符：{site} 站点名、{file} 文件名（多文件是「N 个项目」）、
# {code} 取件码、{link} 取件页完整地址；管理员可以整段替换。
QUICK_COPY_TEMPLATE_DEFAULT = """我在"{site}"分享了 '{file}' 了喵~
{code} <-复制取件码到{site}或点击链接 {link} 来取件吧喵awa!"""

DEFAULTS: dict[str, str] = {
    "allow_register": "1" if settings.ALLOW_REGISTER else "0",
    "site_name": settings.APP_NAME,
    "site_announcement": "",
    "default_quota_bytes": str(settings.default_quota_bytes),
    "max_file_size_bytes": str(settings.max_file_size_bytes),
    "chunk_size_bytes": str(settings.chunk_size_bytes),
    "trash_retention_days": "30",
    # 管理员联系方式：找回密码走不通时，页面会引导用户直接联系这个邮箱/QQ
    "contact_email": "",
    "contact_qq": "",
    # WebDAV 总开关（.env 的 WEBDAV_ENABLED 只是初始值，管理员在后台改）
    "webdav_enabled": "1" if settings.WEBDAV_ENABLED else "0",
    # 管理员开关：登录后自动检查 GitHub main 有没有新版本（只提示，不自动更新）
    "auto_update_check": "0",
    # 管理员开关：后台每 6 小时检查一次，发现新版本自动拉取并重启
    "auto_update": "0",
    # 看板娘 AI 对话的系统提示词（管理员在 设置 → 看板娘 → AI 设置 里改）
    "ai_system_prompt": "",
    # 快传取件码的上限（0 = 不限制）：有效期最长多少天、单个文件最多被下载几次
    "quick_max_days": "30",
    "quick_max_uses": "0",
    # 过期 / 撤销后，物理文件再保留多少天；清理任务按这个时间回收
    "quick_retention_days": "7",
    # 取件码生成后把分享文案写进剪贴板（默认开）与那段文案本身
    "quick_auto_copy": "1",
    "quick_copy_template": QUICK_COPY_TEMPLATE_DEFAULT,
    # 存储空间自动清理（后台「存储」页）：每天几点跑、清哪些、上次跑的结果
    "auto_cleanup_enabled": "0",
    "auto_cleanup_hour": "4",
    "auto_cleanup_orphans": "1",
    "auto_cleanup_quick": "1",
    "auto_cleanup_last_run": "",
    "auto_cleanup_last_result": "",
    # SEO：标题 / 描述 / 关键词 / 分享图 / canonical 基址，以及是否允许搜索引擎收录
    # 默认不收录：个人网盘不该被搜索引擎意外抓进索引，管理员自己去后台打开
    "seo_index": "0",
    "seo_title": "",
    "seo_description": "",
    "seo_keywords": "",
    "seo_image": "",
    "seo_base_url": "",
    # 聊天：每人最多存多少张表情包、多久内能撤回（0 = 不限制）、群里最多多少人
    "sticker_max_per_user": "100",
    "chat_recall_minutes": "10",
    "chat_group_max_members": "200",
    # 单张表情大小上限（MB）：静态图与动图（GIF）分开设置
    "sticker_max_mb": "2",
    "sticker_gif_max_mb": "6",
    # 每个群的群文件配额（GB）：不占个人配额，只算进群的这一份
    "group_file_quota_gb": "10",
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
