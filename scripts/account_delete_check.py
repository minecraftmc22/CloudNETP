# -*- coding: utf-8 -*-
"""注销账号的回归：邮箱验证码是硬门槛，邮箱不可用时要降级。

两个实例：

1. 邮件服务可用 —— 注销必须「密码 + 邮箱验证码」，缺码 / 错码都不许删；
   发码本身也要先验密码，避免拿一个登录会话就往别人邮箱灌验证码。
2. 邮件服务没配 —— 退回只验密码，否则这条注销路径就被彻底堵死了。

全程复制 dev.db 到临时目录、发信写日志文件，不碰真实库也不发真邮件。

    ./.venv/Scripts/python.exe scripts/account_delete_check.py
"""

from __future__ import annotations

import http.cookiejar
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

PASSED = 0
FAILED: list[str] = []


def check(label: str, ok: bool, extra: object = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  OK   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label}   {extra}")


RUNNER = r"""
import json, os, sys
sys.path.insert(0, os.environ["SMOKE_ROOT"])
from app import captcha, email_code
# 只在这个测试进程里放行图形验证码
captcha.verify = lambda *a, **k: (True, "")
LOG = os.environ["SMOKE_CODE_LOG"]

def fake_deliver(to, scene, code):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"to": to, "scene": scene, "code": code}) + "\n")

email_code.deliver = fake_deliver
import app.main as m
import uvicorn
uvicorn.run(m.app, host="127.0.0.1", port=int(os.environ["SMOKE_PORT"]), log_level="warning")
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def parse(raw: str):
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class Client:
    def __init__(self, base: str) -> None:
        jar = http.cookiejar.CookieJar()
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar), urllib.request.ProxyHandler({})
        )

    def call(self, method: str, path: str, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Origin", self.base)
        try:
            with self.opener.open(req, timeout=20) as res:
                return res.status, parse(res.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, parse(exc.read().decode())


def message_of(body) -> str:
    if not isinstance(body, dict):
        return str(body)
    detail = body.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    if detail:
        return str(detail)
    return str(body)


def last_code(log_path: Path, scene: str) -> str:
    if not log_path.exists():
        return ""
    code = ""
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("scene") == scene:
            code = item.get("code") or code
    return code


class Instance:
    """起一个隔离实例；mail_on=False 时把库里的邮件配置也清掉。"""

    def __init__(self, mail_on: bool) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="cloudnetp-del-"))
        self.db = self.work / "test.db"
        shutil.copy(ROOT / "dev.db", self.db)
        self.code_log = self.work / "codes.jsonl"
        self.log_path = self.work / "server.log"
        port = free_port()
        self.base = f"http://127.0.0.1:{port}"

        if not mail_on:
            # 库里可能还留着上次后台配的邮件参数，删掉才能真的回到「未配置」
            con = sqlite3.connect(self.db)
            con.execute("DELETE FROM system_settings WHERE k='mail_config'")
            con.commit()
            con.close()

        runner = self.work / "runner.py"
        runner.write_text(RUNNER, encoding="utf-8")
        env = {
            **os.environ,
            "SMOKE_ROOT": str(ROOT),
            "SMOKE_PORT": str(port),
            "SMOKE_CODE_LOG": str(self.code_log),
            "DATABASE_URL": f"sqlite:///{self.db.as_posix()}",
            "STORAGE_DIR": str(self.work / "storage"),
            "SECRET_KEY": "smoke-secret-key-account-delete",
            "DB_FALLBACK_ENABLED": "false",
            "DEBUG": "false",
        }
        if mail_on:
            env.update({
                "MAIL_ENABLED": "true",
                "MAIL_PROVIDER": "smtp",
                "MAIL_HOST": "smtp.example.test",
                "MAIL_PORT": "465",
                "MAIL_USER": "no-reply@example.test",
                "MAIL_PASSWORD": "smoke-pass",
                "MAIL_FROM": "no-reply@example.test",
                "MAIL_SMTP_SECURITY": "ssl",
                "MAIL_CODE_RESEND": "60",
            })
        else:
            # .env 里可能有真实配置，测试进程里清掉
            for key in ("MAIL_ENABLED", "MAIL_PROVIDER", "MAIL_HOST", "MAIL_PORT",
                        "MAIL_USER", "MAIL_PASSWORD", "MAIL_FROM", "MAIL_BM_KEY"):
                env.pop(key, None)
            env["MAIL_ENABLED"] = "false"

        self._log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(VENV_PY), str(runner)], cwd=str(ROOT), env=env,
            stdout=self._log, stderr=subprocess.STDOUT,
        )
        self.client = Client(self.base)

    def wait(self) -> bool:
        for _ in range(120):
            if self.proc.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(f"{self.base}/api/meta", timeout=5) as res:
                    if res.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self._log.close()
        if FAILED:
            print("--- server log ---")
            print(self.log_path.read_text(encoding="utf-8")[-2000:])
        shutil.rmtree(self.work, ignore_errors=True)


def register(api: Client, name: str, email: str, code_log: Path, *, need_code: bool):
    body = {"username": name, "password": "abc12345", "captchaId": "", "captcha": "0000", "agree": True}
    if email:
        body["email"] = email
    if need_code:
        api.call("POST", "/api/auth/email/code", {"scene": "register", "target": email})
        body["emailCode"] = last_code(code_log, "register")
    return api.call("POST", "/api/auth/register", body)


def main() -> int:
    stamp = str(int(time.time()))[-6:]

    # ---------- A：邮件服务可用 ----------
    print("[A] 邮件服务可用：注销要密码 + 邮箱验证码")
    inst = Instance(mail_on=True)
    try:
        if not inst.wait():
            print(inst.log_path.read_text(encoding="utf-8")[-2000:])
            return 1
        api = inst.client
        _, meta = api.call("GET", "/api/meta")
        check("meta 显示注册需要邮箱码", meta.get("emailCodeRequired") is True, meta.get("emailCodeRequired"))

        email = f"del{stamp}a@example.com"
        username = f"del{stamp}a"
        status, body = register(api, username, email, inst.code_log, need_code=True)
        check("注册成功（邮箱已验证）", status == 201, f"{status} {body}")

        # 发码必须先验密码
        status, body = api.call("POST", "/api/me/email/code", {
            "scene": "delete", "password": "wrong-pass", "device": "del-test",
        })
        check("密码错误时拒绝发码", status == 400 and "密码" in message_of(body), f"{status} {message_of(body)}")
        check("密码错误时没有真发信", not inst.code_log.exists() or "delete" not in inst.code_log.read_text(encoding="utf-8"))

        status, body = api.call("POST", "/api/me/email/code", {
            "scene": "delete", "password": "abc12345", "device": "del-test",
        })
        check("密码正确时发码成功", status == 200 and body.get("sent") is True, f"{status} {body}")
        code = last_code(inst.code_log, "delete")
        check("收到 delete 场景的验证码", len(code) == 6, code)

        # 不带码 / 错码都不许删
        status, body = api.call("POST", "/api/me/delete", {"password": "abc12345"})
        check("缺验证码时 400 且提示", status == 400 and "验证码" in message_of(body), f"{status} {message_of(body)}")
        status, _ = api.call("GET", "/api/me")
        check("账号还在（缺码没被删）", status == 200, status)

        status, body = api.call("POST", "/api/me/delete", {"password": "abc12345", "emailCode": "000000"})
        check("错验证码时 400", status == 400, f"{status} {message_of(body)}")
        status, _ = api.call("GET", "/api/me")
        check("账号还在（错码没被删）", status == 200, status)

        status, body = api.call("POST", "/api/me/delete", {"password": "wrong-pass", "emailCode": code})
        check("密码错误时 400", status == 400, f"{status} {message_of(body)}")

        status, body = api.call("POST", "/api/me/delete", {"password": "abc12345", "emailCode": code})
        check("密码 + 正确验证码 → 注销成功", status == 200 and body.get("deleted") is True,
              f"{status} {str(body)[:160]}")
        status, _ = api.call("GET", "/api/me")
        check("注销后会话失效", status == 401, status)

        again = Client(inst.base)
        status, _ = again.call("POST", "/api/auth/login", {"username": username, "password": "abc12345"})
        check("注销后无法再登录", status in (400, 401, 403, 404), status)
    finally:
        inst.close()

    if FAILED:
        print(f"\n[A] 有失败项，先停下：{FAILED}")
        return 1

    # ---------- B：邮件服务未配置（降级） ----------
    print("\n[B] 邮件服务不可用：退回只验密码")
    inst2 = Instance(mail_on=False)
    try:
        if not inst2.wait():
            print(inst2.log_path.read_text(encoding="utf-8")[-2000:])
            return 1
        api = inst2.client
        _, meta = api.call("GET", "/api/meta")
        check("meta 显示不需要邮箱码", meta.get("emailCodeRequired") is False, meta.get("emailCodeRequired"))

        email = f"del{stamp}b@example.com"
        username = f"del{stamp}b"
        status, body = register(api, username, email, inst2.code_log, need_code=False)
        check("无邮件服务也能注册", status == 201, f"{status} {str(body)[:120]}")

        status, body = api.call("POST", "/api/me/delete", {"password": "abc12345"})
        check("只给密码就注销成功（降级）", status == 200 and body.get("deleted") is True,
              f"{status} {str(body)[:160]}")
    finally:
        inst2.close()

    print("\n" + ("全部通过 %d 项" % PASSED if not FAILED else "失败 %d 项: %s" % (len(FAILED), FAILED)))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
