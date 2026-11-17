# -*- coding: utf-8 -*-
"""超管 UI 冒烟：超管/普通管理员在「用户」页能看到的操作不一样。

用 dev.db 的副本起独立实例（不动真实库），Playwright 驱动本机 Chrome：
  1. 超管能看到「超级管理员」徽章，菜单里有「设为 / 取消超级管理员」
  2. 超管自己那行不能取消自己的超管
  3. 普通管理员看到只读提示条，菜单里没有角色操作
  4. 截图留档

    ./.venv/Scripts/python.exe scripts/super_admin_ui.py
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
sys.path.insert(0, str(ROOT))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)

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


def wait_up(base: str, proc: subprocess.Popen, log_path: Path) -> bool:
    for _ in range(80):
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


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-superui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)

    stamp = str(int(time.time()))[-5:]
    admin_name = f"ui-admin-{stamp}"
    plain_name = f"ui-plain-{stamp}"
    con = sqlite3.connect(str(db_copy))
    cols = [row[1] for row in con.execute("PRAGMA table_info(users)")]
    if "is_super" not in cols:  # 老库还没补列时先补上，方便下面插数据
        con.execute("ALTER TABLE users ADD COLUMN is_super BOOLEAN NOT NULL DEFAULT 0")
    con.execute(
        "INSERT INTO users (username, password_hash, role, is_super, quota_bytes, used_bytes,"
        " banned, token_version, created_at) VALUES (?, ?, 1, 0, ?, 0, 0, 1, datetime('now'))",
        (admin_name, "x", 5 * 1024**3),
    )
    con.execute(
        "INSERT INTO users (username, password_hash, role, is_super, quota_bytes, used_bytes,"
        " banned, token_version, created_at) VALUES (?, ?, 0, 0, ?, 0, 0, 1, datetime('now'))",
        (plain_name, "x", 5 * 1024**3),
    )
    con.commit()
    rows = con.execute(
        "select id, username, role, is_super, token_version from users order by id"
    ).fetchall()
    con.close()
    print("库里的账号：", rows)
    root_row = next((row for row in rows if row[2] == 1 and row[3] == 1), None)
    admin_row = next((row for row in rows if row[1] == admin_name), None)
    plain_row = next((row for row in rows if row[1] == plain_name), None)
    if not root_row:
        # 没有超管时服务启动会自动把最早的管理员提成超管，这里只是提前提示
        print("注意：库里暂时没有超管，等启动后自动提升")

    from app.config import settings
    from app.security import create_token

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
            "DB_MIRROR_INTERVAL": "0",
            "DB_FALLBACK_ENABLED": "1",
            "SECRET_KEY": settings.SECRET_KEY,
        }
    )
    log_path = tmp / "server.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "main.py"],
        cwd=str(ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    from playwright.sync_api import sync_playwright

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来，看日志：" + str(log_path))

        con = sqlite3.connect(str(db_copy))
        rows = con.execute(
            "select id, username, role, is_super, token_version from users order by id"
        ).fetchall()
        con.close()
        root_row = next((row for row in rows if row[2] == 1 and row[3] == 1), None)
        admin_row = next((row for row in rows if row[1] == admin_name), None)
        plain_row = next((row for row in rows if row[1] == plain_name), None)
        check("启动后自动有了超管", root_row is not None, str(rows))

        def open_users(browser, row):
            ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
            ctx.add_cookies(
                [
                    {
                        "name": "cloudnetp_session",
                        "value": create_token(row[0], row[4]),
                        "url": base,
                    }
                ]
            )
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on(
                "console",
                lambda msg: errors.append(msg.text) if msg.type == "error" else None,
            )
            page.goto(f"{base}/#/admin", wait_until="domcontentloaded")
            page.wait_for_selector('[data-tab="users"]', state="attached", timeout=20000)
            page.click('[data-tab="users"]')
            page.wait_for_selector(".admin-row", timeout=20000)
            return ctx, page, errors

        def menu_texts(page, user_id: int) -> list[str]:
            page.click(f'.admin-row[data-id="{user_id}"] [data-menu]')
            page.wait_for_selector(".popover", timeout=5000)
            texts = [t.strip() for t in page.locator(".popover .menu-item span").all_inner_texts()]
            disabled = [
                t.strip()
                for t in page.locator(".popover .menu-item:disabled span").all_inner_texts()
            ]
            page.keyboard.press("Escape")
            return texts if not disabled else texts + ["[disabled] " + d for d in disabled]

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)

            print("\n[A] 超管视角")
            ctx, page, errors = open_users(browser, root_row)
            body = page.inner_text("[data-panel]")
            check("超管能看到自己的超管徽章", "超级管理员" in body, body[:120])
            check("超管没有只读提示条", page.locator(".admin-notice").count() == 0)
            check("库里其它账号也列出来了", plain_name in body, body[:200])

            texts = menu_texts(page, plain_row[0])
            check("超管能把普通用户设为超管", any("设为超级管理员" in t for t in texts), str(texts))
            check("超管能把普通用户设为管理员", any(t == "设为管理员" for t in texts), str(texts))

            texts = menu_texts(page, root_row[0])
            check("自己那行显示取消超管", any("取消超级管理员" in t for t in texts), str(texts))
            check(
                "但不能取消自己",
                any(t.startswith("[disabled] 取消超级管理员") for t in texts),
                str(texts),
            )
            check("页面没有 JS 报错", not errors, str(errors[:2]))
            page.screenshot(path=str(OUT / "super_admin_users.png"))
            ctx.close()

            print("\n[B] 普通管理员视角")
            ctx, page, errors = open_users(browser, admin_row)
            notice = page.locator(".admin-notice")
            check("管理员看到只读提示条", notice.count() == 1, str(notice.count()))
            if notice.count():
                check(
                    "提示条说清了谁在管角色",
                    "超级管理员" in notice.inner_text(),
                    notice.inner_text(),
                )
            body = page.inner_text("[data-panel]")
            check("列表里仍能看到超管徽章", "超级管理员" in body)

            texts = menu_texts(page, plain_row[0])
            check(
                "管理员没有角色操作",
                not any("设为超级管理员" in t or "设为管理员" in t or "取消管理员" in t for t in texts),
                str(texts),
            )
            check(
                "给出「由超管管理」的提示",
                any("成员角色由超级管理员管理" in t for t in texts),
                str(texts),
            )

            # 管理员自己那行：连自己的密码也不能从后台重置（走设置页）
            texts = menu_texts(page, admin_row[0])
            check(
                "管理员不能在这里重置管理员密码",
                any(t == "[disabled] 重置密码" for t in texts),
                str(texts),
            )

            # 对超管账号：重置密码 / 封禁 / 删除都不该给普通管理员用
            texts = menu_texts(page, root_row[0])
            for label in ("重置密码", "封禁账号", "删除用户"):
                check(
                    f"管理员动不了超管的「{label}」",
                    any(t == f"[disabled] {label}" for t in texts),
                    str(texts),
                )
            check("页面没有 JS 报错", not errors, str(errors[:2]))
            page.screenshot(path=str(OUT / "super_admin_readonly.png"))
            ctx.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAIL:
        print(f"失败 {FAIL} 项，通过 {PASS} 项")
        raise SystemExit(1)
    print(f"全部通过（{PASS} 项）")


if __name__ == "__main__":
    main()
