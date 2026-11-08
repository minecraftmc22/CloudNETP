# -*- coding: utf-8 -*-
"""管理页「数据库」卡片 + 设置页「保存配置」按钮的 UI 冒烟。

用 dev.db 的副本起一个独立实例（不动真实库），Playwright 驱动本机 Chrome：
  1. 系统设置里能看到数据库卡片，主库探活、容灾副本、看门狗信息都在
  2. 「刷新配置」能跑通并提示
  3. 「重新连接 SQLite」切过去、标签变「SQLite 容灾」，再「重新连接 MySQL」切回来
  4. 设置 → 对话设置底部「保存配置」能保存并给出时间戳

    ./.venv/Scripts/python.exe scripts/admin_db_shot.py
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
    """用 SQLite 备份 API 复制现役库（连 WAL 里没落盘的内容一起带走）。

    dev.db 是早先单独跑的开发库；现在默认库是容灾副本 storage/db/fallback.db，
    两个都不在时按只读方式打开会直接报错，不会静默建一个空库。
    """
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
    from app.config import settings
    from app.security import create_token

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-dbui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    row = con.execute("select id, token_version from users where role = 1 order by id limit 1").fetchone()
    con.close()
    if not row:
        raise SystemExit("库里没有管理员，先手动建一个再来跑这个脚本")
    cookie = f"cloudnetp_session={create_token(row[0], row[1])}"

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
            "SECRET_KEY": settings.SECRET_KEY,  # 双进程用同一个密钥，Cookie 才认得
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
            ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
            ctx.add_cookies([{"name": "cloudnetp_session", "value": cookie.split("=", 1)[1], "url": base}])
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            # 看板娘是 fixed 浮层，会挡住右侧按钮，截图时先藏起来
            page.add_init_script(
                "window.addEventListener('DOMContentLoaded', () => {"
                "  const s = document.createElement('style');"
                "  s.textContent = '#mascot{display:none!important}';"
                "  document.head.appendChild(s);"
                "});"
            )

            print("\n[A] 管理后台 → 系统设置 → 数据库卡片")
            page.goto(f"{base}/#/admin", wait_until="domcontentloaded")
            page.wait_for_selector('[data-tab="settings"]', timeout=15000)
            page.click('[data-tab="settings"]')
            page.wait_for_selector('[data-card="db"]', timeout=15000)
            card = page.locator('[data-card="db"]')
            text = card.inner_text()
            check("卡片渲染出来了", "当前服务库" in text)
            check("主库连接串已脱敏", "****" in text or "sqlite:///" in text, text.splitlines()[3][:60])
            check("容灾副本路径可见", "fallback.db" in text)
            check("探活状态可见", "可达" in text or "不可达" in text)
            check("三个动作按钮都在",
                  card.locator('[data-db-act="refresh"]').count() == 1
                  and card.locator('[data-db-act="primary"]').count() == 1
                  and card.locator('[data-db-act="fallback"]').count() == 1)

            page.screenshot(path=str(OUT / "admin_db.png"), full_page=True)
            card.screenshot(path=str(OUT / "admin_db_card.png"))

            print("\n[B] 刷新配置")
            card.locator('[data-db-act="refresh"]').click()
            page.wait_for_selector(".toast", timeout=15000)
            toast = page.locator(".toast").last.inner_text()
            check("刷新配置有提示", "配置" in toast or "刷新" in toast, toast)
            page.wait_for_timeout(600)
            check("刷新后卡片还在", page.locator('[data-card="db"]').count() == 1)

            print("\n[C] 重新连接 SQLite")
            page.click('[data-db-act="fallback"]')
            page.wait_for_selector(".dialog [data-act='ok']", timeout=8000)
            page.screenshot(path=str(OUT / "admin_db_confirm.png"))
            page.click(".dialog [data-act='ok']")
            page.wait_for_timeout(1200)
            text = page.locator('[data-card="db"]').inner_text()
            check("标签切到 SQLite 容灾", "SQLite 容灾" in text, text.splitlines()[1][:80])
            check("提示已暂停自动回切", "已暂停自动回切" in text)
            page.screenshot(path=str(OUT / "admin_db_fallback.png"), full_page=True)

            print("\n[D] 重新连接 MySQL")
            page.click('[data-db-act="primary"]')
            page.wait_for_selector(".dialog [data-act='ok']", timeout=8000)
            page.click(".dialog [data-act='ok']")
            page.wait_for_timeout(1500)
            text = page.locator('[data-card="db"]').inner_text()
            check("切回主库", "SQLite 容灾" not in text, text.splitlines()[1][:80])
            check("回切原因有记录", "手动重连" in text or "没有切换过" in text)

            print("\n[E] 设置 → 对话设置 → 保存配置")
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector("[data-ai-save]", timeout=15000)
            page.locator("[data-ai-save]").scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            # 只改 DOM 不触发 change：只有按钮自己收集才能把这个值提交上去
            page.evaluate("() => { document.querySelector('[data-ai-rounds-input]').value = '6'; }")
            page.click("[data-ai-save]")
            page.wait_for_timeout(1200)
            status = page.locator("[data-ai-save-status]").inner_text()
            check("保存后显示时间戳", "已保存" in status, status)
            toast = page.locator(".toast").last.inner_text()
            check("保存有成功提示", "已保存" in toast or "配置" in toast, toast)

            cfg = page.evaluate(
                "async () => (await fetch('/api/ai/config', { credentials: 'same-origin' })).json()"
            )
            check("按钮真的把页面上的值提交上去了", cfg.get("config", {}).get("maxRounds") == 6, str(cfg.get("config", {}).get("maxRounds")))

            # 按钮必须贴右且不溢出卡片（之前设置页踩过宽度压不回去的坑）
            geo = page.evaluate(
                """() => {
                    const body = document.querySelector('[data-ai-body]');
                    const btn = document.querySelector('[data-ai-save]');
                    const b = body.getBoundingClientRect();
                    const r = btn.getBoundingClientRect();
                    return { gap: Math.round(b.right - r.right), overflow: body.scrollWidth - body.clientWidth };
                }"""
            )
            check("保存按钮贴右侧内边距", 8 <= geo["gap"] <= 24, str(geo))
            check("卡片没有横向溢出", geo["overflow"] <= 1, str(geo))
            page.locator(".setting-row.ai-save").screenshot(path=str(OUT / "settings_ai_save_row.png"))
            page.locator("[data-ai-body]").screenshot(path=str(OUT / "settings_ai_save.png"))

            check("没有 JS 报错", not errors, "; ".join(errors[:3]))

            print("\n[F] 手机宽度下的数据库卡片")
            mobile = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
            mobile.add_cookies([{"name": "cloudnetp_session", "value": cookie.split("=", 1)[1], "url": base}])
            mpage = mobile.new_page()
            mpage.goto(f"{base}/#/admin", wait_until="domcontentloaded")
            mpage.wait_for_selector('[data-tab="settings"]', timeout=15000)
            mpage.click('[data-tab="settings"]')
            mpage.wait_for_selector('[data-card="db"]', timeout=15000)
            mpage.wait_for_timeout(400)
            mcard = mpage.locator('[data-card="db"]')
            mcard.screenshot(path=str(OUT / "admin_db_mobile.png"))
            mgeo = mpage.evaluate(
                """() => {
                    const card = document.querySelector('[data-card="db"]');
                    const buttons = [...card.querySelectorAll('[data-db-act]')];
                    return {
                        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                        narrow: buttons.some((b) => b.getBoundingClientRect().width < 60),
                    };
                }"""
            )
            check("手机端没有横向滚动", mgeo["overflow"] <= 1, str(mgeo))
            check("按钮没有被压没", not mgeo["narrow"], str(mgeo))
            mobile.close()

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

    print(f"\n截图目录: {OUT}")
    print(f"结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
