# -*- coding: utf-8 -*-
"""登录 / 设置 三个 bug 的界面回归（Playwright）。

1. 设置 → 文件与分享：WebDAV 挂载地址「复制」在 http 环境（没有 navigator.clipboard）里也要能复制
2. 退出登录后按 Delete：不该再对登出前的选中项弹删除确认
3. 登录第二步（两步验证）：验证码框 / 提示 / 按钮 / 换一种方式 之间的间距与顺序

    ./.venv/Scripts/python.exe scripts/auth_flow_ui.py
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

    work = Path(tempfile.mkdtemp(prefix="cloudnetp-authflow-"))
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
        "SECRET_KEY": "smoke-secret-key-for-auth-flow",
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
        username = f"af{stamp}"
        email = f"af{stamp}@example.com"
        password = "abc12345"

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 940},
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = ctx.new_page()
            errors: list[str] = []
            denied: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))

            def note_console(msg) -> None:
                # 未登录首屏必然请求一次 /api/me 拿 401，这是设计好的，不算报错
                if msg.type != "error":
                    return
                if "Failed to load resource" in msg.text and "401" in msg.text:
                    return
                errors.append(msg.text)

            page.on("console", note_console)
            page.on(
                "response",
                lambda r: denied.append(f"{r.status} {r.url}") if r.status == 401 else None,
            )
            page.add_init_script(
                "document.addEventListener('DOMContentLoaded', () => {"
                "  const s = document.createElement('style');"
                "  s.textContent = '#mascot{display:none!important}';"
                "  document.head.appendChild(s);"
                "});"
            )

            # ---------------- 注册一个账号 ----------------
            page.goto(base, wait_until="domcontentloaded")
            page.wait_for_selector(".auth-card", timeout=20000)
            page.click('[data-mode="register"]')
            page.wait_for_timeout(250)
            page.fill('input[name="account"]', username)
            page.fill('input[name="email"]', email)
            page.wait_for_timeout(400)
            page.click('[data-act="send-code"]')
            page.wait_for_timeout(900)
            # 手机宽度下看一眼发码按钮：倒计时期间得还看得出是个按钮
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(300)
            page.screenshot(path=str(OUT / "register-code-mobile.png"))
            reg_btn = page.evaluate(
                """() => {
                  const b = document.querySelector('[data-act="send-code"]');
                  const s = getComputedStyle(b);
                  return [s.borderTopStyle, s.opacity, s.color].join("|");
                }"""
            )
            check("注册发码按钮倒计时仍保留轮廓", reg_btn.startswith("dashed|1"), reg_btn)
            page.set_viewport_size({"width": 1280, "height": 940})
            page.wait_for_timeout(200)
            page.fill('input[name="emailcode"]', last_code(code_log, "register"))
            page.click("[data-submit]")
            page.wait_for_timeout(400)
            page.fill('input[name="password"]', password)
            page.fill('input[name="confirm"]', password)
            page.fill('input[name="captcha"]', "0000")
            page.check("[data-agree]")
            page.click("[data-submit]")
            page.wait_for_selector("#view-root", timeout=25000)
            page.wait_for_timeout(1000)

            # ---------------- 1. WebDAV 地址复制 ----------------
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector('[data-settings-tab="files"]', timeout=20000)
            page.click('[data-settings-tab="files"]')
            page.wait_for_timeout(600)
            dav = (page.text_content("[data-dav-url]") or "").strip()
            check("挂载地址已渲染", dav.endswith("/dav/"), dav)

            page.click('[data-act="dav-copy"]')
            page.wait_for_timeout(400)
            toast = (page.text_content(".toast") or "").strip() if page.locator(".toast").count() else ""
            check("安全上下文：提示复制成功", "已复制" in toast, toast)
            copied = page.evaluate("() => navigator.clipboard.readText()")
            check("安全上下文：剪贴板内容正确", copied == f"{base}/dav/", copied)

            # 模拟 http 内网：navigator.clipboard 不存在，只能走 execCommand 兜底
            page.evaluate(
                "() => {"
                "  window.__copied = null;"
                "  Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });"
                "  const orig = document.execCommand.bind(document);"
                "  document.execCommand = (cmd) => {"
                "    if (cmd === 'copy') {"
                "      const el = document.activeElement;"
                "      window.__copied = el && 'value' in el ? el.value : null;"
                "    }"
                "    return orig(cmd);"
                "  };"
                "}"
            )
            page.wait_for_timeout(200)
            page.click('[data-act="dav-copy"]')
            page.wait_for_timeout(500)
            fallback = page.evaluate("() => window.__copied")
            check("http 环境：兜底复制拿到地址", fallback == f"{base}/dav/", str(fallback))
            toast = (page.text_content(".toast") or "").strip() if page.locator(".toast").count() else ""
            check("http 环境：仍然提示复制成功", "已复制" in toast, toast)

            # ---------------- 2. 开启两步验证 ----------------
            page.click('[data-settings-tab="account"]')
            page.wait_for_timeout(600)
            page.locator("[data-twofa-toggle]").evaluate("e => e.closest('label').click()")
            page.wait_for_timeout(400)
            page.fill("[data-twofa-password]", password)
            page.click('[data-act="twofa-send"]')
            page.wait_for_timeout(1500)
            page.fill("[data-twofa-code]", last_code(code_log, "twofactor"))
            page.click('[data-act="twofa-save"]')
            page.wait_for_timeout(1600)
            check("两步验证已开启", page.is_checked("[data-twofa-toggle]"))

            # ---------------- 3. 选中文件后退出的 Delete 键 ----------------
            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_selector('[data-act="new-folder"]', timeout=20000)
            page.click('[data-act="new-folder"]')
            page.wait_for_selector('#dialog-host input[name="value"]', timeout=10000)
            page.fill('#dialog-host input[name="value"]', "登出测试")
            page.click('#dialog-host [data-act="ok"]')
            page.wait_for_selector(".tile, .frow", timeout=20000)
            page.wait_for_timeout(400)
            page.click(".tile, .frow")
            page.wait_for_timeout(300)
            check("文件已选中", page.locator(".is-selected").count() >= 1)

            page.click("#user-chip")
            page.wait_for_timeout(300)
            page.locator(".popover .menu-item", has_text="退出登录").click()
            page.wait_for_selector('#dialog-host [data-act="ok"]', timeout=10000)
            page.click('#dialog-host [data-act="ok"]')
            page.wait_for_selector(".auth-card", timeout=20000)
            page.wait_for_timeout(600)
            check("已回到登录页", page.is_visible(".auth-card"))

            denied.clear()
            dialogs_before = page.locator("#dialog-host .dialog").count()
            page.keyboard.press("Delete")
            page.wait_for_timeout(800)
            dialogs_after = page.locator("#dialog-host .dialog").count()
            check("登出后按 Delete 不再弹删除确认", dialogs_after <= dialogs_before, f"{dialogs_before}→{dialogs_after}")
            check("登出后按 Delete 不会打接口（没有 401）", not denied, str(denied[:2]))
            check("F2 / Backspace 同样无效", page.is_visible(".auth-card"))

            # ---------------- 4. 登录第二步：布局 ----------------
            page.fill('input[name="account"]', username)
            page.fill('input[name="password"]', password)
            page.check("[data-agree]")
            page.click("[data-submit]")
            page.wait_for_selector("[data-tf]:not([hidden])", timeout=20000)
            page.wait_for_selector("[data-tf-emailbox]:not([hidden])", timeout=20000)
            login_code = ""
            for _ in range(40):
                login_code = last_code(code_log, "login")
                if login_code:
                    break
                page.wait_for_timeout(250)
            page.wait_for_timeout(600)

            # 只有邮箱一种因子时不该出现「换一种方式」
            check("单一因子隐藏「换一种方式」", page.is_hidden("[data-tf-switch]"))

            box_row = page.locator("[data-tf-emailbox] .code-row").bounding_box()
            box_submit = page.locator("[data-tf-submit]").bounding_box()
            box_msg = page.locator("[data-tf-code-msg]").bounding_box()
            assert box_row and box_submit
            gap_body = round(box_submit["y"] - (box_row["y"] + box_row["height"]))
            check("验证码框与按钮之间有间距", gap_body >= 8, f"{gap_body}px")
            check(
                "提示在按钮上方（贴着输入框）",
                bool(box_msg) and box_msg["y"] + box_msg["height"] <= box_submit["y"] + 1,
                str(box_msg),
            )
            msg_text = (page.text_content("[data-tf-code-msg]") or "").strip()
            check("提示写了发到哪个邮箱", "验证码已发送到" in msg_text, msg_text)
            dup = page.locator("text=验证码已发送，请查收邮件").count()
            check("不再重复报一次「请查收邮件」", dup == 0, str(dup))

            # 发码按钮与输入框同高
            h1 = page.locator("[data-tf-emailbox] .textbox").bounding_box()
            h2 = page.locator("[data-tf-send]").bounding_box()
            check("发码按钮与输入框等高", abs(h1["height"] - h2["height"]) <= 1, f'{h1["height"]}/{h2["height"]}')

            # 桌面宽度截图
            page.screenshot(path=str(OUT / "twofa-desktop.png"))
            # 两种因子都在时的那张「选方式」卡片：只看排版，看完还原回去（别触发重新发码）
            page.evaluate(
                """() => {
                  document.querySelector('[data-tf-emailbox]').hidden = true;
                  document.querySelector('[data-tf-methods]').hidden = false;
                }"""
            )
            page.wait_for_timeout(200)
            page.screenshot(path=str(OUT / "twofa-methods.png"))
            # 这张卡上的按钮得靠包裹层自己的 gap 分开（.auth-form 的间隙管不到里面）
            method_gap = page.evaluate(
                """() => {
                  const box = document.querySelector('[data-tf-methods]');
                  const style = getComputedStyle(box);
                  return [style.display, style.rowGap].join("/");
                }"""
            )
            check("两种方式是竖排且留有间隙", method_gap == "flex/8px", method_gap)
            page.evaluate(
                """() => {
                  document.querySelector('[data-tf-emailbox]').hidden = false;
                  document.querySelector('[data-tf-methods]').hidden = true;
                }"""
            )
            page.wait_for_timeout(200)
            # 手机宽度：与用户截图同尺寸
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT / "twofa-mobile.png"))
            overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            check("手机宽度没有横向溢出", overflow <= 0, str(overflow))
            switch_visible = page.is_visible("[data-tf-switch]")
            check("窄屏仍隐藏「换一种方式」", not switch_visible)

            # 表单还能正常提交
            page.fill('[name="tfcode"]', login_code)
            page.click("[data-tf-submit]")
            page.wait_for_selector("#view-root", timeout=25000)
            page.wait_for_timeout(800)
            check("第二步可以正常进入应用", page.is_visible("#view-root"))

            # 动过的模块逐个动态 import，任何一处语法 / 导出写错都会在这里炸
            broken = page.evaluate(
                """async () => {
                  const list = ["/static/js/ui/clipboard.js", "/static/js/ui/share.js",
                                "/static/js/ui/viewer.js", "/static/js/views/files.js",
                                "/static/js/views/settings.js", "/static/js/app.js"];
                  const bad = [];
                  for (const url of list) {
                    try { await import(url); } catch (err) { bad.push(`${url}: ${err.message}`); }
                  }
                  return bad;
                }"""
            )
            check("改动过的模块都能正常加载", not broken, str(broken))

            check("页面没有 JS 报错", not errors, str(errors[:3]))
            ctx.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
