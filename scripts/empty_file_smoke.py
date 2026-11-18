# -*- coding: utf-8 -*-
"""0 字节文件冒烟：内容为空但有文件名的文件，四条上传入口都要能存进来。

覆盖：
  [A] 分片接口 POST /api/uploads/init（size=0 直接建节点，和秒传同一出口）
  [B] 同名空文件自动编号（空文件 (1).txt）
  [C] 单次 PUT /api/nodes/upload（0 字节 body）
  [D] 快传 quick=true（只进快传暂存区，不进「我的文件」）
  [E] WebDAV PUT（资源管理器里新建空文件走的就是这条）
  [F] 下载回来是 0 字节、列表里能看到、配额不因为空文件变化

自带实例与数据库副本，不碰真实库：python scripts/empty_file_smoke.py
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
TMP = Path(tempfile.mkdtemp(prefix="cn_empty_"))
DB_COPY = TMP / "probe.db"
PASSWORD = "Probe#12345"
EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {label} {extra}")
    else:
        fail += 1
        print(f"  FAIL {label} {extra}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def snapshot_db(dst: Path) -> None:
    """拷一份库来跑，测完连副本一起删。"""
    src_path = ROOT / "storage" / "db" / "fallback.db"
    if not src_path.is_file():
        src_path = ROOT / "dev.db"
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        src.close()
        out.close()


snapshot_db(DB_COPY)
PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
os.environ.update(
    {
        "DATABASE_URL": f"sqlite:///{DB_COPY.as_posix()}",
        "STORAGE_DIR": str(TMP / "storage"),
        "HOST": "127.0.0.1",
        "PORT": str(PORT),
        "RELOAD": "0",
        "DB_PROBE_INTERVAL": "0",
        "DB_MIRROR_INTERVAL": "0",
        "DB_FALLBACK_ENABLED": "0",
    }
)
(TMP / "storage").mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ROOT))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from app import db_failover  # noqa: E402
from app.models import User  # noqa: E402
from app.security import create_token, hash_password  # noqa: E402


def http(method, path, body=None, headers=None):
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Origin", BASE)
    req.add_header(
        "Content-Type", "application/octet-stream" if isinstance(body, bytes) else "application/json"
    )
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw) if raw else {}
            except Exception:
                return resp.status, raw[:200]
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw[:300]


def main():
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    admin = db.query(User).filter(User.role == 1).order_by(User.id).first()
    # 库是副本，改密码不影响真实数据（WebDAV 需要明文密码登录）
    admin.password_hash = hash_password(PASSWORD)
    db.commit()
    username, token = admin.username, create_token(admin.id, admin.token_version)
    used_before = admin.used_bytes
    db.close()

    auth = "Basic " + base64.b64encode(f"{username}:{PASSWORD}".encode()).decode()
    cookie = {"Cookie": f"cloudnetp_session={token}"}

    log_path = TMP / "server.log"
    log_file = log_path.open("wb")
    server = subprocess.Popen(
        [str(VENV_PY), "main.py"],
        cwd=str(ROOT),
        env=os.environ.copy(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(120):
            if server.poll() is not None:
                raise SystemExit("测试实例没起来：" + log_path.read_text(errors="replace")[-800:])
            try:
                with urllib.request.urlopen(f"{BASE}/api/meta", timeout=2) as resp:
                    if resp.status == 200:
                        break
            except Exception:
                pass
            time.sleep(0.5)

        print("== 分片接口 init：size = 0 ==")
        st, data = http(
            "POST",
            "/api/uploads/init",
            {
                "name": "空文件.txt",
                "size": 0,
                "parentId": None,
                "sha256": EMPTY_SHA,
                "mime": "text/plain",
            },
            cookie,
        )
        check("init 直接建好节点", st == 200 and data.get("instant") is True, f"{st} {str(data)[:90]}")
        node = (data or {}).get("node") or {}
        check("节点大小是 0", node.get("size") == 0, str(node.get("size")))
        check("没有留下上传会话", not data.get("sessionId"), str(data.get("sessionId")))

        print("== 再来一个同名 0 字节文件 ==")
        st, data = http(
            "POST",
            "/api/uploads/init",
            {"name": "空文件.txt", "size": 0, "parentId": None, "sha256": None, "mime": "text/plain"},
            cookie,
        )
        node2 = (data or {}).get("node") or {}
        check(
            "同名自动编号 (1)",
            st == 200 and node2.get("name") == "空文件 (1).txt",
            str(node2.get("name")),
        )

        print("== 单次 PUT /api/nodes/upload：0 字节 ==")
        st, data = http(
            "PUT", "/api/nodes/upload?name=single-empty.txt", b"", {**cookie, "Content-Length": "0"}
        )
        check(
            "单次上传成功",
            st == 201 and (data.get("node") or {}).get("size") == 0,
            f"{st} {str(data)[:90]}",
        )

        print("== 快传 quick=true：0 字节 ==")
        st, data = http(
            "POST",
            "/api/uploads/init",
            {
                "name": "quick-empty.txt",
                "size": 0,
                "parentId": None,
                "sha256": None,
                "mime": "text/plain",
                "quick": True,
            },
            cookie,
        )
        check("快传空文件成功", st == 200 and data.get("instant") is True, f"{st} {str(data)[:90]}")

        print("== WebDAV：0 字节 ==")
        st, _ = http("PUT", "/dav/dav-empty.txt", b"", {"Authorization": auth, "Content-Length": "0"})
        check("WebDAV 0 字节 201", st == 201, str(st))
        st, _ = http("PROPFIND", "/dav/dav-empty.txt", b"", {"Authorization": auth, "Depth": "0"})
        check("对空文件 PROPFIND 正常", st == 207, str(st))

        print("== 下载回来还是 0 字节 ==")
        req = urllib.request.Request(f"{BASE}/api/nodes/{node.get('id')}/download", method="GET")
        req.add_header("Cookie", f"cloudnetp_session={token}")
        req.add_header("Origin", BASE)
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            check("下载 200 且内容为空", resp.status == 200 and body == b"", f"{resp.status} {body!r}"[:60])

        print("== 详情 / 预览 / 文本预览 ==")
        st, detail = http("GET", f"/api/nodes/{node.get('id')}", None, cookie)
        check("节点详情能读", st == 200, str(st))
        st, _ = http("GET", f"/api/nodes/{node.get('id')}/preview", None, cookie)
        check("二进制预览 200（空流）", st == 200, str(st))
        st, text = http("GET", f"/api/nodes/{node.get('id')}/text", None, cookie)
        check(
            "文本预览拿到空内容",
            st == 200 and (text.get("text") in ("", None)),
            f"{st} {str(text)[:60]}",
        )

        print("== 我的文件列表 ==")
        st, listing = http("GET", "/api/nodes?sort=name&order=asc", None, cookie)
        names = {item["name"] for item in (listing.get("items") or [])}
        check("空文件在列表里", "空文件.txt" in names and "single-empty.txt" in names, str(sorted(names)[:8]))
        check("快传空文件不进我的文件", "quick-empty.txt" not in names)

        print("== 配额 ==")
        st, me = http("GET", "/api/me", None, cookie)
        used = me["user"]["usedBytes"]
        check("配额不变（空文件不占空间）", used == used_before, f"{used_before} -> {used}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()
        log_file.close()
        tail = log_path.read_text(encoding="utf-8", errors="replace")
        check("服务端没有 Traceback", "Traceback" not in tail)
        shutil.rmtree(TMP, ignore_errors=True)
        print(f"\n通过 {ok} / 失败 {fail}")


if __name__ == "__main__":
    main()
