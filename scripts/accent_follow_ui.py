# -*- coding: utf-8 -*-
"""「底色跟着主题色走」的界面回归。

用户报的问题：文件页搜索框、侧栏存储空间卡片、设置 / 管理后台的分类栏（分段控件）
底色是死的层色，换主题色时它们纹丝不动，跟卡片上的主题薄雾对不上。

脚本起临时实例、注册一个管理员，把主题色在紫 / 绿之间来回切，检查这几个元素的
computed background-image 是否真的带上了新主色，并截图亮暗两套主题。

    ./.venv/Scripts/python.exe scripts/accent_follow_ui.py
"""

from __future__ import annotations

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

ACCENT_A = "#c084fc"  # 默认粉紫
RGB_A = "192, 132, 252"
ACCENT_B = "#22c55e"  # 换成绿色
RGB_B = "34, 197, 94"


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


STYLE_JS = """
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const cs = getComputedStyle(el);
  return {
    bgImage: cs.backgroundImage,
    bgColor: cs.backgroundColor,
    border: cs.borderTopColor,
    color: cs.color,
    tint: getComputedStyle(document.documentElement).getPropertyValue("--accent-tint").trim(),
  };
}
"""


def main() -> int:
    from playwright.sync_api import sync_playwright

    work = Path(tempfile.mkdtemp(prefix="cloudnetp-accent-"))
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
        "SECRET_KEY": "smoke-secret-key-for-accent",
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
        username = f"acc{stamp}"
        password = "abc12345"

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            r = ctx.request.post(
                f"{base}/api/auth/register",
                data={
                    "username": username,
                    "password": password,
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

            r = ctx.request.post(
                f"{base}/api/auth/login",
                data={"username": username, "password": password, "captchaId": "", "captcha": "0000"},
            )
            check("登录", r.ok, str(r.status))

            def set_accent(hex_color: str) -> None:
                resp = ctx.request.patch(
                    f"{base}/api/me/settings",
                    data={"accentColor": hex_color, "accentStyle": {"mode": "solid", "colors": []}},
                )
                check(f"主题色写入 {hex_color}", resp.ok, str(resp.status))

            def load(route: str, ready: str) -> None:
                page.goto(f"{base}/#/{route}", wait_until="domcontentloaded")
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector(ready, timeout=20000)
                page.wait_for_timeout(600)

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

            targets = [
                ("files", ".toolbar .searchbox", "文件页搜索框", ".toolbar .searchbox .textbox"),
                ("files", ".toolbar .searchbox", "侧栏存储空间", ".nav__storage"),
                ("files", ".toolbar .searchbox", "次级按钮（新建文件夹）", '[data-act="new-folder"]'),
                ("files", ".toolbar .searchbox", "排序按钮", ".sort-btn"),
                ("settings", ".settings-tabs", "设置页分类栏", ".settings-tabs .segmented"),
                ("settings", ".settings-tabs", "设置页分类栏滑块", ".settings-tabs .segmented__thumb"),
                ("admin", ".admin-tabs", "后台分类栏", ".admin-tabs .segmented"),
                ("admin", ".admin-tabs", "后台分类栏滑块", ".admin-tabs .segmented__thumb"),
            ]

            results: dict[str, dict[str, dict]] = {}
            for hex_color, rgb in ((ACCENT_A, RGB_A), (ACCENT_B, RGB_B)):
                set_accent(hex_color)
                results[hex_color] = {}
                for route, ready, label, sel in targets:
                    load(route, ready)
                    info = page.evaluate(STYLE_JS, sel)
                    if not info:
                        check(f"{label} 存在", False, sel)
                        continue
                    results[hex_color][sel] = info
                    check(
                        f"{label} 底色含主题色 {hex_color}",
                        rgb in info["bgImage"] or rgb in info["border"],
                        (info["bgImage"] or "")[:90],
                    )
                # 分类栏选中项的文字也掺了主色：Chrome 把 color-mix 算成 color(srgb …)，
                # 不能按 rgb 字符串搜，直接跟另一次换色比
                load("admin", ".admin-tabs")
                results[hex_color]["active"] = page.evaluate(
                    STYLE_JS, ".admin-tabs .segmented__btn.is-active"
                )

            # 换色前后必须真的不一样（防「本来就写死成紫色」的假通过）
            for _route, _ready, label, sel in targets:
                a = results.get(ACCENT_A, {}).get(sel)
                b = results.get(ACCENT_B, {}).get(sel)
                if not (a and b):
                    continue
                check(f"{label} 换色后有变化", a["bgImage"] != b["bgImage"], "")

            # 分类栏选中项的文字要掺主题色，不是死的 --text-1
            active_a = results.get(ACCENT_A, {}).get("active") or {}
            active_b = results.get(ACCENT_B, {}).get("active") or {}
            check(
                "后台分类栏选中项文字跟着主题色",
                active_a.get("color") and active_a["color"] != active_b.get("color"),
                f"{active_a.get('color')} / {active_b.get('color')}",
            )

            # 截图：亮 / 暗两套，紫色与绿色各一张
            for hex_color in (ACCENT_A, ACCENT_B):
                set_accent(hex_color)
                for theme in ("light", "dark"):
                    load("files", ".toolbar .searchbox")
                    page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
                    page.wait_for_timeout(250)
                    page.screenshot(path=str(OUT / f"accent-files-{theme}-{hex_color[1:]}.png"))
                    load("settings", ".settings-tabs")
                    page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
                    page.wait_for_timeout(250)
                    page.screenshot(path=str(OUT / f"accent-settings-{theme}-{hex_color[1:]}.png"))
                    load("admin", ".admin-tabs")
                    page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
                    page.wait_for_timeout(250)
                    page.screenshot(path=str(OUT / f"accent-admin-{theme}-{hex_color[1:]}.png"))

            check("页面没有 JS 报错", not errors, str(errors[:3]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n合计 {PASS + FAIL} 项：通过 {PASS}，失败 {FAIL}")
    print(f"截图目录：{OUT}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
