# -*- coding: utf-8 -*-
"""后台成员管理权限矩阵。

策略（用户确认过）：
  · 配额这类「设置」——任何管理员都能改，包括超管和其他管理员的配额；
  · 密码 / 封禁 / 删除 / 角色 / 超管标记——只有超管能动，普通管理员碰超管或同级管理员一律拦住，
    否则两个管理员可以互相改密、互相封号，超管身份就形同虚设。

这里把矩阵钉住，免得以后改权限时悄悄放宽：
    管理员改超管配额        → 200
    管理员改同级管理员配额  → 200
    管理员改超管密码/封禁/角色/超管/删除 → 403 或 400
    超管改管理员配额/密码/封禁/角色      → 200

    ./.venv/Scripts/python.exe scripts/admin_perm_check.py
"""

from __future__ import annotations

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
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

PASS = FAIL = 0
GB = 1024**3


def check(label: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


RUNNER = r"""
import os, sys
sys.path.insert(0, os.environ["SMOKE_ROOT"])
from app import captcha
captcha.verify = lambda *a, **k: (True, "")
import app.main as m
import uvicorn
uvicorn.run(m.app, host="127.0.0.1", port=int(os.environ["SMOKE_PORT"]), log_level="warning")
"""


class Client:
    """带 cookie 的极简客户端：每个 Client 就是一个登录会话。"""

    def __init__(self, base: str):
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(), urllib.request.ProxyHandler({})
        )

    def call(self, method: str, path: str, data: dict | None = None) -> tuple[int, object]:
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + path, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Origin", self.base)
        try:
            with self.opener.open(req, timeout=20) as res:
                raw = res.read().decode()
                return res.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as err:
            raw = err.read().decode()
            try:
                return err.code, json.loads(raw)
            except Exception:
                return err.code, raw


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
    work = Path(tempfile.mkdtemp(prefix="cloudnetp-perm-"))
    db_path = work / "test.db"
    shutil.copy(ROOT / "dev.db", db_path)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    runner = work / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")

    env = {
        **os.environ,
        "SMOKE_ROOT": str(ROOT),
        "SMOKE_PORT": str(port),
        "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "STORAGE_DIR": str(work / "storage"),
        "SECRET_KEY": "smoke-secret-key-for-perm",
        "DB_FALLBACK_ENABLED": "false",
        "MAIL_ENABLED": "false",
    }
    log_file = open(work / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(VENV_PY), str(runner)], cwd=str(ROOT), env=env,
        stdout=log_file, stderr=subprocess.STDOUT,
    )

    try:
        if not wait_http(base, proc):
            print((work / "server.log").read_text(encoding="utf-8")[-3000:])
            return 1

        stamp = str(int(time.time()))[-6:]
        names = {
            "super": f"sp{stamp}",
            "admin": f"ad{stamp}",
            "other": f"oa{stamp}",
        }
        for name in names.values():
            st, body = Client(base).call("POST", "/api/auth/register", {
                "username": name, "password": "abc12345",
                "captchaId": "", "captcha": "0000", "agree": True,
            })
            check(f"注册 {name}", st == 201, str(st))

        con = sqlite3.connect(db_path)
        con.execute("UPDATE users SET role=1, is_super=1 WHERE username=?", (names["super"],))
        con.execute("UPDATE users SET role=1, is_super=0 WHERE username=?", (names["admin"],))
        con.execute("UPDATE users SET role=1, is_super=0 WHERE username=?", (names["other"],))
        con.commit()
        rows = con.execute(
            "SELECT username, id FROM users WHERE username IN (?,?,?)", tuple(names.values())
        ).fetchall()
        con.close()
        ids = {u: i for u, i in rows}
        if len(ids) != 3:
            print("建号失败", rows)
            return 1

        admin = Client(base)
        st, _ = admin.call("POST", "/api/auth/login", {
            "username": names["admin"], "password": "abc12345", "captchaId": "", "captcha": "0000"})
        check("普通管理员登录", st == 200, str(st))
        sup = Client(base)
        st, _ = sup.call("POST", "/api/auth/login", {
            "username": names["super"], "password": "abc12345", "captchaId": "", "captcha": "0000"})
        check("超管登录", st == 200, str(st))

        # ---------------- 普通管理员：能改的 ----------------
        for label, target in (("超管的配额", names["super"]), ("同级管理员的配额", names["other"])):
            st, body = admin.call("PATCH", f"/api/admin/users/{ids[target]}", {"quotaBytes": 7 * GB})
            got = (body or {}).get("user", {}).get("quotaBytes") if isinstance(body, dict) else None
            check(f"管理员可以改{label}", st == 200 and got == 7 * GB, f"{st} / {got}")

        # ---------------- 普通管理员：碰不得的 ----------------
        blocked = [
            ("重置超管密码", "POST", f"/api/admin/users/{ids[names['super']]}/password", None, (403,)),
            ("封禁超管", "PATCH", f"/api/admin/users/{ids[names['super']]}", {"banned": True}, (400, 403)),
            ("改超管角色", "PATCH", f"/api/admin/users/{ids[names['super']]}", {"role": 0}, (400, 403)),
            ("取消超管标记", "PATCH", f"/api/admin/users/{ids[names['super']]}", {"isSuper": False}, (400, 403)),
            ("删除超管", "DELETE", f"/api/admin/users/{ids[names['super']]}", None, (400, 403)),
            ("重置同级管理员密码", "POST", f"/api/admin/users/{ids[names['other']]}/password", None, (403,)),
            ("封禁同级管理员", "PATCH", f"/api/admin/users/{ids[names['other']]}", {"banned": True}, (403,)),
            ("改同级管理员角色", "PATCH", f"/api/admin/users/{ids[names['other']]}", {"role": 0}, (403,)),
            ("删除同级管理员", "DELETE", f"/api/admin/users/{ids[names['other']]}", None, (403,)),
        ]
        for label, method, path, data, codes in blocked:
            st, body = admin.call(method, path, data)
            detail = (body or {}).get("detail") if isinstance(body, dict) else body
            check(f"管理员被拦：{label}", st in codes, f"{st} {detail}")

        # ---------------- 超管：不受限 ----------------
        st, body = sup.call("PATCH", f"/api/admin/users/{ids[names['other']]}", {"quotaBytes": 9 * GB})
        check("超管可以改管理员配额", st == 200, str(st))
        st, _ = sup.call("POST", f"/api/admin/users/{ids[names['other']]}/password")
        check("超管可以重置管理员密码", st == 200, str(st))
        st, body = sup.call("PATCH", f"/api/admin/users/{ids[names['other']]}", {
            "banned": True, "banReason": "权限回归", "banDays": 1})
        check("超管可以封禁管理员", st == 200, str(st))
        st, body = sup.call("PATCH", f"/api/admin/users/{ids[names['other']]}", {"banned": False})
        check("超管可以解封管理员", st == 200, str(st))

        # 管理员自己仍然碰不到自己的角色，删不掉自己
        st, _ = admin.call("PATCH", f"/api/admin/users/{ids[names['admin']]}", {"role": 0})
        check("管理员改不了自己的角色", st in (400, 403), str(st))
        st, _ = admin.call("DELETE", f"/api/admin/users/{ids[names['admin']]}")
        check("管理员删不掉自己", st == 400, str(st))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n合计 {PASS + FAIL} 项：通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
