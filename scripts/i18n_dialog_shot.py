# -*- coding: utf-8 -*-
"""弹窗 / 右键菜单的 i18n 回归：确认 Dialog、Toast、菜单的文案在英文界面下也翻了。

    ./.venv/Scripts/python.exe scripts/i18n_dialog_shot.py [--lang en-US]

只起一个临时实例（现有库的副本），不写真实数据：新建文件夹只填名字不提交，菜单只看文案。
"""

from __future__ import annotations

import os
import re
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
sys.path.insert(0, str(ROOT))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)
CJK = re.compile(r"[\u4e00-\u9fa5]")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def snapshot_db(dst: Path) -> None:
    src_path = ROOT / "dev.db"
    if not src_path.is_file():
        src_path = ROOT / "storage" / "db" / "fallback.db"
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        src.close()
        out.close()


def main() -> None:
    from app.config import settings
    from app.security import create_token

    lang = "en-US"
    if "--lang" in sys.argv:
        lang = sys.argv[sys.argv.index("--lang") + 1]

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-dlg-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    row = con.execute("select id, token_version from users where role = 1 order by id limit 1").fetchone()
    con.close()
    if not row:
        raise SystemExit("库里没有管理员")
    cookie = create_token(row[0], row[1])

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db_copy.as_posix()}",
            "STORAGE_DIR": str(tmp / "storage"),
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "RELOAD": "false",
            "DB_PROBE_INTERVAL": "0",
            "SECRET_KEY": settings.SECRET_KEY,
        }
    )
    log_path = tmp / "server.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "main.py"], cwd=str(ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT
    )

    from playwright.sync_api import sync_playwright

    passed = failed = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  OK   {label}")
        else:
            failed += 1
            print(f"  FAIL {label}  {detail}")

    try:
        up = False
        for _ in range(80):
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"{base}/api/version", timeout=2) as resp:
                    if resp.status == 200:
                        up = True
                        break
            except Exception:
                pass
            time.sleep(0.5)
        if not up:
            raise SystemExit("服务没起来\n" + log_path.read_text(encoding="utf-8", errors="replace")[-1500:])

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(
                    executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe", headless=True
                )
            ctx = browser.new_context(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
            ctx.add_cookies([{"name": "cloudnetp_session", "value": cookie, "url": base}])
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.add_init_script(
                f"try {{ localStorage.setItem('cloudnetp.lang', '{lang}'); }} catch (e) {{}}"
                "window.addEventListener('DOMContentLoaded', () => {"
                "  const s = document.createElement('style');"
                "  s.textContent = '#mascot{display:none!important}';"
                "  document.head.appendChild(s);"
                "});"
            )
            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_selector('[data-act="new-folder"]', timeout=15000)
            page.wait_for_timeout(500)

            print("\n[A] 新建文件夹弹窗")
            page.click('[data-act="new-folder"]')
            page.wait_for_selector(".dialog", timeout=8000)
            page.wait_for_timeout(300)
            dialog_text = page.inner_text(".dialog")
            page.screenshot(path=str(OUT / f"i18n_dialog_folder_{lang}.png"))
            if lang == "zh-CN":
                check("中文弹窗正常", "新建文件夹" in dialog_text, dialog_text.replace("\n", " | "))
            else:
                check("标题已翻译", "新建文件夹" not in dialog_text, dialog_text.replace("\n", " | "))
                check(
                    "按钮是英文",
                    "Cancel" in dialog_text and ("Save" in dialog_text or "Create" in dialog_text),
                    dialog_text,
                )
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)

            print("\n[B] 右键菜单")
            row_el = page.query_selector(".frow, .fcard")
            if row_el:
                row_el.click(button="right")
            else:
                page.mouse.click(600, 320, button="right")
            page.wait_for_selector(".popover", timeout=8000)
            page.wait_for_timeout(300)
            menu_text = page.inner_text(".popover")
            page.screenshot(path=str(OUT / f"i18n_menu_{lang}.png"))
            if lang != "zh-CN":
                check("菜单没有中文", not CJK.search(menu_text), menu_text.replace("\n", " | "))
            else:
                check("中文菜单正常", "新建文件夹" in menu_text or "刷新" in menu_text, menu_text)
            page.keyboard.press("Escape")

            print("\n[C] 设置页 → 修改密码弹窗")
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector('[data-act="password"]', timeout=15000)
            page.click('[data-act="password"]')
            page.wait_for_selector(".dialog", timeout=8000)
            page.wait_for_timeout(300)
            pwd_text = page.inner_text(".dialog")
            page.screenshot(path=str(OUT / f"i18n_dialog_password_{lang}.png"))
            if lang != "zh-CN":
                check("修改密码弹窗没有中文", not CJK.search(pwd_text), pwd_text.replace("\n", " | "))

            check("没有 JS 报错", not errors, str(errors[:3]))
            print(f"\n结果: {passed} 通过, {failed} 失败")
            print(f"截图目录: {OUT}")
            browser.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        log_file.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
