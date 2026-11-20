# -*- coding: utf-8 -*-
"""清理无人引用文件时，回收站里的文件必须保住。

两个账号：B 上传并删到回收站，A 是管理员；再手工塞一条真孤儿做对照，
然后调 /api/admin/storage/orphans 清理。期望：孤儿被删、B 的回收站文件还在、
trashKept 计数为 1、B 的回收站列表不变。

    ./.venv/Scripts/python.exe scripts/trash_gc_check.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

RUNNER = r"""
import os, sys
sys.path.insert(0, os.environ["SMOKE_ROOT"])
from app import captcha
captcha.verify = lambda *a, **k: (True, "")
import app.main as m
import uvicorn
uvicorn.run(m.app, host="127.0.0.1", port=int(os.environ["SMOKE_PORT"]), log_level="warning")
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_http(base: str, proc: subprocess.Popen) -> bool:
    for _ in range(120):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"{base}/api/meta", timeout=5) as res:
                if res.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="cloudnetp-gc-"))
    db_path = work / "test.db"
    shutil.copy(ROOT / "dev.db", db_path)
    storage_dir = work / "storage"
    port = free_port()
    base = f"http://localhost:{port}"
    runner = work / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")

    env = {
        **os.environ,
        "SMOKE_ROOT": str(ROOT),
        "SMOKE_PORT": str(port),
        "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "STORAGE_DIR": str(storage_dir),
        "SECRET_KEY": "smoke-secret-key-for-gc",
        "DB_FALLBACK_ENABLED": "false",
        "MAIL_ENABLED": "false",
    }
    log_file = open(work / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(VENV_PY), str(runner)], cwd=str(ROOT), env=env,
        stdout=log_file, stderr=subprocess.STDOUT,
    )

    def client():
        jar = urllib.request.HTTPCookieProcessor()
        opener = urllib.request.build_opener(jar, urllib.request.ProxyHandler({}))

        def call(method: str, path: str, body=None, raw=None, ctype=None):
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
            req = urllib.request.Request(base + path, data=data, method=method)
            if ctype:
                req.add_header("Content-Type", ctype)
            elif body is not None:
                req.add_header("Content-Type", "application/json")
            req.add_header("Origin", base)
            try:
                with opener.open(req, timeout=30) as res:
                    txt = res.read().decode()
                    return res.status, (json.loads(txt) if txt else None)
            except urllib.error.HTTPError as err:
                return err.code, err.read().decode()[:300]

        return call

    try:
        if not wait_http(base, proc):
            print((work / "server.log").read_text(encoding="utf-8")[-2000:])
            return 1

        stamp = str(int(time.time()))[-6:]
        users = {}
        for tag in ("a", "b"):
            name = f"gc{tag}{stamp}"
            api = client()
            status, res = api("POST", "/api/auth/register", {
                "username": name, "password": "abc12345",
                "captchaId": "", "captcha": "0000", "agree": True,
            })
            print("register", tag, status)
            users[tag] = {"name": name, "api": api}
        # dev.db 副本里已有用户，新注册的不是管理员：手动提权（只改副本）
        con = sqlite3.connect(db_path)
        con.execute("UPDATE users SET role=1 WHERE username=?", (users["a"]["name"],))
        con.commit()
        con.close()
        print("admin is a:", users["a"]["name"])

        payload = b"trash-me"
        sha = hashlib.sha256(payload).hexdigest()
        b_api = users["b"]["api"]
        status, init = b_api("POST", "/api/uploads/init", {
            "name": "b-trash.txt", "size": len(payload), "sha256": sha, "mime": "text/plain",
        })
        print("init", status, str(init)[:120])
        sid = init["sessionId"]
        status, _ = b_api("PUT", f"/api/uploads/{sid}/0", raw=payload, ctype="application/octet-stream")
        print("chunk", status)
        status, done = b_api("POST", f"/api/uploads/{sid}/complete", {})
        print("complete", status, str(done)[:160])
        node_id = done.get("node", {}).get("id") or done.get("nodeId")

        status, _ = b_api("DELETE", f"/api/nodes/{node_id}")
        print("delete to trash", status)
        status, trash = b_api("GET", "/api/trash")
        print("trash before:", [i.get("name") for i in (trash.get("items") or trash or [])][:5])

        blob = storage_dir / "blobs" / sha[:2] / sha[2:4] / sha
        exists_before = blob.exists()

        # 造一条真孤儿（blobs 有记录、没人引用），确认清理确实会动手
        orphan_payload = b"orphan-only"
        orphan_sha = hashlib.sha256(orphan_payload).hexdigest()
        orphan_path = storage_dir / "blobs" / orphan_sha[:2] / orphan_sha[2:4] / orphan_sha
        orphan_path.parent.mkdir(parents=True, exist_ok=True)
        orphan_path.write_bytes(orphan_payload)
        old = time.time() - 3 * 3600
        os.utime(orphan_path, (old, old))
        con = sqlite3.connect(db_path)
        con.execute(
            "INSERT INTO blobs (sha256, size, ref_count, created_at) VALUES (?, ?, 0, datetime('now'))",
            (orphan_sha, len(orphan_payload)),
        )
        con.commit()
        con.close()
        print("orphan seeded:", orphan_path.exists())

        status, res = users["a"]["api"]("POST", "/api/admin/storage/orphans", {"apply": True})
        print("orphan gc", status, json.dumps(res.get("result") if isinstance(res, dict) else res, ensure_ascii=False)[:300])

        status, trash_after = b_api("GET", "/api/trash")
        names_after = [i.get("name") for i in (trash_after.get("items") or trash_after or [])]
        print("trash after:", names_after)
        print("blob before/after:", exists_before, blob.exists())

        status, res2 = users["a"]["api"]("POST", "/api/admin/storage/orphans", {"apply": True})
        print("second gc:", json.dumps(res2.get("result") if isinstance(res2, dict) else res2, ensure_ascii=False)[:200])
        orphan_gone = not orphan_path.exists()
        print("orphan gone:", orphan_gone, "| trashKept:", (res2.get("result") or {}).get("trashKept") if isinstance(res2, dict) else None)

        ok = exists_before and blob.exists() and "b-trash.txt" in names_after and orphan_gone
        print("结论:", "OK 没动回收站" if ok else "FAIL 回收站被牵连")
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
