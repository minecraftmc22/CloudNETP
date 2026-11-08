# -*- coding: utf-8 -*-
"""i18n 冒烟：用 dev.db 副本起独立实例，分别以中文 / 英文打开，抓图并统计「英文界面里还剩哪些中文」。

    ./.venv/Scripts/python.exe scripts/i18n_shot.py [--lang en-US]

只看界面骨架和主要视图的静态文案，用户数据（文件名等）不参与统计。
"""

from __future__ import annotations

import json
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
    # dev.db 是早先单独跑的开发库；现在默认库是容灾副本 storage/db/fallback.db
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
    print(log_path.read_text(encoding="utf-8", errors="replace")[-1200:])
    return False


def main() -> None:
    from app.config import settings
    from app.security import create_token

    lang = "en-US"
    if "--lang" in sys.argv:
        lang = sys.argv[sys.argv.index("--lang") + 1]

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-i18n-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    row = con.execute("select id, token_version from users where role = 1 order by id limit 1").fetchone()
    con.close()
    if not row:
        raise SystemExit("dev.db 里没有管理员")
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
            "DB_FALLBACK_ENABLED": "1",
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
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(
                    executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    headless=True,
                )
            ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
            ctx.add_cookies([{"name": "cloudnetp_session", "value": cookie, "url": base}])
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            # add_init_script 在文档脚本之前执行：语言必须在模块加载前写进 localStorage
            page.add_init_script(
                f"try {{ localStorage.setItem('cloudnetp.lang', '{lang}'); }} catch (e) {{}}"
                "window.addEventListener('DOMContentLoaded', () => {"
                "  const s = document.createElement('style');"
                "  s.textContent = '#mascot{display:none!important}';"
                "  document.head.appendChild(s);"
                "});"
            )

            report = {}
            for name, route, sel in [
                ("files", "#/files", ".view-head"),
                ("settings", "#/settings", ".settings"),
                ("shares", "#/shares", ".view-head"),
                ("about", "#/about", ".view-head"),
                ("trash", "#/trash", ".view-head"),
                ("feedback", "#/feedback", ".view-head"),
                ("admin", "#/admin", ".admin-tabs"),
            ]:
                page.goto(f"{base}/{route}", wait_until="domcontentloaded")
                page.wait_for_selector(sel, timeout=15000)
                page.wait_for_timeout(700)
                page.screenshot(path=str(OUT / f"i18n_{name}.png"), full_page=(name == "settings"))
                # 只统计界面骨架：导航 + 视图头部 + 设置卡片，不碰文件列表里的用户数据
                texts = page.evaluate(
                    """() => {
                        const out = [];
                        const pick = ['#nav', '.view-head', '.settings', '.empty', '.toolbar', '.titlebar',
                                      '.admin-tabs', '.settings-card', '.admin-facts', '.stat-card',
                                      '.admin-toolbar', '.admin-hint', '.admin-pager'];
                        document.querySelectorAll(pick.join(',')).forEach((root) => {
                            const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                            let n;
                            while ((n = walker.nextNode())) {
                                const t = (n.textContent || '').trim();
                                if (t) out.push(t);
                            }
                            root.querySelectorAll('[title],[placeholder],[aria-label]').forEach((el) => {
                                for (const a of ['title', 'placeholder', 'aria-label']) {
                                    const v = el.getAttribute(a);
                                    if (v && v.trim()) out.push(v.trim());
                                }
                            });
                        });
                        return [...new Set(out)];
                    }"""
                )
                leftover = [t for t in texts if CJK.search(t)]
                report[name] = leftover

            # 管理后台的「系统设置」页最长（一整页卡片 + 数据库卡），单独进去截一张
            page.goto(f"{base}/#/admin", wait_until="domcontentloaded")
            page.wait_for_selector(".admin-tabs", timeout=15000)
            page.click('[data-tab="settings"]')
            page.wait_for_selector('[data-card="db"]', timeout=15000)
            page.wait_for_timeout(800)
            page.screenshot(path=str(OUT / "i18n_admin_settings.png"), full_page=True)
            admin_texts = page.evaluate(
                """() => {
                    const out = [];
                    document.querySelectorAll('.admin-body').forEach((root) => {
                        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                        let n;
                        while ((n = walker.nextNode())) {
                            const t = (n.textContent || '').trim();
                            if (t) out.push(t);
                        }
                    });
                    return [...new Set(out)];
                }"""
            )
            report["admin-settings"] = [t for t in admin_texts if CJK.search(t)]

            # 其余四个页签（用户 / 举报 / 反馈 / 审计）只收文案，不截图
            for tab, label in [
                ("users", "admin-users"),
                ("reports", "admin-reports"),
                ("feedback", "admin-feedback"),
                ("logs", "admin-logs"),
            ]:
                page.click(f'[data-tab="{tab}"]')
                page.wait_for_timeout(900)
                texts = page.evaluate(
                    """() => {
                        const out = [];
                        document.querySelectorAll('.admin-body').forEach((root) => {
                            const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                            let n;
                            while ((n = walker.nextNode())) {
                                const t = (n.textContent || '').trim();
                                if (t) out.push(t);
                            }
                        });
                        return [...new Set(out)];
                    }"""
                )
                report[label] = [t for t in texts if CJK.search(t)]

            # 登录页（无 Cookie）
            guest = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
            gpage = guest.new_page()
            gpage.on("pageerror", lambda exc: errors.append(str(exc)))
            gpage.add_init_script(f"try {{ localStorage.setItem('cloudnetp.lang', '{lang}'); }} catch (e) {{}}")
            gpage.goto(base, wait_until="domcontentloaded")
            gpage.wait_for_selector(".auth-root .auth-card", timeout=15000)
            gpage.wait_for_timeout(600)
            gpage.screenshot(path=str(OUT / "i18n_auth.png"))
            gtexts = gpage.evaluate(
                """() => {
                    const out = [];
                    const root = document.querySelector('.auth-root');
                    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                    let n;
                    while ((n = walker.nextNode())) {
                        const t = (n.textContent || '').trim();
                        if (t) out.push(t);
                    }
                    root.querySelectorAll('[title],[placeholder],[aria-label]').forEach((el) => {
                        for (const a of ['title', 'placeholder', 'aria-label']) {
                            const v = el.getAttribute(a);
                            if (v && v.trim()) out.push(v.trim());
                        }
                    });
                    return [...new Set(out)];
                }"""
            )
            report["auth"] = [t for t in gtexts if CJK.search(t)]
            guest.close()

            # 语言切换的闭环：中文 -> 英文 -> 刷新仍然英文 -> 账号里存住 -> 切回中文
            check_ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            check_ctx.add_cookies([{"name": "cloudnetp_session", "value": cookie, "url": base}])
            cpage = check_ctx.new_page()
            cpage.add_init_script("try { localStorage.setItem('cloudnetp.lang', 'zh-CN'); } catch (e) {}")
            cpage.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            cpage.wait_for_selector('[data-lang="en-US"]', timeout=15000)
            cpage.click('[data-lang="en-US"]')
            cpage.wait_for_timeout(900)
            switched = cpage.evaluate("document.documentElement.lang")
            nav_en = cpage.evaluate("document.querySelector('#nav .nav-item__label').textContent")
            stored = cpage.evaluate(
                "async () => (await (await fetch('/api/me')).json()).user.settings.language"
            )
            cpage.reload(wait_until="domcontentloaded")
            cpage.wait_for_selector("[data-ai-save], .settings", timeout=15000)
            after_reload = cpage.evaluate("document.documentElement.lang")
            cpage.click('[data-lang="zh-CN"]')
            cpage.wait_for_timeout(900)
            back = cpage.evaluate("document.documentElement.lang")
            nav_zh = cpage.evaluate("document.querySelector('#nav .nav-item__label').textContent")
            print("\n语言切换闭环：")
            print(f"  点击 English 后 html lang = {switched}，导航 = {nav_en}")
            print(f"  账号里存的值 = {stored!r}")
            print(f"  刷新后 html lang = {after_reload}")
            print(f"  切回中文后 = {back}，导航 = {nav_zh}")
            check_ctx.close()

            print(f"\n语言: {lang}")
            for name, items in report.items():
                print(f"  [{name}] 残留中文 {len(items)} 条")
                for item in items:
                    print("    - " + item)
            if errors:
                print("\nJS 错误：")
                for e in errors[:8]:
                    print("  " + e)
            else:
                print("\n没有 JS 报错")
            print(f"\n截图目录: {OUT}")
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
