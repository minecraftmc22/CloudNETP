"""审计动作字典：action → 中文标签 / 语气，供管理后台筛选与着色。

新增埋点时务必在这里登记，否则日志页只能显示原始 action 名。
"""

from __future__ import annotations

# tone: info 普通 / success 成功 / warn 需注意 / danger 破坏性
ACTIONS: dict[str, dict[str, str]] = {
    # ---- 账号 ----
    "register": {"label": "注册账号", "tone": "success", "group": "账号"},
    "login": {"label": "登录", "tone": "info", "group": "账号"},
    "login_failed": {"label": "登录失败", "tone": "warn", "group": "账号"},
    "logout": {"label": "退出登录", "tone": "info", "group": "账号"},
    "change_password": {"label": "修改密码", "tone": "warn", "group": "账号"},
    "profile_update": {"label": "修改资料", "tone": "info", "group": "账号"},
    "avatar_update": {"label": "更换头像", "tone": "info", "group": "账号"},
    "avatar_remove": {"label": "移除头像", "tone": "info", "group": "账号"},
    # ---- 文件 ----
    "folder_create": {"label": "新建文件夹", "tone": "info", "group": "文件"},
    "node_rename": {"label": "重命名", "tone": "info", "group": "文件"},
    "node_move": {"label": "移动", "tone": "info", "group": "文件"},
    "node_upload": {"label": "上传文件", "tone": "success", "group": "文件"},
    "upload_instant": {"label": "秒传", "tone": "success", "group": "文件"},
    "node_delete": {"label": "删除到回收站", "tone": "warn", "group": "文件"},
    "node_delete_permanent": {"label": "直接删除", "tone": "danger", "group": "文件"},
    "node_restore": {"label": "还原文件", "tone": "success", "group": "文件"},
    "node_purge": {"label": "彻底删除", "tone": "danger", "group": "文件"},
    "trash_empty": {"label": "清空回收站", "tone": "danger", "group": "文件"},
    "trash_enable": {"label": "开启回收站", "tone": "info", "group": "文件"},
    "trash_disable": {"label": "关闭回收站", "tone": "warn", "group": "文件"},
    # ---- 分享 ----
    "share_create": {"label": "创建分享", "tone": "success", "group": "分享"},
    "share_revoke": {"label": "取消分享", "tone": "warn", "group": "分享"},
    "share_download": {"label": "分享被下载", "tone": "info", "group": "分享"},
    "share_report": {"label": "分享被举报", "tone": "warn", "group": "分享"},
    "share_save": {"label": "转存到网盘", "tone": "success", "group": "分享"},
    # ---- WebDAV ----
    "dav_upload": {"label": "WebDAV 写入", "tone": "success", "group": "WebDAV"},
    "dav_delete": {"label": "WebDAV 删除", "tone": "warn", "group": "WebDAV"},
    "dav_delete_permanent": {"label": "WebDAV 彻底删除", "tone": "danger", "group": "WebDAV"},
    "dav_mkcol": {"label": "WebDAV 建目录", "tone": "info", "group": "WebDAV"},
    "dav_move": {"label": "WebDAV 移动", "tone": "info", "group": "WebDAV"},
    "dav_copy": {"label": "WebDAV 复制", "tone": "info", "group": "WebDAV"},
    # ---- 管理后台 ----
    "admin_user_quota": {"label": "调整配额", "tone": "warn", "group": "管理"},
    "admin_user_ban": {"label": "封禁用户", "tone": "danger", "group": "管理"},
    "admin_user_unban": {"label": "解封用户", "tone": "success", "group": "管理"},
    "admin_user_role": {"label": "变更角色", "tone": "warn", "group": "管理"},
    "admin_user_password": {"label": "重置密码", "tone": "warn", "group": "管理"},
    "admin_user_delete": {"label": "删除用户", "tone": "danger", "group": "管理"},
    "admin_settings_update": {"label": "修改系统设置", "tone": "warn", "group": "管理"},
    "admin_logs_purge": {"label": "清理审计日志", "tone": "danger", "group": "管理"},
    "admin_blob_gc": {"label": "清理孤儿文件", "tone": "warn", "group": "管理"},
    "admin_report_approve": {"label": "举报通过·拉黑文件", "tone": "danger", "group": "管理"},
    "admin_report_reject": {"label": "举报驳回", "tone": "info", "group": "管理"},
    "admin_blob_unblock": {"label": "解除文件拉黑", "tone": "warn", "group": "管理"},
    "admin_db_refresh": {"label": "刷新数据库配置", "tone": "warn", "group": "管理"},
    "admin_db_reconnect": {"label": "手动切换数据库", "tone": "danger", "group": "管理"},
}

UNKNOWN = {"label": "其它操作", "tone": "info", "group": "其它"}


def describe(action: str) -> dict[str, str]:
    meta = ACTIONS.get(action, UNKNOWN)
    return {"action": action, **meta}
