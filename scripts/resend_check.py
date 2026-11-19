# -*- coding: utf-8 -*-
"""验证码重发间隔：后台可配 → 后端按它限流 → /api/meta 下发。

链路：管理员在「邮箱服务」页签填 N 秒 → 存进 system_settings →
email_code 发码时按 N 限流（retryAfter=N）→ 前端只负责显示倒计时。
这里用临时实例把这条链走一遍，发信被替换成写日志，不产生真实外发。

    ./.venv/Scripts/python.exe scripts/resend_check.py
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
import json, os, sys
sys.path.insert(0, os.environ["RESEND_ROOT"])
from app import captcha, email_code
captcha.verify = lambda *a, **k: (True, "")
LOG = os.environ["RESEND_LOG"]

def fake_deliver(to, scene, code):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"to": to, "scene": scene, "code": code}) + "\n")

email_code.deliver = fake_deliver
import app.main as m
import uvicorn
uvicorn.run(m.app, host="127.0.0.1", port=int(os.environ["RESEND_PORT"]), log_level="warning")
"""


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.open = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
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
            with self.open.open(req, timeout=20) as res:
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
            if client.call("GET", "/api/meta")[0] == 200:
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="cloudnetp-resend-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    client = Client(base)
    log_file = open(work / "server.log", "w", encoding="utf-8")
    env = {
        **os.environ,
        "RESEND_ROOT": str(ROOT),
        "RESEND_PORT": str(port),
        "RESEND_LOG": str(work / "codes.jsonl"),
        "DATABASE_URL": f"sqlite:///{(work / 'test.db').as_posix()}",
        # 和降级检查一样：不隔离就会读到项目 roles.json，写进真实兜底库
        "STORAGE_DIR": str(work / "storage"),
        "SECRET_KEY": "resend-check-secret-key-0123456789",
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
    try:
        if not wait_ready(client, proc):
            print((work / "server.log").read_text(encoding="utf-8", errors="replace")[-2000:])
            check("测试实例启动", False)
            return 1
        check("测试实例启动", True, base)

        # 首个注册账号自动成为超级管理员，正好用来改后台配置
        status, body = client.call(
            "POST", "/api/auth/register",
            {
                "username": "resendadmin",
                "password": "resend12345",
                "email": "resendadmin@example.com",
                "captchaId": "",
                "captcha": "",
                "agree": True,
            },
        )
        check("首个账号注册为管理员", status in (200, 201), f"status={status} {str(body)[:90]}")
        check("确实是管理员", (body.get("user") or {}).get("role") == 1 or (body.get("user") or {}).get("isSuper") is True)

        mail = {
            "enabled": True,
            "provider": "smtp",
            "fromName": "CloudNETP",
            "fromAddr": "noreply@example.com",
            "smtpHost": "127.0.0.1",
            "smtpPort": 2525,
            "smtpUser": "noreply@example.com",
            "smtpPass": "dummy-pass",
            "smtpSecurity": "none",
            "codeResendSeconds": 15,
        }
        status, body = client.call("PUT", "/api/admin/mail", mail)
        cfg = body.get("config") or {}
        check("保存邮件配置 200", status == 200, f"status={status} {str(body)[:90]}")
        check("后台值已生效", cfg.get("codeResendSeconds") == 15, str(cfg.get("codeResendSeconds")))
        check("来源标记为 admin", cfg.get("codeResendSecondsSource") == "admin", str(cfg.get("codeResendSecondsSource")))
        check("邮件服务已就绪", cfg.get("ready") is True, str(cfg.get("ready")))

        status, body = client.call("GET", "/api/admin/mail")
        check(
            "重新读取仍是 15",
            (body.get("config") or {}).get("codeResendSeconds") == 15,
            str((body.get("config") or {}).get("codeResendSeconds")),
        )

        status, meta = client.call("GET", "/api/meta")
        check("meta 下发 15", meta.get("codeResendSeconds") == 15, str(meta.get("codeResendSeconds")))
        check("meta 标记需要邮箱验证码", meta.get("emailCodeRequired") is True, str(meta.get("emailCodeRequired")))

        target = "someone@example.com"
        status, body = client.call(
            "POST", "/api/auth/email/code",
            {"scene": "register", "target": target, "device": "resend-check"},
        )
        check("首次发码 200", status == 200, f"status={status} {str(body)[:90]}")
        check("retryAfter 跟随后台 15", body.get("retryAfter") == 15, str(body.get("retryAfter")))

        status, body = client.call(
            "POST", "/api/auth/email/code",
            {"scene": "register", "target": target, "device": "resend-check"},
        )
        detail = body.get("detail") or {}
        check("冷却内再发 429", status == 429, f"status={status}")
        check(
            "429 带结构化 retryAfter",
            isinstance(detail, dict) and 0 < (detail.get("retryAfter") or 0) <= 15,
            str(detail),
        )

        # 把间隔改成 20 立刻生效：证明后端每次发码都读当前配置，不是启动时缓存
        status, body = client.call("PUT", "/api/admin/mail", {"codeResendSeconds": 20})
        check("改间隔 200", status == 200, f"status={status}")
        client.call(
            "POST", "/api/auth/email/code",
            {"scene": "bind", "target": "other@example.com", "device": "resend-check"},
        )
        status, meta = client.call("GET", "/api/meta")
        check("改完 meta 立刻是 20", meta.get("codeResendSeconds") == 20, str(meta.get("codeResendSeconds")))
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
