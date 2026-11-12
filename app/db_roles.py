"""主库 / 次库的角色选择（存本地文件，启动前就能读到）。

为什么不用 `system_settings`：角色决定「连哪个库」，而 `system_settings` 本身就在库里，
启动时还没连上，读不到。所以这份配置落在 `storage/db/roles.json`——
本地文件随时可读、在 Docker 里跟着 /data 卷一起持久化，改完立刻生效。

优先级：这个文件（管理后台选的） > `.env` 的 `DB_PRIMARY` / `DB_SECONDARY` > 内置默认
（默认值按 `DATABASE_URL` 与 `DB_FALLBACK_ENABLED` 推断，保持老版本行为不变）。

文件写坏/权限不足时一律当作「没有这份文件」，回落到 .env，不让站点因为一个 JSON 起不来。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("uvicorn.error").getChild("cloudnetp.db")

ROLE_PRIMARY = "primary"
ROLE_SECONDARY = "secondary"

# 主库只能是这两种；次库多一个「不设置」
PRIMARY_KINDS = ("mysql", "sqlite")
SECONDARY_KINDS = ("none", "mysql", "sqlite")

FILE_NAME = "roles.json"

# (mtime, size, 内容)：文件没动就不重复解析
_cache: tuple[tuple[float, int], dict[str, str]] | None = None


def path_for(storage_dir: Path) -> Path:
    return Path(storage_dir) / "db" / FILE_NAME


def normalize(primary: Any, secondary: Any) -> tuple[str, str]:
    """把任意输入收敛成一对合法角色；认不出来的值变成空串，交给上层退回默认。"""
    p = str(primary or "").strip().lower()
    s = str(secondary or "").strip().lower()
    if p not in PRIMARY_KINDS:
        p = ""
    if s not in SECONDARY_KINDS:
        s = ""
    return p, s


def read(storage_dir: Path) -> dict[str, str] | None:
    """读管理后台保存的角色。没有文件 / 解析失败返回 None。"""
    global _cache
    path = path_for(storage_dir)
    try:
        st = path.stat()
    except OSError:
        _cache = None
        return None

    stamp = (st.st_mtime, st.st_size)
    if _cache is not None and _cache[0] == stamp:
        return dict(_cache[1])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("读取数据库角色配置失败（按未配置处理）：%s", exc)
        _cache = None
        return None
    if not isinstance(raw, dict):
        _cache = None
        return None

    primary, secondary = normalize(raw.get("primary"), raw.get("secondary"))
    data = {"primary": primary, "secondary": secondary}
    _cache = (stamp, data)
    return dict(data)


def write(storage_dir: Path, primary: str, secondary: str) -> dict[str, str]:
    """保存角色并立即生效（下次读 settings 就会拿到新值）。

    先写临时文件再 `os.replace`，避免断电/写一半留下一份坏 JSON。
    """
    global _cache
    p, s = normalize(primary, secondary)
    data = {
        "primary": p or "sqlite",
        "secondary": s or "none",
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = path_for(storage_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".roles-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _cache = None  # 下次 read 重新落盘解析，保证读到的就是刚写的
    return {"primary": data["primary"], "secondary": data["secondary"]}


def clear(storage_dir: Path) -> None:
    """删掉管理后台的覆盖，回到 .env 的设定（目前只有测试与「恢复默认」用得到）。"""
    global _cache
    _cache = None
    try:
        path_for(storage_dir).unlink()
    except OSError:
        pass
