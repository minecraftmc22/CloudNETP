"""首次启动的前置准备：生成 .env、补一个随机 SECRET_KEY。

`start.sh` / `start.ps1` 会自动调用；手动部署时也可以单跑：

    python scripts/bootstrap.py

只做三件幂等的事：
1. 没有 .env 就从 .env.example 拷一份；
2. SECRET_KEY 为空（或整行缺失）就生成 64 位十六进制随机串写回去；
3. 打印当前数据库类型，顺带提醒怎么换成 MySQL。

重复运行不会覆盖已有内容，可以放心每次启动都跑。
"""

from __future__ import annotations

import os
import secrets
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
EXAMPLE_FILE = ROOT / ".env.example"


def ensure_env() -> bool:
    """返回是否新建了 .env。"""
    if ENV_FILE.exists():
        return False
    if not EXAMPLE_FILE.is_file():
        print("[bootstrap] 找不到 .env.example，跳过 .env 创建")
        return False
    shutil.copyfile(EXAMPLE_FILE, ENV_FILE)
    print(f"[bootstrap] 已从 .env.example 生成 {ENV_FILE.name}")
    return True


def ensure_secret() -> None:
    """SECRET_KEY 为空时生成一个；整行缺失就补在文件末尾。"""
    if not ENV_FILE.is_file():
        return

    lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines()
    found = False
    changed = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("SECRET_KEY="):
            continue
        found = True
        value = stripped.partition("=")[2].strip().strip('"').strip("'")
        if not value:
            lines[index] = f"SECRET_KEY={secrets.token_hex(32)}"
            changed = True
            print("[bootstrap] SECRET_KEY 为空，已生成随机值")
        break

    if not found:
        lines.append(f"SECRET_KEY={secrets.token_hex(32)}")
        changed = True
        print("[bootstrap] .env 里没有 SECRET_KEY，已补一行随机值")

    if changed:
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def show_database() -> None:
    if not ENV_FILE.is_file():
        return
    # 命令行 / systemd 显式 export 的 DATABASE_URL 优先级更高，跟 config.py 的规则一致
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            stripped = line.strip()
            if stripped.startswith("DATABASE_URL="):
                url = stripped.partition("=")[2].strip().strip('"').strip("'")
                break
    if url.startswith("sqlite") or not url:
        print("[bootstrap] 当前用 SQLite 本地库；要接 MySQL 改 .env 的 DATABASE_URL 后重启")
    else:
        # 别把密码打到终端里，只显示协议与主机
        head = url.split("://", 1)[-1].split("@")[-1]
        print(f"[bootstrap] 当前数据库：{url.split('://', 1)[0]}://{head}")


def main() -> int:
    created = ensure_env()
    ensure_secret()
    if created:
        print("[bootstrap] 想改端口 / 域名 / 配额，直接编辑 .env")
    show_database()
    return 0


if __name__ == "__main__":
    sys.exit(main())
