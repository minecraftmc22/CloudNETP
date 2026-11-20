# -*- coding: utf-8 -*-
"""自绘下拉浮层（ui/select.js）的界面回归。

起临时实例、注册一个管理员，在后台存储页点开「清理时间」下拉，验证：
浮层不是系统白底、选项与原生一致、选中项跟随、能点选 / 键盘 / Esc / 点外面关闭、
触屏仍走系统选择器；最后到邮件服务页确认 .selectbox 里的下拉也能用。

    ./.venv/Scripts/python.exe scripts/select_ui.py
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
import os, sys
sys.path.insert(0, os.environ["SMOKE_ROOT"])
from app import captcha
captcha.verify = lambda *a, **k: (True, "")
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


def main() -> int:
    from playwright.sync_api import sync_playwright

    work = Path(tempfile.mkdtemp(prefix="cloudnetp-select-"))
    db_path = work / "test.db"
    shutil.copy(ROOT / "dev.db", db_path)
    port = free_port()
    base = f"http://localhost:{port}"
    runner = work / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")

    env = {
        **os.environ,
        "SMOKE_ROOT": str(ROOT),
        "SMOKE_PORT": str(port),
        "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "STORAGE_DIR": str(work / "storage"),
        "SECRET_KEY": "smoke-secret-key-for-select",
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
        username = f"sel{stamp}"

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            r = ctx.request.post(
                f"{base}/api/auth/register",
                data={
                    "username": username,
                    "password": "abc12345",
                    "captchaId": "",
                    "captcha": "0000",
                    "agree": True,
                },
            )
            check("注册", r.ok, str(r.status))
            con = sqlite3.connect(db_path)
            con.execute("UPDATE users SET role=1 WHERE username=?", (username,))
            con.commit()
            con.close()

            page = ctx.new_page()
            page.add_init_script(
                "document.addEventListener('DOMContentLoaded', () => {"
                "  const s = document.createElement('style');"
                "  s.textContent = '#mascot{display:none!important}"
                "*{animation-duration:0s!important;transition-duration:0s!important}';"
                "  document.head.appendChild(s);"
                "});"
            )
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            page.goto(f"{base}/#/admin", wait_until="domcontentloaded")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector('[data-tab="storage"]', timeout=20000)
            page.click('[data-tab="storage"]')
            page.wait_for_selector("[data-cleanup-hour]", timeout=15000)
            page.wait_for_timeout(500)

            sel = page.locator("[data-cleanup-hour]")
            check("原生 select 还在（闭合态样式不变）", sel.count() == 1)
            box = sel.bounding_box()
            print("  select rect:", box)

            # 1) 鼠标点开：出现自绘浮层
            sel.click()
            page.wait_for_selector("#ui-select-menu:not([hidden])", timeout=5000)
            page.wait_for_timeout(300)  # 等 pop-in 动画落定，否则量到的是缩放中的盒子
            menu = page.locator("#ui-select-menu")
            check("点开出现浮层", menu.is_visible())

            bg = menu.evaluate("el => getComputedStyle(el).backgroundColor")
            check("浮层不是白色", bg not in ("rgb(255, 255, 255)", "rgba(0, 0, 0, 0)"), bg)
            radius = menu.evaluate("el => getComputedStyle(el).borderRadius")
            check("浮层有圆角", radius not in ("0px", ""), radius)

            items = page.locator(".ui-select__item")
            check("选项数量与 select 一致", items.count() == sel.evaluate("el => el.options.length"),
                  f"{items.count()}")
            sel_text = sel.evaluate("el => el.options[el.selectedIndex].text")
            active = page.locator(".ui-select__item.is-selected")
            check("选中项有标记", active.count() == 1, active.inner_text() if active.count() else "-")
            check("选中项与原生一致", active.inner_text().strip() == sel_text.strip(),
                  f"{active.inner_text().strip()} / {sel_text.strip()}")

            # 浮层要贴着 select，不能跑到屏幕角落
            mbox = menu.bounding_box()
            below = 0 <= mbox["y"] - (box["y"] + box["height"]) <= 12
            above = -2 <= (box["y"] - (mbox["y"] + mbox["height"])) <= 12
            check("浮层贴着 select（下方或翻到上方）", abs(mbox["x"] - box["x"]) <= 2 and (below or above),
                  f'{mbox["x"]:.0f},{mbox["y"]:.0f} vs {box["x"]:.0f},{box["y"]:.0f} h={mbox["height"]:.0f}')
            page.screenshot(path=str(OUT / "select-open.png"))

            # 暗色下再看一眼，别只在亮色里像样
            page.evaluate("document.documentElement.dataset.theme = 'dark'")
            page.wait_for_timeout(250)
            page.screenshot(path=str(OUT / "select-open-dark.png"))
            page.evaluate("document.documentElement.dataset.theme = ''")

            # 2) 选一个选项：写回 select 并收起
            target = page.locator(".ui-select__item").nth(8)
            want = target.inner_text().strip()
            target.click()
            page.wait_for_timeout(250)
            check("选完收起浮层", page.is_hidden("#ui-select-menu"))
            now = sel.evaluate("el => el.options[el.selectedIndex].text")
            check("select 值已更新", now.strip() == want, f"{now.strip()} / {want}")

            # 3) 键盘：↓ 打开、↑↓ 移动、Esc 关闭
            sel.focus()
            page.keyboard.press("ArrowDown")
            page.wait_for_selector("#ui-select-menu:not([hidden])", timeout=3000)
            check("键盘能打开", page.is_visible("#ui-select-menu"))
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            check("Esc 关闭且不重开", page.is_hidden("#ui-select-menu"))

            # 4) 点外面关闭
            sel.click()
            page.wait_for_selector("#ui-select-menu:not([hidden])", timeout=3000)
            page.mouse.click(20, 400)
            page.wait_for_timeout(250)
            check("点空白处关闭", page.is_hidden("#ui-select-menu"))

            # 5) 触屏不接管：仍然交给系统选择器
            sel.dispatch_event("pointerdown", {"pointerType": "touch", "bubbles": True})
            page.wait_for_timeout(200)
            check("触屏不打开自绘浮层", page.is_hidden("#ui-select-menu"))

            # 6) 包在 .selectbox 里的下拉（后台-邮件服务）也能用
            page.click('[data-tab="mail"]')
            page.wait_for_timeout(1200)
            wrapped = page.locator(".selectbox select").first
            if wrapped.count():
                wrapped.click()
                page.wait_for_selector("#ui-select-menu:not([hidden])", timeout=4000)
                check(".selectbox 内也能打开", page.is_visible("#ui-select-menu"))
                page.screenshot(path=str(OUT / "select-selectbox.png"))
                page.keyboard.press("Escape")
            else:
                check(".selectbox 内也能打开", False, "没找到 .selectbox select")

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
