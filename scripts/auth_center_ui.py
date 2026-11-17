# -*- coding: utf-8 -*-
"""登录页垂直居中回归：内容比视口矮时必须上下留一样多，内容比视口高时要能滚。

  ./.venv/Scripts/python.exe scripts/auth_center_ui.py

A/B 对照：同一页面里把 .auth-root 的 grid-template-rows 改回 auto（修复前的行为），
确认修复前后差值一个贴顶、一个居中。
"""

from __future__ import annotations

import os
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


def wait_up(base: str, proc: subprocess.Popen, log_path: Path) -> bool:
    for _ in range(90):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"{base}/api/version", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    print(log_path.read_text(encoding="utf-8", errors="replace")[-1500:])
    return False


MEASURE = """
(sel) => {
  const root = document.querySelector('.auth-root');
  const stage = document.querySelector(sel);
  const rRoot = root.getBoundingClientRect();
  const rStage = stage.getBoundingClientRect();
  const cs = getComputedStyle(root);
  const padTop = parseFloat(cs.paddingTop);
  const padBottom = parseFloat(cs.paddingBottom);
  return {
    top: Math.round(rStage.top - rRoot.top - padTop),
    bottom: Math.round(rRoot.bottom - padBottom - rStage.bottom),
    stageH: Math.round(rStage.height),
    scrollable: root.scrollHeight > root.clientHeight + 4,
    scrollH: root.scrollHeight,
    clientH: root.clientHeight,
    innerH: window.innerHeight,
  };
}
"""


def gaps(page, sel: str = ".auth-stage") -> dict:
    return page.evaluate(MEASURE, sel)


def balanced(g: dict) -> bool:
    """上下留白相等（差 ≤ 2px）且都不是 0 —— 也就是真的居中了"""
    return g["top"] > 12 and abs(g["top"] - g["bottom"]) <= 2


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-centerui-")).resolve()
    db_path = tmp / "ui.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["STORAGE_DIR"] = str(tmp / "storage")
    os.environ["DB_FALLBACK_ENABLED"] = "0"
    os.environ["DB_PROBE_INTERVAL"] = "0"
    os.environ["DB_MIRROR_INTERVAL"] = "0"

    sys.path.insert(0, str(ROOT))
    from app.config import settings
    from app.db import engine, ensure_schema

    ensure_schema(engine)

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
            "STORAGE_DIR": str(tmp / "storage"),
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "RELOAD": "false",
            "DB_PROBE_INTERVAL": "0",
            "DB_FALLBACK_ENABLED": "0",
            "SECRET_KEY": settings.SECRET_KEY,
        }
    )
    log_path = tmp / "server.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "main.py"], cwd=str(ROOT), env=env,
        stdout=log_file, stderr=subprocess.STDOUT,
    )

    from playwright.sync_api import sync_playwright

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来")
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            errors: list[str] = []

            for label, w, h in [
                ("1080p 全屏", 1920, 1080),
                ("1920 屏 125% 缩放", 1536, 765),
                ("笔记本 1440×900", 1440, 900),
                ("1280×720", 1280, 720),
                ("1366 屏 125% 缩放", 1093, 614),
            ]:
                print(f"\n[{label}] {w}×{h}")
                ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1)
                page = ctx.new_page()
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(base, wait_until="domcontentloaded")
                page.wait_for_selector(".auth-card", timeout=20000)
                page.wait_for_timeout(900)  # 等 content-in 动画结束，别量到动画中间态

                if label.startswith("1366"):
                    # 注册态表单最长，用它验证「超高时能滚」
                    page.click('[data-mode="register"]')
                    page.wait_for_timeout(500)

                now = gaps(page)
                print(f"       修复后 {now}")
                if not now["scrollable"]:
                    check("内容比视口矮时上下留白相等", balanced(now), str(now))
                else:
                    check("内容超高时从顶部开始（能滚到最上面）", now["top"] <= 4, str(now))
                    check("滚动容器确实可滚", now["scrollH"] > now["clientH"], str(now))

                # A/B：还原修复前的 grid-template-rows:auto
                page.evaluate(
                    """() => { document.querySelector('.auth-root').style.gridTemplateRows = 'auto'; }"""
                )
                page.wait_for_timeout(150)
                old = gaps(page)
                print(f"       修复前 {old}")
                check("修复前确实是贴顶（复现问题）", old["top"] < 4, str(old))

                page.evaluate(
                    """() => { document.querySelector('.auth-root').style.gridTemplateRows = ''; }"""
                )
                page.wait_for_timeout(150)
                page.screenshot(path=str(OUT / f"auth_center_{w}x{h}.png"))
                ctx.close()

            # 申诉页也用 auth-root：塞个同样结构进去，确认它跟着一起居中
            print("\n[申诉页壳子 1440×900]")
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
            page = ctx.new_page()
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(base, wait_until="domcontentloaded")
            page.wait_for_selector(".auth-card", timeout=20000)
            page.wait_for_timeout(700)
            page.evaluate(
                """() => {
                    document.querySelector('.auth-root').innerHTML =
                      '<div class="appeal-stage"><div class="appeal-card" style="height:420px"></div></div>';
                }"""
            )
            page.wait_for_timeout(900)
            ap = gaps(page, ".appeal-stage")
            print(f"       {ap}")
            check("申诉卡垂直居中", balanced(ap), str(ap))
            ctx.close()

            # 手机竖屏：只留表单，仍要居中
            print("\n[手机 390×844]")
            ctx = browser.new_context(
                viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True
            )
            page = ctx.new_page()
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(base, wait_until="domcontentloaded")
            page.wait_for_selector(".auth-card", timeout=20000)
            page.wait_for_timeout(900)
            mob = gaps(page)
            print(f"       {mob}")
            check("手机上表单垂直居中", balanced(mob), str(mob))
            page.screenshot(path=str(OUT / "auth_center_mobile.png"))
            ctx.close()

            check("没有 JS 报错", not errors, str(errors[:1]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_file.close()

    print()
    print(f"通过 {PASS} / {PASS + FAIL}，截图在 {OUT}")
    return 1 if FAIL else 0


sys.exit(main())
