# -*- coding: utf-8 -*-
"""注销账号面板的界面回归（Playwright）。

起临时实例（复制 dev.db、图形码放行、发信写日志），然后：
点「注销账号」→ 确认后果 → 面板里的密码 / 邮箱验证码行是否就位、
空密码点发送是否被拦、发码后按钮是否进入倒计时、取消能否收起，
最后把邮箱置为未验证，确认面板退化成「只验密码」并给出说明。

    ./.venv/Scripts/python.exe scripts/account_delete_ui.py
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
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)
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
sys.path.insert(0, os.environ["SMOKE_ROOT"])
from app import captcha, email_code
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


def last_code(log_path: Path, scene: str) -> str:
    code = ""
    if not log_path.exists():
        return code
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("scene") == scene:
            code = str(item.get("code", ""))
    return code


def main() -> int:
    from playwright.sync_api import sync_playwright

    work = Path(tempfile.mkdtemp(prefix="cloudnetp-delui-"))
    db_path = work / "test.db"
    shutil.copy(ROOT / "dev.db", db_path)
    code_log = work / "codes.jsonl"
    port = free_port()
    base = f"http://localhost:{port}"
    runner = work / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")

    env = {
        **os.environ,
        "SMOKE_ROOT": str(ROOT),
        "SMOKE_PORT": str(port),
        "SMOKE_CODE_LOG": str(code_log),
        "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "STORAGE_DIR": str(work / "storage"),
        "SECRET_KEY": "smoke-secret-key-account-delete-ui",
        "DB_FALLBACK_ENABLED": "false",
        "MAIL_ENABLED": "true",
        "MAIL_PROVIDER": "smtp",
        "MAIL_HOST": "smtp.example.test",
        "MAIL_PORT": "465",
        "MAIL_FROM": "no-reply@example.test",
        "MAIL_SMTP_SECURITY": "ssl",
    }
    log_path = work / "server.log"
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([str(VENV_PY), str(runner)], cwd=str(ROOT), env=env,
                            stdout=log_file, stderr=subprocess.STDOUT)

    try:
        if not wait_http(base, proc):
            print(log_path.read_text(encoding="utf-8")[-3000:])
            return 1

        stamp = str(int(time.time()))[-6:]
        username = f"du{stamp}"
        email = f"du{stamp}@example.com"
        password = "abc12345"

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            api = ctx.request
            api.post(f"{base}/api/auth/email/code", data={"scene": "register", "target": email})
            r = api.post(f"{base}/api/auth/register", data={
                "username": username, "password": password, "email": email,
                "emailCode": last_code(code_log, "register"),
                "captchaId": "", "captcha": "0000", "agree": True,
            })
            check("注册成功", r.ok, str(r.status) + " " + r.text()[:120])

            page = ctx.new_page()
            page.add_init_script(
                "document.addEventListener('DOMContentLoaded', () => {"
                "  const s = document.createElement('style');"
                "  s.textContent = '#mascot{display:none!important}';"
                "  document.head.appendChild(s);"
                "});"
            )
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(f"{exc} || {getattr(exc, 'stack', '')}"[:600]))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

            def open_account() -> None:
                page.set_viewport_size({"width": 1280, "height": 900})
                # 先回首页让 app 完成登录判断（#/settings 直接进来时，cookie 还没被浏览器
                # 收下会先撞一次 401，停在登录页上）
                page.goto(base, wait_until="domcontentloaded")
                page.wait_for_timeout(1200)
                page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
                page.wait_for_timeout(600)
                page.click('[data-settings-tab="account"]')
                page.wait_for_selector('[data-act="delete-account"]', timeout=10000)

            def open_panel() -> None:
                page.click('[data-act="delete-account"]')
                page.wait_for_selector(".dialog [data-act='ok']", timeout=8000)
                page.click(".dialog [data-act='ok']")
                page.wait_for_selector("[data-delete-panel]:not([hidden])", timeout=8000)

            open_account()
            check("危险区域有注销按钮", page.is_visible('[data-act="delete-account"]'))

            open_panel()
            check("确认后果后展开面板", page.is_visible("[data-delete-panel]"))
            check("面板里有密码框", page.is_visible("[data-delete-password]"))
            check("邮箱验证码行可见（邮箱已验证）", page.is_visible("[data-delete-code]"))
            check("降级提示此时是隐藏的", not page.is_visible("[data-delete-hint]"))

            # 空密码点发码 → 面板内提示，不发请求
            page.click('[data-act="delete-send"]')
            page.wait_for_timeout(300)
            check("空密码点发送被拦下", page.is_visible("[data-delete-hint]")
                  and "密码" in page.inner_text("[data-delete-hint]"), page.inner_text("[data-delete-hint]"))

            # 填密码发码 → 按钮进入倒计时
            page.fill("[data-delete-password]", password)
            page.click('[data-act="delete-send"]')
            page.wait_for_timeout(1200)
            btn_text = page.inner_text('[data-act="delete-send"]')
            check("发码后按钮进入倒计时", "重发" in btn_text, btn_text)
            check("真的发出了 delete 场景的码", len(last_code(code_log, "delete")) == 6,
                  last_code(code_log, "delete"))
            page.screenshot(path=str(OUT / "account-delete-panel.png"))

            # 取消收起
            page.click('[data-act="delete-cancel"]')
            page.wait_for_timeout(300)
            check("取消后收起面板", not page.is_visible("[data-delete-panel]"))

            # 降级：邮箱置为未验证 → 面板只剩密码
            con = sqlite3.connect(db_path)
            con.execute("UPDATE users SET email_verified=0 WHERE username=?", (username,))
            con.commit()
            con.close()

            open_account()
            open_panel()
            check("邮箱未验证时验证码行隐藏", not page.is_visible("[data-delete-code]"))
            check("并给出降级说明", page.is_visible("[data-delete-hint]")
                  and "密码" in page.inner_text("[data-delete-hint]"), page.inner_text("[data-delete-hint]"))
            page.screenshot(path=str(OUT / "account-delete-fallback.png"))

            # 手机端不溢出
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(300)
            overflow = page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
            check("390px 无横向溢出", not overflow)
            page.screenshot(path=str(OUT / "account-delete-mobile.png"))

            check("无前端报错", not errors, errors[:3])
            browser.close()

        print("\n" + ("全部通过 %d 项" % PASS if not FAIL else "失败 %d 项" % FAIL))
        return 0 if not FAIL else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        if FAIL:
            print("--- server log ---")
            print(log_path.read_text(encoding="utf-8")[-2000:])
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
