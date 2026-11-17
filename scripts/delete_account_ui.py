# -*- coding: utf-8 -*-
"""注销账号 UI 冒烟：设置页危险区 → 后果说明 → 密码 + 邮箱验证 → 真的删掉并回到登录页。

临时库（不动 dev.db）+ 独立实例 + Playwright。

    ./.venv/Scripts/python.exe scripts/delete_account_ui.py
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
    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-delui-")).resolve()
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
    from app.security import hash_password

    ensure_schema(engine)
    pw = "Passw0rd!2024"
    with SessionLocal() as db:
        boss = User(
            username="delboss",
            password_hash=hash_password(pw),
            email="boss@example.com",
            email_verified=True,
            role=1,
            is_super=True,
            quota_bytes=10 * 1024**3,
            token_version=1,
            created_at=utcnow(),
        )
        member = User(
            username="delme",
            password_hash=hash_password(pw),
            email="delme@example.com",
            email_verified=False,
            role=0,
            quota_bytes=10 * 1024**3,
            token_version=1,
            created_at=utcnow(),
        )
        db.add_all([boss, member])
        db.commit()
        member_id = member.id

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
            ctx = browser.new_context(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
            page = ctx.new_page()
            errors: list[str] = []

            def on_console(msg) -> None:
                if msg.type == "error" and not msg.text.startswith("Failed to load resource"):
                    errors.append(msg.text)

            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", on_console)
            # 看板娘是 fixed 覆盖层，会挡住右下角的按钮：测设置页时先收起来
            page.add_init_script(
                "window.addEventListener('DOMContentLoaded', () => {"
                "  const s = document.createElement('style');"
                "  s.textContent = '#mascot{display:none!important}';"
                "  document.head.appendChild(s);"
                "});"
            )

            print("\n[A] 普通用户登录并打开设置")
            page.goto(base, wait_until="domcontentloaded")
            page.wait_for_selector(".auth-card", timeout=20000)
            page.fill('[name="account"]', "delme")
            page.fill('[name="password"]', pw)
            if not page.is_checked("[data-agree]"):
                page.check("[data-agree]")
            page.click("[data-submit]")
            page.wait_for_selector("#shell:not([hidden])", timeout=20000)

            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector('[data-card="danger"]', timeout=20000)
            card = page.locator('[data-card="danger"]')
            check("设置页有危险区域", card.count() == 1)
            check(
                "卡片上写明不可撤销",
                "不可撤销" in card.inner_text(),
                card.inner_text()[:80],
            )
            page.screenshot(path=str(OUT / "delete_account_card.png"))

            print("\n[B] 第一层确认：后果说明")
            page.click('[data-act="delete-account"]')
            page.wait_for_selector(".dialog", timeout=10000)
            body = page.locator(".dialog").inner_text()
            for piece in ["无法撤销", "分享", "长期记忆", "别人那份不受影响"]:
                check(f"后果说明包含「{piece}」", piece in body, body[:120])
            check("确认按钮是危险样式", page.locator('.dialog [data-act="ok"].btn--danger').count() == 1)
            page.screenshot(path=str(OUT / "delete_account_warning.png"))
            page.click('.dialog [data-act="ok"]')

            print("\n[C] 第二层：密码 + 邮箱验证")
            page.wait_for_selector(".dialog [data-field]", timeout=10000)
            fields = page.locator(".dialog [data-field]").count()
            check("要求密码与邮箱两项", fields == 2, str(fields))
            # 先填错邮箱：行内要拦住
            page.fill('.dialog [data-field="password"] input', pw)
            page.fill('.dialog [data-field="email"] input', "wrong@example.com")
            page.click('.dialog [data-act="ok"]')
            page.wait_for_timeout(400)
            err_text = page.locator('.dialog [data-field="email"]').inner_text()
            check("邮箱不一致会当场提示", "不一致" in err_text, err_text[:80])
            page.screenshot(path=str(OUT / "delete_account_form.png"))

            # 改对再提交
            page.fill('.dialog [data-field="email"] input', "delme@example.com")
            page.click('.dialog [data-act="ok"]')
            page.wait_for_url(f"{base}/", timeout=20000)
            page.wait_for_selector(".auth-card", timeout=20000)
            check("注销后回到登录页", True)
            page.screenshot(path=str(OUT / "delete_account_done.png"))

            print("\n[D] 数据库里确实没了")
            con = sqlite3.connect(str(db_path))
            row = con.execute("select id from users where id = ?", (member_id,)).fetchone()
            others = con.execute("select count(*) from users").fetchone()[0]
            logs = con.execute(
                "select detail from audit_logs where action = 'account_delete'"
            ).fetchall()
            con.close()
            check("用户已从库里删除", row is None, str(row))
            check("别的账号还在", others >= 1, str(others))
            check("记了注销审计", len(logs) >= 1 and "delme" in (logs[0][0] or ""), str(logs[:1]))

            print("\n[E] 注销后旧会话不可用")
            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            hidden_shell = page.evaluate("document.querySelector('#shell').hidden")
            relogin = page.evaluate("!document.querySelector('#auth-root').hidden")
            check("回到未登录状态", bool(hidden_shell) and bool(relogin), f"shell={hidden_shell} auth={relogin}")

            if errors:
                print("\nJS 错误：")
                for item in errors[:6]:
                    print("  " + item)
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
