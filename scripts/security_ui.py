# -*- coding: utf-8 -*-
"""邮箱验证 / 两步验证 / 通行密钥 的界面回归（Playwright）。

起一个临时实例（复制 dev.db、验证码发信写日志、图形码放行），
然后真在浏览器里：注册 → 设置页 → 三个面板逐个展开截图。

    ./.venv/Scripts/python.exe scripts/security_ui.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
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
    from playwright.sync_api import sync_playwright

    work = Path(tempfile.mkdtemp(prefix="cloudnetp-secui-"))
    shutil.copy(ROOT / "dev.db", work / "test.db")
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
        "DATABASE_URL": f"sqlite:///{(work / 'test.db').as_posix()}",
        "STORAGE_DIR": str(work / "storage"),
        "SECRET_KEY": "smoke-secret-key-for-security-ui",
        "DB_FALLBACK_ENABLED": "false",
        "MAIL_ENABLED": "true",
        "MAIL_PROVIDER": "smtp",
        "MAIL_HOST": "smtp.example.test",
        "MAIL_PORT": "465",
        "MAIL_USER": "no-reply@example.test",
        "MAIL_PASSWORD": "smoke-pass",
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
            check("测试实例启动", False)
            return 1
        check("测试实例启动", True, base)

        stamp = str(int(time.time()))[-6:]
        username = f"ui{stamp}"
        email = f"ui{stamp}@example.com"
        password = "abc12345"

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            # 看板娘画布会盖住设置卡片底部的按钮，测试里先藏掉
            page.add_init_script(
                "document.addEventListener('DOMContentLoaded', () => {"
                "  const s = document.createElement('style');"
                "  s.textContent = '#mascot{display:none!important}';"
                "  document.head.appendChild(s);"
                "});"
            )
            # 虚拟认证器：无头浏览器里也能真的走完 WebAuthn 注册，不用真指纹
            cdp = page.context.new_cdp_session(page)
            cdp.send("WebAuthn.enable")
            cdp.send(
                "WebAuthn.addVirtualAuthenticator",
                {
                    "options": {
                        "protocol": "ctap2",
                        "transport": "internal",
                        "hasResidentKey": True,
                        "hasUserVerification": True,
                        "isUserVerified": True,
                        "automaticPresenceSimulation": True,
                    }
                },
            )
            errors: list[str] = []
            unauthorized: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on(
                "response",
                lambda r: unauthorized.append(r.url) if r.status == 401 else None,
            )
            page.on(
                "console",
                lambda msg: errors.append(msg.text) if msg.type == "error" else None,
            )

            page.goto(base, wait_until="domcontentloaded")
            page.wait_for_selector(".auth-card", timeout=20000)

            # 默认是登录模式，邮箱验证码行不该出现
            check("登录模式隐藏邮箱验证码", page.is_hidden("[data-f-emailcode]"))

            page.click('[data-mode="register"]')
            page.wait_for_timeout(250)
            page.fill('input[name="account"]', username)
            page.fill('input[name="email"]', email)
            page.wait_for_timeout(500)
            check("注册模式显示邮箱验证码", page.is_visible("[data-f-emailcode]"))
            check("邮箱标记为必填", (page.text_content("[data-email-opt]") or "").strip() == "必填")
            page.screenshot(path=str(OUT / "security-register.png"))

            page.click('[data-act="send-code"]')
            page.wait_for_timeout(1200)
            send_text = (page.text_content('[data-act="send-code"]') or "").strip()
            check("发送后按钮进入倒计时", "秒后重发" in send_text, send_text)
            check("按钮在倒计时里被禁用", page.is_disabled('[data-act="send-code"]'))

            code = last_code(code_log, "register")
            check("服务端真的发出了验证码", bool(code), code)

            page.fill('input[name="emailcode"]', code)
            # 注册分两步：第一步只有账号与邮箱，密码在第二步
            check("第一步隐藏密码字段", page.is_hidden('input[name="password"]'))
            check("第一步没有「上一步」", page.is_hidden('[data-act="reg-back"]'))
            page.click("[data-submit]")
            page.wait_for_timeout(400)
            check("第二步显示密码字段", page.is_visible('input[name="password"]'))
            check("第二步出现「上一步」", page.is_visible('[data-act="reg-back"]'))
            page.fill('input[name="password"]', password)
            page.fill('input[name="confirm"]', password)
            page.fill('input[name="captcha"]', "0000")
            page.check("[data-agree]")
            page.click("[data-submit]")
            page.wait_for_selector(".nav, #view-root", timeout=25000)
            page.wait_for_timeout(1200)
            check("注册后进入应用", page.is_visible("#view-root"))

            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector('[data-settings-tab="account"]', timeout=20000)
            page.click('[data-settings-tab="account"]')
            page.wait_for_timeout(600)

            check("有两步验证开关", page.is_visible("label.switch:has([data-twofa-toggle])"))
            check("有通行密钥卡片", page.is_visible('[data-card="passkeys"]'))
            page.wait_for_timeout(800)
            note = page.text_content("[data-passkey-note]")
            check("通行密钥可用（localhost 是安全上下文）", page.is_hidden("[data-passkey-note]"), str(note))

            page.click('[data-act="email"]')
            page.wait_for_timeout(300)
            check("邮箱面板第一步要密码", page.is_visible("[data-email-panel] [data-email-step='password']"))
            page.fill("[data-email-password]", password)
            page.click('[data-act="email-next"]')
            page.wait_for_timeout(300)
            check("第二步填邮箱与验证码", page.is_visible("[data-email-panel] [data-email-step='code']"))
            page.screenshot(path=str(OUT / "security-account-email.png"))
            page.click('[data-act="email-back"]')
            page.wait_for_timeout(300)
            check("可以退回密码步骤", page.is_visible("[data-email-panel] [data-email-step='password']"))
            page.click('[data-act="email-cancel"]')

            page.click('[data-act="password"]')
            page.wait_for_timeout(900)
            check("改密面板展开", page.is_visible("[data-password-panel]"))
            check("改密有图形验证码", page.is_visible("[data-pwd-captcha-img]"))
            src = page.get_attribute("[data-pwd-captcha-img]", "src") or ""
            check("图形验证码图片已加载", src.startswith("data:image"), src[:40])
            check("有邮箱时要求邮箱验证码", page.is_visible("[data-pwd-email-row]"))
            page.screenshot(path=str(OUT / "security-account-password.png"))
            page.click('[data-act="pwd-cancel"]')

            # 开关的 input 是视觉隐藏的，点 label 才是真实用户操作路径
            page.locator("[data-twofa-toggle]").evaluate("e => e.closest('label').click()")
            page.wait_for_timeout(400)
            check("开关面板展开", page.is_visible("[data-twofa-panel]"))
            check("开关未真正勾上（等服务端确认）", not page.is_checked("[data-twofa-toggle]"))
            page.fill("[data-twofa-password]", password)
            page.click('[data-act="twofa-send"]')
            page.wait_for_timeout(1500)
            page.fill("[data-twofa-code]", last_code(code_log, "twofactor"))
            page.screenshot(path=str(OUT / "security-account-twofa.png"))
            page.click('[data-act="twofa-save"]')
            page.wait_for_timeout(1500)
            check(
                "两步验证已开启",
                page.is_checked("[data-twofa-toggle]"),
                (page.text_content("[data-twofa-desc]") or "").strip()[:40],
            )

            page.click('[data-act="passkey-add"]')
            page.wait_for_timeout(300)
            check("添加通行密钥面板展开", page.is_visible("[data-passkey-panel]"))
            page.screenshot(path=str(OUT / "security-account-passkey.png"))
            page.fill("[data-passkey-label]", "测试设备")
            page.fill("[data-passkey-password]", password)
            page.click('[data-act="passkey-create"]')
            page.wait_for_selector(".passkey-item", timeout=20000)
            check("通行密钥添加成功", page.is_visible(".passkey-item"))
            check(
                "列表显示备注",
                "测试设备" in (page.text_content(".passkey-item__label") or ""),
                (page.text_content(".passkey-item__label") or "").strip(),
            )

            page.click('[data-act="passkey-remove"]')
            page.wait_for_selector('#dialog-host [data-act="ok"]', timeout=10000)
            page.fill('#dialog-host input[name="password"]', password)
            page.click('#dialog-host [data-act="ok"]')
            page.wait_for_timeout(1500)
            check("通行密钥已移除", not page.is_visible(".passkey-item"))

            # --- 新会话登录：密码过了还要过邮箱验证码 ---
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page2 = ctx.new_page()
            page2.on("pageerror", lambda exc: errors.append(str(exc)))
            page2.goto(base, wait_until="domcontentloaded")
            page2.wait_for_selector(".auth-card", timeout=20000)
            page2.fill('input[name="account"]', username)
            page2.fill('input[name="password"]', password)
            page2.check("[data-agree]")
            page2.click("[data-submit]")
            page2.wait_for_timeout(2500)
            page2.wait_for_selector("[data-tf]:not([hidden])", timeout=20000)
            check("密码通过后进入第二步", page2.is_visible("[data-tf]"))
            check("第一步表单已收起", page2.is_hidden("[data-form]"))
            # 只有邮箱这一种因子时要自动发码，不该让用户再点一次
            login_code = ""
            for _ in range(40):
                login_code = last_code(code_log, "login")
                if login_code:
                    break
                page2.wait_for_timeout(250)
            check("第二步自动发出了邮箱验证码", bool(login_code))
            page2.fill('[name="tfcode"]', login_code)
            page2.screenshot(path=str(OUT / "security-login-twofa.png"))
            page2.click("[data-tf-submit]")
            page2.wait_for_selector("#view-root", timeout=25000)
            page2.wait_for_timeout(800)
            check("第二步通过后进入应用", page2.is_visible("#view-root"))
            ctx.close()

            # --- 手机宽度：新增面板不能把页面撑出横向滚动 ---
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector("[data-settings-tab]", timeout=20000)
            # 移动端页签可能是下拉，直接触发元素的 click 更稳
            page.locator('[data-settings-tab="account"]').evaluate("e => e.click()")
            page.wait_for_timeout(600)
            page.locator('[data-act="email"]').evaluate("e => e.click()")
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT / "security-mobile-account.png"))
            overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            check("手机宽度无横向溢出", overflow <= 1, f"overflow={overflow}px")

            page.wait_for_timeout(400)
            # 未登录时 /api/me 必然 401，应用自己会处理；除它之外出现 401 就是真问题
            unexpected = [u for u in unauthorized if "/api/me" not in u]
            check("没有意外的 401", not unexpected, " | ".join(unexpected[:3]))
            real_errors = [
                e
                for e in errors
                if "favicon" not in e.lower()
                and not ("Failed to load resource" in e and "401" in e)
            ]
            check("页面无 JS 报错", not real_errors, " | ".join(real_errors[:3]))

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项；截图在 {OUT}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
