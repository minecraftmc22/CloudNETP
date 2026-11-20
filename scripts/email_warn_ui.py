# -*- coding: utf-8 -*-
"""账号 → 邮箱「未验证」红字提醒 + 「去验证」按钮的界面回归（Playwright）。

起临时实例（复制 dev.db，图形码放行、验证码发信写日志），然后：
已验证 / 未验证 / 未绑定三种状态下的显示，点「去验证」走完密码 + 验证码。

    ./.venv/Scripts/python.exe scripts/email_warn_ui.py
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
    for line in log_path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item.get("scene") == scene:
            code = str(item.get("code", ""))
    return code


def set_verified(db_path: Path, username: str, verified: bool, drop_email: bool = False) -> None:
    con = sqlite3.connect(db_path)
    if drop_email:
        con.execute("UPDATE users SET email=NULL, email_verified=0 WHERE username=?", (username,))
    else:
        con.execute(
            "UPDATE users SET email_verified=? WHERE username=?", (1 if verified else 0, username)
        )
    con.commit()
    con.close()


def main() -> int:
    from playwright.sync_api import sync_playwright

    work = Path(tempfile.mkdtemp(prefix="cloudnetp-emailwarn-"))
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
        "SECRET_KEY": "smoke-secret-key-for-email-warn",
        "DB_FALLBACK_ENABLED": "false",
        # 邮件「参数齐备」就够 ready() 为真，不会真发信（deliver 已被替换成写日志）
        "MAIL_ENABLED": "true",
        "MAIL_PROVIDER": "smtp",
        "MAIL_HOST": "smtp.example.test",
        "MAIL_PORT": "465",
        "MAIL_FROM": "no-reply@example.test",
        "MAIL_SMTP_SECURITY": "ssl",
    }

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
        if not wait_http(base, proc):
            print(log_path.read_text(encoding="utf-8")[-3000:])
            return 1

        stamp = str(int(time.time()))[-6:]
        username = f"ew{stamp}"
        email = f"ew{stamp}@example.com"
        password = "abc12345"

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            api = ctx.request
            r = api.post(f"{base}/api/auth/email/code", data={"scene": "register", "target": email})
            check("注册发码", r.ok, str(r.status))
            code = last_code(code_log, "register")
            r = api.post(
                f"{base}/api/auth/register",
                data={
                    "username": username,
                    "password": password,
                    "email": email,
                    "emailCode": code,
                    "captchaId": "",
                    "captcha": "0000",
                    "agree": True,
                },
            )
            check("注册成功（邮箱已验证）", r.ok, str(r.status) + " " + r.text()[:120])

            page = ctx.new_page()
            page.add_init_script(
                "document.addEventListener('DOMContentLoaded', () => {"
                "  const s = document.createElement('style');"
                "  s.textContent = '#mascot{display:none!important}';"
                "  document.head.appendChild(s);"
                "});"
            )
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on(
                "console",
                lambda msg: errors.append(msg.text) if msg.type == "error" else None,
            )

            def open_account(width: int = 1280) -> None:
                page.set_viewport_size({"width": width, "height": 900})
                # 先回首页让 app 完成登录判断：直接进 #/settings 时偶尔会先撞一次 401 停在登录页
                page.goto(base, wait_until="domcontentloaded")
                page.wait_for_timeout(1200)
                page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
                # 只改 hash 是同文档导航、不会重新拉 /api/me，必须真刷一次才能读到改过的库
                page.reload(wait_until="domcontentloaded")
                try:
                    page.wait_for_selector('[data-settings-tab="account"]', timeout=15000)
                except Exception:
                    page.screenshot(path=str(OUT / "email-warn-debug.png"))
                    print("  DEBUG url:", page.url)
                    print("  DEBUG body:", page.inner_text("body")[:400].replace("\n", " | "))
                    print("  DEBUG errors:", errors[:5])
                    raise
                page.click('[data-settings-tab="account"]')
                page.wait_for_selector("[data-email-desc]", timeout=10000)

            # 1. 已验证：不显示红字
            set_verified(db_path, username, True)
            open_account()
            page.wait_for_timeout(400)
            check("已验证时隐藏红字提醒", page.is_hidden("[data-email-warn]"))
            check(
                "已验证描述含「可用于登录」",
                "可用于登录" in page.inner_text("[data-email-desc]"),
                page.inner_text("[data-email-desc]"),
            )

            # 2. 未验证：红字可见、颜色是 danger
            set_verified(db_path, username, False)
            open_account()
            page.wait_for_timeout(400)
            warn = page.locator("[data-email-warn]")
            check("未验证时显示红字提醒", warn.is_visible())
            color = warn.evaluate("el => getComputedStyle(el).color")
            check("提醒是红色", color in ("rgb(229, 72, 77)", "rgb(255, 99, 105)"), color)
            text = page.inner_text("[data-email-warn]")
            check("提醒文案提到 未验证/两步验证", "未验证" in text and "两步验证" in text, text)
            check("描述行不再重复「（未验证）」", "（未验证）" not in page.inner_text("[data-email-desc]"))
            check("换绑按钮仍在", page.inner_text('[data-act="email"]').strip() == "换绑")

            verify_btn = page.locator('[data-act="email-verify"]')
            check("红字旁显示「去验证」", verify_btn.is_visible(), verify_btn.inner_text().strip())

            # 点「去验证」应直接展开邮箱面板并停在密码步
            verify_btn.click()
            page.wait_for_timeout(250)
            check("点去验证展开面板", page.is_visible("[data-email-panel]"))
            check(
                "面板停在密码步",
                page.is_visible('[data-email-step="password"]'),
            )
            check(
                "邮箱已预填当前地址",
                page.input_value("[data-email-address]") == email,
                page.input_value("[data-email-address]"),
            )
            page.click('[data-act="email-cancel"]')
            page.wait_for_timeout(200)
            check("取消后收起面板", page.is_hidden("[data-email-panel]"))

            size = page.evaluate(
                "() => { const r = document.querySelector('[data-email-warn]').getBoundingClientRect();"
                " return {w: r.width, h: r.height}; }"
            )
            check("提醒行有高度", size["h"] > 10, f'{size}')
            overflow = page.evaluate(
                "() => document.querySelector('[data-email-warn]').scrollWidth -"
                " document.querySelector('[data-email-warn]').clientWidth"
            )
            check("桌面无横向溢出", overflow <= 1, str(overflow))

            page.screenshot(path=str(OUT / "email-warn-desk.png"), full_page=False)

            # 3. 手机宽度：折行但不溢出
            open_account(390)
            page.wait_for_timeout(400)
            page.locator('[data-settings-pane="account"]').scroll_into_view_if_needed()
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            check("390px 页面无横向溢出", overflow <= 1, str(overflow))
            page.screenshot(path=str(OUT / "email-warn-mobile.png"), full_page=False)

            # 4. 端到端：点「去验证」走完密码 + 验证码，红字应消失
            open_account()
            page.click('[data-act="email-verify"]')
            page.fill("[data-email-password]", password)
            page.click('[data-act="email-next"]')
            page.wait_for_timeout(200)
            page.click('[data-act="email-send"]')
            page.wait_for_timeout(900)
            bind_code = last_code(code_log, "bind")
            check("去验证能发码", bool(bind_code), bind_code)
            page.fill("[data-email-code]", bind_code)
            page.click('[data-act="email-save"]')
            page.wait_for_timeout(900)
            check("验证后红字消失", page.is_hidden("[data-email-warn]"))
            check(
                "验证后描述回到已验证口径",
                "可用于登录" in page.inner_text("[data-email-desc]"),
                page.inner_text("[data-email-desc]"),
            )
            me = api.get(f"{base}/api/me").json()
            check("服务端 emailVerified=True", me.get("user", {}).get("emailVerified") is True)

            # 5. 未绑定邮箱：既不显示红字也不该有地址
            set_verified(db_path, username, False, drop_email=True)
            open_account()
            page.wait_for_timeout(400)
            check("无邮箱时隐藏红字提醒", page.is_hidden("[data-email-warn]"))
            check(
                "无邮箱时描述是引导文案",
                "还没有绑定" in page.inner_text("[data-email-desc]"),
                page.inner_text("[data-email-desc]"),
            )

            check("无前端报错", not errors, "; ".join(errors[:3]))
            browser.close()

        print(f"\n通过 {PASS} / 失败 {FAIL}")
        return 0 if FAIL == 0 else 1
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
