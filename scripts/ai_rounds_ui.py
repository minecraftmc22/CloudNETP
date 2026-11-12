# -*- coding: utf-8 -*-
"""设置页「工具轮次上限 → 无限制」的 UI 冒烟。

用库副本起独立实例，Playwright 驱动本机 Chrome，真实走一遍保存链路：

  A 「无限制」快捷项存在，点它 → 库里 maxRounds 存成 0
  B 刷新页面 → 仍然是高亮，自定义框为空（读回没错位）
  C 点「保存配置」整块提交 → 0 不会被 collectAll 的默认 8 覆盖
  D 切回「8 轮」 / 自定义 30 → 各自正确落库
  E 375px 窄屏：6 个 chip 不把设置页撑出横向滚动

    ./.venv/Scripts/python.exe scripts/ai_rounds_ui.py
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
sys.path.insert(0, str(ROOT))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)

passed = 0
failed = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {label} {extra}")
    else:
        failed += 1
        print(f"  FAIL {label} {extra}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def snapshot_db(dst: Path) -> None:
    src_path = ROOT / "storage" / "db" / "fallback.db"
    if not src_path.is_file():
        src_path = ROOT / "dev.db"
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


def main() -> int:
    from app.config import settings
    from app.security import create_token

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-ai-rounds-ui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    plain = con.execute("select id, token_version from users where role != 1 order by id limit 1").fetchone()
    if not plain:
        con.execute(
            "insert into users (username, password_hash, display_name, quota_bytes, used_bytes, role,"
            " token_version, created_at) values (?,?,?,?,?,?,?,datetime('now'))",
            ("roundsui", "x", "RoundsUI", 10 * 1024 ** 3, 0, 0, 0),
        )
        con.commit()
        plain = con.execute("select id, token_version from users where username = 'roundsui'").fetchone()
    # 库里可能留着线上实测过的默认接口（优先级高于 .env），清掉免得表单被收起
    con.execute("delete from system_settings where k = 'ai_default_config'")
    con.commit()
    con.close()
    cookie = create_token(plain[0], plain[1])

    def saved_rounds():
        conn = sqlite3.connect(str(db_copy))
        try:
            row = conn.execute("select ai_config from user_settings where user_id = ?", (plain[0],)).fetchone()
            return json.loads(row[0] or "{}").get("maxRounds") if row else None
        finally:
            conn.close()

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
            "AI_ENABLED": "true",
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
            raise SystemExit("服务没起来，看日志：" + str(log_path))

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)

            ctx = browser.new_context(viewport={"width": 1440, "height": 1100}, device_scale_factor=2)
            ctx.add_cookies([{"name": "cloudnetp_session", "value": cookie, "url": base}])
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            page.add_init_script(
                "window.addEventListener('DOMContentLoaded', () => {"
                "  const s = document.createElement('style');"
                "  s.textContent = '#mascot{display:none!important}';"
                "  document.head.appendChild(s);"
                "});"
            )

            def open_settings():
                page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
                page.wait_for_selector("[data-ai-rounds]", state="attached", timeout=20000)

            print("\n[A] 「无限制」快捷项")
            open_settings()
            chips = page.locator("[data-ai-rounds] [data-ai-round]")
            check("快捷项一共 6 个（含无限制）", chips.count() == 6, str(chips.count()))
            unlimited = page.locator('[data-ai-round="0"]')
            check("「无限制」文案正确", unlimited.inner_text().strip() == "无限制", unlimited.inner_text())
            unlimited.click()
            page.wait_for_timeout(600)
            check("点无限制 → 库里存 0", saved_rounds() == 0, str(saved_rounds()))
            check("无限制高亮", unlimited.get_attribute("class").find("is-active") >= 0)
            check("自定义框留空", page.locator("[data-ai-rounds-input]").input_value() == "")

            print("\n[B] 刷新后读回")
            open_settings()
            check(
                "刷新后仍是无限制高亮",
                "is-active" in (page.locator('[data-ai-round="0"]').get_attribute("class") or ""),
            )
            check("刷新后自定义框仍为空", page.locator("[data-ai-rounds-input]").input_value() == "")

            print("\n[C] 显式「保存配置」不把 0 变回 8")
            page.locator("[data-ai-save]").click()
            page.wait_for_timeout(700)
            check("整块保存后仍是 0", saved_rounds() == 0, str(saved_rounds()))

            print("\n[D] 切回有限轮次 / 自定义")
            page.locator('[data-ai-round="8"]').click()
            page.wait_for_timeout(600)
            check("点 8 轮 → 库里存 8", saved_rounds() == 8, str(saved_rounds()))
            check("8 轮高亮", "is-active" in (page.locator('[data-ai-round="8"]').get_attribute("class") or ""))
            check("无限制取消高亮", "is-active" not in (page.locator('[data-ai-round="0"]').get_attribute("class") or ""))

            custom = page.locator("[data-ai-rounds-input]")
            custom.fill("30")
            custom.dispatch_event("change")
            page.wait_for_timeout(600)
            check("自定义 30 → 库里存 30", saved_rounds() == 30, str(saved_rounds()))
            check("自定义值保留在输入框", custom.input_value() == "30", custom.input_value())

            print("\n[E] 窄屏")
            mctx = browser.new_context(viewport={"width": 375, "height": 800}, device_scale_factor=2)
            mctx.add_cookies([{"name": "cloudnetp_session", "value": cookie, "url": base}])
            mpage = mctx.new_page()
            mpage.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            mpage.wait_for_selector("[data-ai-rounds]", state="attached", timeout=20000)
            overflow = mpage.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            check("375px 设置页无横向溢出", overflow <= 1, str(overflow))
            check("窄屏下无限制 chip 可见", mpage.locator('[data-ai-round="0"]').is_visible())
            mpage.locator("[data-ai-card]").screenshot(path=str(OUT / "ai_rounds_settings.png"))
            mctx.close()

            check("设置页没有 JS 报错", not errors, str(errors[:2]))
            ctx.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failed:
        print(f"失败 {failed} 项")
        return 1
    print(f"全部通过（{passed} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
