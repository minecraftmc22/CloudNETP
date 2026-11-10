# -*- coding: utf-8 -*-
"""WebDAV 总开关 / 用户开关的离线联调脚本。

用库副本起独立实例（不动真实库），把这几件事跑一遍：
  1. 默认能挂载（PROPFIND 带 Basic 返回 207）
  2. 用户关掉自己的 WebDAV → 403；/api/me 里 webdavEnabled=false；重新开启恢复
  3. 管理员关掉总开关 → 所有用户 404（连认证都不做）；后台返回 webdavEnabled=false
  4. 后台重新开启 → 恢复 207

    ./.venv/Scripts/python.exe scripts/webdav_toggle_smoke.py
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
PASSWORD = "Test123456"

passed = 0
failed = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {label} {extra}")
    else:
        failed += 1
        print(f"  FAIL {label} {extra}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def snapshot_db(dst: Path) -> None:
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


def call(base: str, method: str, path: str, cookie: str | None = None, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", f"cloudnetp_session={cookie}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw}


def propfind(base: str, username: str, password: str) -> tuple[int, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = urllib.request.Request(f"{base}/dav/", method="PROPFIND")
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Depth", "1")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(400).decode("utf-8", "replace")


def wait_up(base: str, proc: subprocess.Popen, log_path: Path) -> bool:
    for _ in range(80):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"{base}/api/version", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    print(log_path.read_text(encoding="utf-8", errors="replace")[-1500:])
    return False


def main() -> int:
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-dav-")).resolve()
    db_copy = tmp / "smoke.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    admin = con.execute("select id, username, token_version from users where role = 1 order by id limit 1").fetchone()
    plain = con.execute("select id, username, token_version from users where role != 1 order by id limit 1").fetchone()
    if not plain:
        con.execute(
            "insert into users (username, password_hash, display_name, quota_bytes, used_bytes, role,"
            " token_version, created_at) values (?,?,?,?,?,?,?,datetime('now'))",
            ("davsmoke", "x", "DavSmoke", 10 * 1024 ** 3, 0, 0, 0),
        )
        con.commit()
        plain = con.execute("select id, username, token_version from users where username = 'davsmoke'").fetchone()
    if not admin:
        raise SystemExit("库里没有管理员，先手动建一个再来跑这个脚本")
    hashed = hash_password(PASSWORD)
    for row in (admin, plain):
        con.execute("update users set password_hash=? where id=?", (hashed, row[0]))
    con.commit()
    con.close()

    admin_cookie = create_token(admin[0], admin[2])
    plain_cookie = create_token(plain[0], plain[2])
    admin_name, plain_name = admin[1], plain[1]

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db_copy.as_posix()}",
            "STORAGE_DIR": str(tmp / "storage"),
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "RELOAD": "false",
            "DB_PROBE_INTERVAL": "0",
            "DB_MIRROR_INTERVAL": "0",
            "DB_FALLBACK_ENABLED": "0",
            "SECRET_KEY": __import__("app.config", fromlist=["settings"]).settings.SECRET_KEY,
            "WEBDAV_ENABLED": "true",
        }
    )
    log_path = tmp / "server.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "main.py"], cwd=str(ROOT), env=env,
        stdout=log_file, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来，看日志：" + str(log_path))

        print("\n[A] 默认可以挂载")
        status, body = propfind(base, plain_name, PASSWORD)
        check("PROPFIND 207", status == 207, str(status))
        check("返回 multistatus", "multistatus" in body, body[:80])
        status, data = call(base, "GET", "/api/me", plain_cookie)
        settings = (data.get("user") or {}).get("settings") or {}
        check("/api/me 带 webdavEnabled", settings.get("webdavEnabled") is True, str(settings.get("webdavEnabled")))

        print("\n[B] 用户关掉自己的 WebDAV")
        status, _ = call(base, "PATCH", "/api/me/settings", plain_cookie, {"webdavEnabled": False})
        check("保存成功", status == 200, str(status))
        status, data = call(base, "GET", "/api/me", plain_cookie)
        check(
            "/api/me 变成 false",
            ((data.get("user") or {}).get("settings") or {}).get("webdavEnabled") is False,
        )
        status, body = propfind(base, plain_name, PASSWORD)
        check("PROPFIND 403", status == 403, str(status))
        check("提示去设置里开启", "设置" in body, body[:120])
        status, _ = propfind(base, admin_name, PASSWORD)
        check("别的用户不受影响（207）", status == 207, str(status))

        print("\n[C] 用户重新开启")
        call(base, "PATCH", "/api/me/settings", plain_cookie, {"webdavEnabled": True})
        status, _ = propfind(base, plain_name, PASSWORD)
        check("恢复 207", status == 207, str(status))

        print("\n[D] 管理员关掉总开关")
        status, data = call(base, "GET", "/api/admin/settings", admin_cookie)
        check("后台设置里有 webdavEnabled", (data.get("settings") or {}).get("webdavEnabled") is True)
        status, data = call(base, "PATCH", "/api/admin/settings", admin_cookie, {"webdavEnabled": False})
        check("保存成功", status == 200, str(status))
        check("后台读数变 false", (data.get("settings") or {}).get("webdavEnabled") is False)
        status, body = propfind(base, plain_name, PASSWORD)
        check("用户 PROPFIND 404", status == 404, str(status))
        status, body = propfind(base, admin_name, PASSWORD)
        check("管理员也是 404", status == 404, str(status))

        print("\n[E] 管理员重新开启")
        call(base, "PATCH", "/api/admin/settings", admin_cookie, {"webdavEnabled": True})
        status, _ = propfind(base, plain_name, PASSWORD)
        check("恢复 207", status == 207, str(status))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failed:
        print(f"失败 {failed} 项，通过 {passed} 项")
        return 1
    print(f"全部通过（{passed} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
