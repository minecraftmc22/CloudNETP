# -*- coding: utf-8 -*-
"""125% 缩放（小视口）下的登录 / 注册页与看板娘位置回归。

用 1093×614 的视口模拟 1366×768 屏幕开到 125%：

  1. 注册表单比屏幕高时能滚，提交按钮滚得到
  2. 密码框右侧的眼睛能切换明文；确认密码输入不同时立刻提示
  3. 用户协议页滚轮能下滑
  4. 浏览器里存着的旧偏移不会把看板娘顶到屏幕外

    ./.venv/Scripts/python.exe scripts/auth_zoom_ui.py
"""

from __future__ import annotations

import os
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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-zoomui-")).resolve()
    db_path = tmp / "ui.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["STORAGE_DIR"] = str(tmp / "storage")
    os.environ["DB_FALLBACK_ENABLED"] = "0"
    os.environ["DB_PROBE_INTERVAL"] = "0"
    os.environ["DB_MIRROR_INTERVAL"] = "0"

    sys.path.insert(0, str(ROOT))
    from app.config import settings
    from app.db import SessionLocal, engine, ensure_schema
    from app.models import User, utcnow
    from app.security import create_token, hash_password

    ensure_schema(engine)
    pw = "Passw0rd!2024"
    with SessionLocal() as db:
        boss = User(
            username="zoomadmin",
            password_hash=hash_password(pw),
            email="zoom@example.com",
            email_verified=True,
            role=1,
            is_super=True,
            quota_bytes=10 * 1024**3,
            token_version=1,
            created_at=utcnow(),
        )
        db.add(boss)
        db.commit()
        uid, ver = boss.id, boss.token_version

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
            # 1366×768 物理像素在 125% 缩放下约等于这么大
            ctx = browser.new_context(viewport={"width": 1093, "height": 614}, device_scale_factor=2)
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))

            print("\n[A] 注册页：能滚，提交按钮滚得到")
            page.goto(base, wait_until="domcontentloaded")
            page.wait_for_selector(".auth-card", timeout=20000)
            page.click('[data-mode="register"]')
            page.wait_for_timeout(500)
            page.fill('[name="account"]', "zoomuser")

            before = page.evaluate(
                """() => {
                    const root = document.querySelector('.auth-root');
                    const submit = document.querySelector('[data-submit]');
                    return {
                      overflowY: getComputedStyle(root).overflowY,
                      scrollable: root.scrollHeight > root.clientHeight + 4,
                      submitBottom: Math.round(submit.getBoundingClientRect().bottom),
                      innerH: window.innerHeight,
                    };
                }"""
            )
            check("登录页自己是滚动容器", before["overflowY"] in ("auto", "scroll"), before["overflowY"])
            check("内容比视口高（模拟生效）", before["scrollable"], str(before))
            check("初始时提交按钮在视口外", before["submitBottom"] > before["innerH"], str(before))
            page.screenshot(path=str(OUT / "zoom_register_top.png"))

            page.mouse.move(760, 420)
            page.mouse.wheel(0, 620)
            page.wait_for_timeout(450)
            after = page.evaluate(
                """() => {
                    const root = document.querySelector('.auth-root');
                    const submit = document.querySelector('[data-submit]');
                    return {
                      scrollTop: Math.round(root.scrollTop),
                      submitBottom: Math.round(submit.getBoundingClientRect().bottom),
                      innerH: window.innerHeight,
                    };
                }"""
            )
            check("滚轮确实滚了", after["scrollTop"] > 100, str(after))
            check("提交按钮已进入视口", after["submitBottom"] <= after["innerH"], str(after))
            page.screenshot(path=str(OUT / "zoom_register_bottom.png"))

            print("\n[B] 密码可见按钮")
            page.evaluate("document.querySelector('.auth-root').scrollTo(0, 0)")
            page.fill('[name="password"]', "Passw0rd!2024")
            pwd_type = page.evaluate("document.querySelector('[name=\"password\"]').type")
            check("默认是掩码", pwd_type == "password", pwd_type)
            page.click('[name="password"] ~ .pwd-eye')
            page.wait_for_timeout(200)
            shown = page.evaluate(
                """() => {
                    const input = document.querySelector('[name="password"]');
                    const btn = input.parentElement.querySelector('.pwd-eye');
                    return { type: input.type, pressed: btn.getAttribute('aria-pressed'),
                             icon: btn.querySelector('use').getAttribute('href') };
                }"""
            )
            check("点一下变明文", shown["type"] == "text", str(shown))
            check("按钮状态同步", shown["pressed"] == "true" and shown["icon"] == "#i-eye-off", str(shown))
            page.screenshot(path=str(OUT / "zoom_register_eye.png"))
            page.click('[name="password"] ~ .pwd-eye')
            page.wait_for_timeout(150)
            check("再点回到掩码", page.evaluate("document.querySelector('[name=\"password\"]').type") == "password")

            print("\n[C] 确认密码即时提示")
            page.fill('[name="password"]', "Passw0rd!2024")
            page.fill('[name="confirm"]', "Passw0rd!2025")
            page.wait_for_timeout(250)
            bad = page.evaluate(
                """() => {
                    const err = document.querySelector('[data-confirm-err]');
                    const box = document.querySelector('[name="confirm"]').closest('.textbox');
                    const msg = document.querySelector('[data-msg]');
                    return { hidden: err.hidden, text: err.textContent.trim(),
                             bad: box.classList.contains('is-bad'),
                             formMsg: msg.textContent.trim() };
                }"""
            )
            check("不用提交就提示不一致", not bad["hidden"] and "不一致" in bad["text"], str(bad))
            check("输入框同时标红", bool(bad["bad"]), str(bad))
            check("没触发整表单报错", not bad["formMsg"], bad["formMsg"])
            page.screenshot(path=str(OUT / "zoom_register_mismatch.png"))

            page.fill('[name="confirm"]', "Passw0rd!2024")
            page.wait_for_timeout(250)
            fixed = page.evaluate(
                """() => {
                    const err = document.querySelector('[data-confirm-err]');
                    const box = document.querySelector('[name="confirm"]').closest('.textbox');
                    return { hidden: err.hidden, bad: box.classList.contains('is-bad') };
                }"""
            )
            check("改对后提示消失", fixed["hidden"] and not fixed["bad"], str(fixed))

            print("\n[D] 协议页能滚")
            page.goto(f"{base}/terms", wait_until="domcontentloaded")
            page.wait_for_timeout(700)
            page.mouse.move(540, 400)
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(450)
            scrolled = page.evaluate("window.scrollY")
            check("滚轮能下滑协议页", scrolled > 200, str(scrolled))
            page.screenshot(path=str(OUT / "zoom_terms.png"))

            print("\n[E] 旧偏移不会把看板娘顶出屏幕")
            ctx.add_cookies([{"name": "cloudnetp_session", "value": create_token(uid, ver), "url": base}])
            page.goto(base, wait_until="domcontentloaded")
            page.evaluate(
                "localStorage.setItem('mascot-pos', JSON.stringify({dx: 400, dy: 400}))"
            )
            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_selector("#mascot", timeout=20000)
            page.wait_for_timeout(1500)
            pos = page.evaluate(
                """() => {
                    const root = document.documentElement;
                    const m = document.querySelector('#mascot');
                    const r = m.getBoundingClientRect();
                    return {
                      dy: root.style.getPropertyValue('--mascot-dy'),
                      dx: root.style.getPropertyValue('--mascot-dx'),
                      bottom: Math.round(r.bottom),
                      right: Math.round(r.right),
                      innerH: window.innerHeight, innerW: window.innerWidth,
                      saved: localStorage.getItem('mascot-pos'),
                    };
                }"""
            )
            check("dy 被夹回 0", pos["dy"] in ("0px", "0"), str(pos))
            check("dx 被夹回 0", pos["dx"] in ("0px", "0"), str(pos))
            check("底边不越过视口", pos["bottom"] <= pos["innerH"] + 1, str(pos))
            check("右边不越过视口", pos["right"] <= pos["innerW"] + 1, str(pos))
            check("坏位置没被写回缓存", "400" not in (pos["saved"] or ""), str(pos["saved"]))
            page.screenshot(path=str(OUT / "zoom_mascot.png"))

            check("没有 JS 报错", not errors, str(errors[:1]))
            ctx.close()
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
