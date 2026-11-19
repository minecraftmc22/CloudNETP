# -*- coding: utf-8 -*-
"""邮箱验证 / 两步验证 / 通行密钥 的离线联调。

全程不碰真实库、不发真实邮件：

- 复制 dev.db 到临时目录起一个独立实例；
- 验证码「发信」被替换成写日志文件，测试脚本从日志里读真码；
- 图形验证码在校验层被放行（**仅本次测试进程**），否则没法在无人值守时注册。

    ./.venv/Scripts/python.exe scripts/security_smoke.py
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
sys.path.insert(0, str(ROOT))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"

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


RUNNER = r"""
import json, os, sys
sys.path.insert(0, os.environ["SMOKE_ROOT"])
from app import captcha, email_code
# 只在这个测试进程里放行图形验证码：真跑时由用户看图输入
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


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar),
            urllib.request.ProxyHandler({}),
        )
        self.jar = jar

    def call(self, method: str, path: str, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self.opener.open(req, timeout=20) as res:
                raw = res.read().decode("utf-8")
                return res.status, _parse(raw)
        except urllib.error.HTTPError as exc:
            return exc.code, _parse(exc.read().decode("utf-8"))


def _parse(raw: str):
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def detail_of(body) -> dict:
    """把 FastAPI 的 detail 各类形态统一成 dict。"""
    if not isinstance(body, dict):
        return {}
    detail = body.get("detail")
    if isinstance(detail, dict):
        return detail
    return {}


def last_code(log_path: Path, scene: str, to: str = "") -> str:
    if not log_path.exists():
        return ""
    code = ""
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("scene") != scene:
            continue
        if to and str(item.get("to", "")).lower() != to.lower():
            continue
        code = str(item.get("code", ""))
    return code


def wait_ready(client: Client, proc: subprocess.Popen) -> bool:
    for _ in range(120):
        if proc.poll() is not None:
            return False
        try:
            status, _body = client.call("GET", "/api/meta")
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="cloudnetp-sec-"))
    db_copy = work / "test.db"
    shutil.copy(ROOT / "dev.db", db_copy)
    code_log = work / "codes.jsonl"
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    runner = work / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")

    env = {
        **os.environ,
        "SMOKE_ROOT": str(ROOT),
        "SMOKE_PORT": str(port),
        "SMOKE_CODE_LOG": str(code_log),
        "DATABASE_URL": f"sqlite:///{db_copy.as_posix()}",
        "STORAGE_DIR": str(work / "storage"),
        "SECRET_KEY": "smoke-secret-key-for-security-tests",
        "DB_FALLBACK_ENABLED": "false",
        "DEBUG": "false",
        # 用 .env 级别的邮件配置把 ready() 点亮，避免测试里写加密凭据
        "MAIL_ENABLED": "true",
        "MAIL_PROVIDER": "smtp",
        "MAIL_HOST": "smtp.example.test",
        "MAIL_PORT": "465",
        "MAIL_USER": "no-reply@example.test",
        "MAIL_PASSWORD": "smoke-pass",
        "MAIL_FROM": "no-reply@example.test",
        "MAIL_SMTP_SECURITY": "ssl",
        "MAIL_CODE_RESEND": "60",
    }
    env.pop("SMOKE_PROXY", None)

    log_path = work / "server.log"
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(VENV_PY), str(runner)],
        cwd=str(ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    try:
        anon = Client(base)
        if not wait_ready(anon, proc):
            print(log_path.read_text(encoding="utf-8")[-3000:])
            check("测试实例启动", False)
            return 1
        check("测试实例启动", True, base)

        stamp = str(int(time.time()))[-6:]
        email1 = f"sec{stamp}a@example.com"
        email2 = f"sec{stamp}b@example.com"
        username = f"sec{stamp}"
        password = "abc12345"
        new_password = "abc67890"

        # ---- 1. /api/meta ----
        status, meta = anon.call("GET", "/api/meta")
        check("meta 200", status == 200)
        check("meta.emailCodeRequired", meta.get("emailCodeRequired") is True, str(meta.get("emailCodeRequired")))
        check("meta.mailMisconfigured=false", meta.get("mailMisconfigured") is False)
        check("meta.codeResendSeconds=60", meta.get("codeResendSeconds") == 60)
        check("meta.passkeyReady", meta.get("passkeyReady") is True)

        # ---- 2. 注册发码 + 冷却 ----
        status, body = anon.call(
            "POST", "/api/auth/email/code", {"scene": "register", "target": email1}
        )
        check("注册发码 200", status == 200, str(body)[:120])
        check("retryAfter=60", body.get("retryAfter") == 60, str(body.get("retryAfter")))

        status, body = anon.call(
            "POST", "/api/auth/email/code", {"scene": "register", "target": email1}
        )
        check("冷却期再次发码 429", status == 429, f"status={status}")
        check("冷却体带 retryAfter", detail_of(body).get("retryAfter", 0) > 0, str(detail_of(body)))

        # ---- 3. 注册必须带邮箱验证码 ----
        base_payload = {
            "username": username,
            "password": password,
            "email": email1,
            "captchaId": "x",
            "captcha": "x",
            "agree": True,
        }
        status, body = anon.call("POST", "/api/auth/register", dict(base_payload))
        check("缺验证码注册 400", status == 400, str(body)[:120])
        status, _ = anon.call("POST", "/api/auth/register", {**base_payload, "emailCode": "000000"})
        check("错误验证码注册 400", status == 400)

        code = last_code(code_log, "register", email1)
        check("取到注册验证码", bool(code), code)
        status, body = anon.call("POST", "/api/auth/register", {**base_payload, "emailCode": code})
        check("注册 201", status == 201, str(body)[:160])
        user = body.get("user") or {}
        check("注册后邮箱已验证", user.get("emailVerified") is True)
        check("cookie 已下发", any(c.name and "session" in c.name for c in anon.jar))

        # ---- 4. 绑定 / 换绑：密码 + 新邮箱验证码 ----
        status, _ = anon.call(
            "POST",
            "/api/me/email/code",
            {"scene": "bind", "target": email2, "password": "wrong-pass"},
        )
        check("绑邮箱密码错 400", status == 400)

        status, body = anon.call(
            "POST",
            "/api/me/email/code",
            {"scene": "bind", "target": email2, "password": password},
        )
        check("绑邮箱发码 200", status == 200, str(body)[:120])
        bind_code = last_code(code_log, "bind", email2)
        check("取到换绑验证码", bool(bind_code))

        status, body = anon.call("PATCH", "/api/me/email", {"email": email2})
        check("换绑缺验证码 400", status == 400)

        status, body = anon.call("PATCH", "/api/me/email", {"email": email2, "code": bind_code})
        check("换绑 200", status == 200, str(body)[:160])
        check("换绑后邮箱已验证", (body.get("user") or {}).get("emailVerified") is True)

        # ---- 5. 修改密码：旧密码 + 邮箱码 + 图形码 ----
        status, body = anon.call(
            "PUT",
            "/api/me/password",
            {"old_password": password, "new_password": new_password, "captchaId": "x", "captcha": "x"},
        )
        check("改密缺邮箱码 400", status == 400, str(body)[:120])

        status, body = anon.call("POST", "/api/me/email/code", {"scene": "password"})
        check("改密发码 200", status == 200, str(body)[:120])
        pwd_code = last_code(code_log, "password", email2)
        check("取到改密验证码", bool(pwd_code))

        status, body = anon.call(
            "PUT",
            "/api/me/password",
            {
                "old_password": password,
                "new_password": new_password,
                "emailCode": pwd_code,
                "captchaId": "x",
                "captcha": "x",
            },
        )
        check("改密 200", status == 200, str(body)[:120])

        # ---- 6. 两步验证 ----
        status, body = anon.call("GET", "/api/me/security")
        check("security 200", status == 200, str(body)[:160])
        check("emailFactorAvailable", body.get("emailFactorAvailable") is True)
        check("passkeySupported", body.get("passkeySupported") is True)
        check("初始未开启两步验证", body.get("twoFactorEnabled") is False)

        status, body = anon.call("POST", "/api/me/email/code", {"scene": "twofactor"})
        check("两步验证发码 200", status == 200, str(body)[:120])
        tfa_code = last_code(code_log, "twofactor", email2)

        status, body = anon.call(
            "PUT", "/api/me/2fa", {"enabled": True, "password": new_password, "emailCode": tfa_code}
        )
        check("开启两步验证 200", status == 200, str(body)[:140])
        check("开关已置 true", body.get("twoFactorEnabled") is True)

        # ---- 7. 登录第二步：先拿票据，不能直接当会话 ----
        fresh = Client(base)
        status, body = fresh.call("POST", "/api/auth/login", {"username": username, "password": new_password})
        check("2FA 登录返回 200", status == 200, str(body)[:140])
        two = body.get("twoFactor") or {}
        check("返回 twoFactor.required", two.get("required") is True)
        check("下发了票据", bool(two.get("ticket")))
        check("登录第一步不下发会话", not any("session" in c.name for c in fresh.jar))
        check("可用方式含 email", "email" in (two.get("methods") or []))

        # 票据不能当会话 Cookie 用
        probe = Client(base)
        probe.jar.set_cookie(
            http.cookiejar.Cookie(
                version=0, name="cloudnetp_session", value=two.get("ticket", ""),
                port=None, port_specified=False, domain="127.0.0.1", domain_specified=False,
                domain_initial_dot=False, path="/", path_specified=True, secure=False,
                expires=None, discard=True, comment=None, comment_url=None, rest={},
            )
        )
        status, _ = probe.call("GET", "/api/me")
        check("票据不能当会话（/api/me 401）", status == 401, f"status={status}")

        status, body = fresh.call("POST", "/api/auth/2fa/email", {"ticket": two.get("ticket")})
        check("2FA 发码 200", status == 200, str(body)[:120])
        login_code = last_code(code_log, "login", email2)
        check("取到登录验证码", bool(login_code))

        status, body = fresh.call(
            "POST",
            "/api/auth/2fa/email/verify",
            {"ticket": two.get("ticket"), "code": login_code},
        )
        check("2FA 验证登录 200", status == 200, str(body)[:140])
        check("第二步后下发会话", any("session" in c.name for c in fresh.jar))

        # 同一张票据不能再用
        status, _ = fresh.call(
            "POST", "/api/auth/2fa/email", {"ticket": two.get("ticket")}
        )
        check("票据一次性（复用 401）", status == 401, f"status={status}")

        # ---- 8. 通行密钥 ----
        status, body = anon.call(
            "POST", "/api/me/passkeys/options", {"password": "wrong-pass"}
        )
        check("通行密钥密码错 400", status == 400)

        status, body = anon.call("POST", "/api/me/passkeys/options", {"password": new_password})
        check("通行密钥 options 200", status == 200, str(body)[:140])
        options = body.get("options") or {}
        check("options 带 challenge", bool(options.get("challenge")))
        check("rp.id 已推出", bool((options.get("rp") or {}).get("id")), str(options.get("rp")))

        status, body = anon.call(
            "POST",
            "/api/me/passkeys",
            {"nonce": body.get("nonce", ""), "credential": {"id": "fake"}, "label": "test"},
        )
        check("伪造凭据被拒 400", status == 400, str(body)[:120])

        status, body = anon.call("DELETE", "/api/me/passkeys/999999", {"password": new_password})
        check("删除不存在的通行密钥 404", status == 404, f"status={status}")

        # ---- 9. 邮件未就绪时降级 ----
        # 这里只验证接口契约：把 scene 换成匿名发码并断言 503 需要重启实例换配置，
        # 放到手工验收里做（见 README 的发版检查清单）。

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n通过 {passed} 项，失败 {failed} 项")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
