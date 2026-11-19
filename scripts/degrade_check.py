# -*- coding: utf-8 -*-
"""邮件服务未配置时的注册降级路径（临时实例 + urllib，不联网）。

管理后台没配 SMTP/BillionMail 时：注册不要求邮箱验证码、邮箱可选，
注册接口本身照常可用；一旦配好邮件，同一个注册页自动变严格。
这里把这些分支钉死，免得以后改动把自托管小站的注册堵死。

    ./.venv/Scripts/python.exe scripts/degrade_check.py
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import socket
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
sys.path.insert(0, os.environ["DEGRADE_ROOT"])
from app import captcha, mail_config
captcha.verify = lambda *a, **k: (True, "")
# 强制「邮件服务没配好」，不依赖本机 .env
mail_config.ready = lambda *a, **k: False
import app.main as m
import uvicorn
uvicorn.run(m.app, host="127.0.0.1", port=int(os.environ["DEGRADE_PORT"]), log_level="warning")
"""


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar)
        )

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with self.opener.open(req, timeout=20) as res:
                return res.status, json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                parsed = {"detail": raw.decode("utf-8", "replace")}
            return exc.code, parsed


def wait_ready(client: Client, proc: subprocess.Popen, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            status, _ = client.call("GET", "/api/meta")
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="cloudnetp-degrade-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    client = Client(base)
    log_path = work / "server.log"
    log_file = open(log_path, "w", encoding="utf-8")
    env = {
        **os.environ,
        "DEGRADE_ROOT": str(ROOT),
        "DEGRADE_PORT": str(port),
        "DATABASE_URL": f"sqlite:///{(work / 'test.db').as_posix()}",
        # 必须隔离存储目录，否则会读到项目的 storage/db/roles.json，
        # 主库连不上时兜底写进真实站点的 fallback.db
        "STORAGE_DIR": str(work / "storage"),
        "SECRET_KEY": "degrade-check-secret-key-0123456789",
    }
    env.pop("SSL_CERTFILE", None)
    env.pop("SSL_KEYFILE", None)
    proc = subprocess.Popen(
        [str(VENV_PY), "-c", RUNNER],
        cwd=str(work),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    password = "degrade123"
    try:
        if not wait_ready(client, proc):
            print(log_path.read_text(encoding="utf-8", errors="replace")[-2000:])
            check("测试实例启动", False)
            return 1
        check("测试实例启动", True, base)

        status, data = client.call("GET", "/api/meta")
        check("meta 200", status == 200, f"status={status}")
        check("mailReady=false", data.get("mailReady") is False, str(data.get("mailReady")))
        check(
            "emailCodeRequired=false",
            data.get("emailCodeRequired") is False,
            str(data.get("emailCodeRequired")),
        )

        status, _ = client.call(
            "POST", "/api/auth/email/code",
            {"scene": "register", "target": "x@example.com", "device": "degrade"},
        )
        check("发码 503", status == 503, f"status={status}")

        # 1) 邮箱都不填也能注册
        status, body = client.call(
            "POST", "/api/auth/register",
            {
                "username": "degrade01",
                "password": password,
                "captchaId": "",
                "captcha": "",
                "agree": True,
            },
        )
        user = body.get("user") or {}
        check("无邮箱注册成功", status in (200, 201), f"status={status} {str(body)[:100]}")
        check("新账号邮箱为空", user.get("email") in (None, ""), str(user.get("email")))
        check("新账号未标记已验证", user.get("emailVerified") is not True)

        # 2) 填了邮箱但不填验证码也应放行（降级：邮箱只先存下来）
        status, body = client.call(
            "POST", "/api/auth/register",
            {
                "username": "degrade02",
                "password": password,
                "email": "degrade02@example.com",
                "captchaId": "",
                "captcha": "",
                "agree": True,
            },
        )
        user = body.get("user") or {}
        check("有邮箱免验证码注册成功", status in (200, 201), f"status={status} {str(body)[:100]}")
        check("降级注册的邮箱未验证", user.get("emailVerified") is not True)

        # 3) 无邮箱账号改密：旧密码 + 图形码（此处图形码已放行）
        client.call(
            "POST", "/api/auth/login",
            {"username": "degrade01", "password": password, "captchaId": "", "captcha": ""},
        )
        status, body = client.call(
            "PUT", "/api/me/password",
            {"old_password": password, "new_password": "degrade456"},
        )
        check("无邮箱账号可改密", status == 200, f"status={status} {str(body)[:100]}")

        status, _ = client.call(
            "POST", "/api/auth/login",
            {"username": "degrade01", "password": password, "captchaId": "", "captcha": ""},
        )
        check("旧密码已失效", status >= 400, f"status={status}")

        status, _ = client.call(
            "POST", "/api/auth/login",
            {"username": "degrade01", "password": "degrade456", "captchaId": "", "captcha": ""},
        )
        check("新密码可登录", status == 200, f"status={status}")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        shutil.rmtree(work, ignore_errors=True)
        print(f"\n通过 {PASS} 项，失败 {FAIL} 项")


if __name__ == "__main__":
    raise SystemExit(main())
