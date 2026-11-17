# -*- coding: utf-8 -*-
"""封禁申诉 UI 冒烟：登录被拒进申诉页、在线弹窗、后台审核通过。

临时库（不动 dev.db）+ 独立实例 + Playwright：

  1. 被封账号在登录页登录 → 直接进申诉页，能看到原因/封禁时间/解封时间
  2. 提交申诉 → 落库为 pending
  3. 后台「申诉审核」看到这条 → 通过并解封
  4. 申诉页刷新显示已恢复，原账号能正常登录
  5. 在线用户被封 → 立刻弹「你已被管理员封禁」，可去申诉 / 退出登录

    ./.venv/Scripts/python.exe scripts/ban_appeal_ui.py
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
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
    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-banui-")).resolve()
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
            username="banboss",
            password_hash=hash_password(pw),
            role=1,
            is_super=True,
            quota_bytes=5 * 1024**3,
            created_at=utcnow(),
        )
        carol = User(
            username="bancarol",
            password_hash=hash_password(pw),
            quota_bytes=5 * 1024**3,
            created_at=utcnow(),
        )
        bob = User(
            username="banbob",
            password_hash=hash_password(pw),
            quota_bytes=5 * 1024**3,
            created_at=utcnow(),
        )
        db.add_all([boss, carol, bob])
        db.commit()
        for u in (boss, carol, bob):
            db.refresh(u)
        boss_id, carol_id, bob_id = boss.id, carol.id, bob.id
        admin_cookie = create_token(boss.id, boss.token_version)

        # 先把 carol 封掉，模拟「下线后被封」：登录时才应该跳申诉页
        carol.banned = True
        carol.ban_reason = "上传违规文件（测试）"
        carol.banned_at = utcnow()
        carol.ban_until = utcnow() + timedelta(days=7)
        carol.banned_by = boss_id
        db.commit()

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
            "DB_FALLBACK_ENABLED": "0",
            "DB_PROBE_INTERVAL": "0",
            "DB_MIRROR_INTERVAL": "0",
            "SECRET_KEY": settings.SECRET_KEY,
        }
    )
    log_path = tmp / "server.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "main.py"], cwd=str(ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT
    )

    from playwright.sync_api import sync_playwright

    def db_row(sql: str, args: tuple = ()):
        con = sqlite3.connect(str(db_path))
        try:
            row = con.execute(sql, args).fetchone()
        finally:
            con.close()
        return row

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来，看日志：" + str(log_path))

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)

            # ---------------- 被封用户：登录 → 申诉页 ----------------
            user_ctx = browser.new_context(viewport={"width": 1280, "height": 1000}, device_scale_factor=2)
            page = user_ctx.new_page()
            errors: list[str] = []

            def on_console(msg) -> None:
                # 被封后 /api/me 的 403、未登录时的 401 是预期内的，浏览器都会记一条
                if msg.type == "error" and not msg.text.startswith("Failed to load resource"):
                    errors.append(msg.text)

            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", on_console)

            print("\n[A] 登录页登录被封账号")
            page.goto(f"{base}/", wait_until="domcontentloaded")
            page.wait_for_selector('[name="account"]', timeout=20000)
            page.fill('[name="account"]', "bancarol")
            page.fill('[name="password"]', pw)
            if not page.is_checked("[data-agree]"):
                page.check("[data-agree]")
            page.click("[data-submit]")
            page.wait_for_selector(".appeal-card", timeout=20000)
            check("跳到了申诉页", page.locator(".appeal-card").count() == 1)
            check("地址栏是 #/appeal", page.url.endswith("#/appeal"), page.url)
            body = page.locator(".appeal-card").inner_text()
            check("标题写着被封禁", "你已被管理员封禁" in body, body[:80])
            check("显示封禁原因", "上传违规文件（测试）" in body, body[:160])
            check("显示封禁时间", "封禁时间" in body)
            check("显示解封时间", "解封时间" in body)
            page.screenshot(path=str(OUT / "ban_appeal_page.png"))
            page.locator(".appeal-card").screenshot(path=str(OUT / "ban_appeal_card.png"))

            print("\n[B] 提交申诉")
            message = "这个文件是别人传给我的，不知道违规，请复核"
            page.fill("[data-msg]", message)
            count_text = page.locator("[data-count]").inner_text()
            check("字数统计跟着走", count_text.startswith(f"{len(message)} / 1000"), count_text)
            page.click('[data-act="submit"]')
            page.wait_for_selector(".appeal-history.is-pending", timeout=10000)
            check("提交后显示审核中", "审核中" in page.locator(".appeal-history").inner_text())
            row = db_row(
                "select status, message, ban_reason from ban_appeals where user_id = ? order by id desc",
                (carol_id,),
            )
            check("落库为 pending", row and row[0] == "pending", str(row))
            check("申诉内容正确", row and "请复核" in row[1], str(row))
            check("快照了封禁原因", row and row[2] == "上传违规文件（测试）", str(row))

            # ---------------- 管理后台：审核 ----------------
            print("\n[C] 后台申诉审核")
            admin_ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
            admin_ctx.add_cookies(
                [{"name": settings.COOKIE_NAME, "value": admin_cookie, "url": base}]
            )
            apage = admin_ctx.new_page()
            apage.on("pageerror", lambda exc: errors.append(str(exc)))
            apage.goto(f"{base}/#/admin", wait_until="domcontentloaded")
            apage.wait_for_selector("[data-tab='appeals']", timeout=20000)
            apage.click("[data-tab='appeals']")
            apage.wait_for_selector(".appeal-item", timeout=15000)
            card_text = apage.locator(".appeal-item").first.inner_text()
            check("后台看到申诉", "bancarol" in card_text, card_text[:120])
            check("后台看到申诉内容", "请复核" in card_text)
            check("后台看到封禁原因", "上传违规文件（测试）" in card_text)
            apage.screenshot(path=str(OUT / "ban_admin_appeals.png"))

            apage.click("[data-approve]")
            apage.wait_for_selector(".dialog", timeout=8000)
            check("通过前要确认", "通过申诉并解封" in apage.locator(".dialog").inner_text())
            apage.click('.dialog [data-act="ok"]')
            apage.wait_for_timeout(1200)
            row = db_row("select banned, ban_reason from users where id = ?", (carol_id,))
            check("账号已解封", row and int(row[0]) == 0 and row[1] is None, str(row))
            row = db_row("select status from ban_appeals where user_id = ? order by id desc", (carol_id,))
            check("申诉标记已通过", row and row[0] == "approved", str(row))

            print("\n[C2] 审计日志能看懂是什么操作")
            apage.click("[data-tab='logs']")
            apage.wait_for_selector(".log-list", timeout=15000)
            logs_text = apage.locator(".log-list").inner_text()
            check("申诉审核有中文动作名", "申诉通过" in logs_text, logs_text[:100])
            check("不再全是「其它操作」", logs_text.count("其它操作") == 0, logs_text[:160])

            print("\n[D] 申诉页刷新 → 已恢复")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector(".appeal-state.is-ok", timeout=15000)
            check("显示账号已恢复", "账号已恢复" in page.locator(".appeal-state").inner_text())
            page.click('[data-act="back"]')
            page.wait_for_selector('[name="account"]', timeout=15000)
            check("回到登录页", page.locator('[name="account"]').count() == 1)

            print("\n[E] 解封后能正常登录")
            page.fill('[name="account"]', "bancarol")
            page.fill('[name="password"]', pw)
            if not page.is_checked("[data-agree]"):
                page.check("[data-agree]")
            page.click("[data-submit]")
            page.wait_for_selector("#shell:not([hidden])", timeout=20000)
            check("进入网盘", page.locator("#shell").is_visible())
            check("没有停在申诉页", page.locator(".appeal-card").count() == 0)

            # ---------------- 在线用户被封：弹窗 ----------------
            print("\n[F] 在线用户被封 → 立刻弹窗")
            bob_ctx = browser.new_context(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
            bpage = bob_ctx.new_page()
            bpage.on("pageerror", lambda exc: errors.append(str(exc)))
            resp = bob_ctx.request.post(
                f"{base}/api/auth/login",
                data={"username": "banbob", "password": pw},
            )
            check("bob 登录成功", resp.status == 200, str(resp.status))
            bpage.goto(f"{base}/#/files", wait_until="domcontentloaded")
            bpage.wait_for_selector("#shell:not([hidden])", timeout=20000)

            # 管理员把 bob 封掉（走真实接口，能顺带验证后端）
            ban_resp = admin_ctx.request.patch(
                f"{base}/api/admin/users/{bob_id}",
                data={"banned": True, "banReason": "在线封禁测试", "banDays": 3},
            )
            check("管理员封禁成功", ban_resp.status == 200, f"{ban_resp.status} {ban_resp.text()[:80]}")

            # 触发一次可见性检查，立刻拉 /api/me（不用等 25 秒轮询）
            bpage.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            bpage.wait_for_selector(".ban-host.is-open .ban-card", timeout=15000)
            modal = bpage.locator(".ban-card").inner_text()
            check("弹出封禁提示", "你已被管理员封禁" in modal, modal[:80])
            check("提示里有原因", "在线封禁测试" in modal, modal[:200])
            check("提示里有解封时间", "解封时间" in modal)
            check("有去申诉按钮", bpage.locator('[data-ban="appeal"]').count() == 1)
            check("有退出登录按钮", bpage.locator('[data-ban="logout"]').count() == 1)
            bpage.screenshot(path=str(OUT / "ban_online_modal.png"))

            print("\n[G] 弹窗去申诉 / 退出登录")
            bpage.click('[data-ban="appeal"]')
            bpage.wait_for_selector(".appeal-card", timeout=15000)
            check("从弹窗进申诉页", "在线封禁测试" in bpage.locator(".appeal-card").inner_text())
            bpage.click('[data-act="logout"]')
            bpage.wait_for_selector('[name="account"]', timeout=15000)
            check("退出登录回到登录页", bpage.locator('[name="account"]').count() == 1)

            print("\n[H] 控制台")
            check("没有 JS 报错", not errors, str(errors[:3]))

            user_ctx.close()
            admin_ctx.close()
            bob_ctx.close()
            browser.close()

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    print()
    print(f"通过 {PASS} / {PASS + FAIL}，截图在 {OUT}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
