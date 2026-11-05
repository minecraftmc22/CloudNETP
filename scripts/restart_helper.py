# -*- coding: utf-8 -*-
"""自动更新后的重启助手。

用法：python scripts/restart_helper.py <旧进程 PID>
等旧进程释放端口后，用同一个虚拟环境解释器重新启动 run.py。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402


def port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", settings.PORT), timeout=0.4):
            return True
    except OSError:
        return False


def main() -> int:
    parent = sys.argv[1] if len(sys.argv) > 1 else "?"
    time.sleep(1.5)
    for _ in range(120):  # 最多等 60 秒
        if not port_open():
            break
        time.sleep(0.5)

    log_path = ROOT / "storage" / "update-restart.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "a", encoding="utf-8", buffering=1)
    log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 重启服务（旧 PID {parent}）\n")

    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [sys.executable, str(ROOT / "run.py")],
            cwd=str(ROOT),
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except OSError as exc:
        log.write(f"启动失败：{exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
