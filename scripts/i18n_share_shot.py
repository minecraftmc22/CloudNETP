# -*- coding: utf-8 -*-
"""分享页 `/s/…` 的 i18n 冒烟：英文界面里还残留哪些中文。

分享页是独立入口（share.html 不挂主应用的 store），所以单独跑一遍。
场景覆盖：提取码门槛 → 单文件 / 合集 / 文件夹 → 举报、转存弹窗（含未登录提示）。

    ./.venv/Scripts/python.exe scripts/i18n_share_shot.py [--lang en-US]

做法：拷一份库起独立实例（绝不碰真库），用接口建三种分享，再用不带 Cookie 的
浏览器上下文访问，收集 `.share-page` 里的文本与 title/placeholder/aria-label。
文件名、文件夹名属于用户数据，按名字精确剔除，不计入残留。
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

TEXT_JS = """() => {
    const out = [];
    const root = document.querySelector('.share-page') || document.body;
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
    print(log_path.read_text(encoding="utf-8", errors="replace")[-1200:])
    return False


def api(base: str, cookie: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method="POST" if data else "GET")
    req.add_header("Cookie", f"cloudnetp_session={cookie}")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def main() -> None:
    from app.config import settings
    from app.security import create_token

    lang = "en-US"
    if "--lang" in sys.argv:
        lang = sys.argv[sys.argv.index("--lang") + 1]

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-share-i18n-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    row = con.execute("select id, token_version from users where role = 1 order by id limit 1").fetchone()
    if not row:
        raise SystemExit("库里没有管理员")
    files = [
        r[0]
        for r in con.execute(
            "select id from nodes where user_id = ? and type = 'file' and deleted_at is null "
            "and (parent_id is null) order by size desc limit 3",
            (row[0],),
        )
    ]
    folders = [
        r[0]
        for r in con.execute(
            "select id from nodes where user_id = ? and type = 'folder' and deleted_at is null "
            "and (parent_id is null) order by id limit 1",
            (row[0],),
        )
    ]
    names = {
        r[0]
        for r in con.execute(
            "select name from nodes where user_id = ? and deleted_at is null", (row[0],)
        )
    }
    con.close()
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

    tokens: dict[str, str] = {}
    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来")
        if not files:
            raise SystemExit("库里没有可分享的文件")

        single = api(base, cookie, "/api/shares", {"nodeId": files[0]})
        tokens["file"] = single["share"]["token"]
        if len(files) >= 2:
            multi = api(base, cookie, "/api/shares", {"nodeIds": files[:2]})
            tokens["collection"] = multi["share"]["token"]
        gated = api(base, cookie, "/api/shares", {"nodeId": files[0], "extractCode": "K7M2"})
        tokens["gate"] = gated["share"]["token"]
        if folders:
            folder = api(base, cookie, "/api/shares", {"nodeId": folders[0]})
            tokens["folder"] = folder["share"]["token"]

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(
                    executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    headless=True,
                )
            errors: list[str] = []
            report: dict[str, list[str]] = {}

            def new_page(logged_in: bool):
                ctx = browser.new_context(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
                if logged_in:
                    ctx.add_cookies([{"name": "cloudnetp_session", "value": cookie, "url": base}])
                page = ctx.new_page()
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                page.add_init_script(
                    f"try {{ localStorage.setItem('cloudnetp.lang', '{lang}'); }} catch (e) {{}}"
                )
                return ctx, page

            def collect(page, name: str, shot: bool = True) -> None:
                page.wait_for_timeout(600)
                texts = page.evaluate(TEXT_JS)
                titles = [page.title()]
                leftover = [
                    t
                    for t in texts + titles
                    if CJK.search(t) and not any(n and n in t for n in names)
                ]
                report[name] = leftover
                if shot:
                    page.screenshot(path=str(OUT / f"i18n_share_{name}.png"))

            # ---------- 未登录：单文件 + 举报 + 需要登录 ----------
            ctx, page = new_page(False)
            page.goto(f"{base}/s/{tokens['file']}", wait_until="domcontentloaded")
            page.wait_for_selector(".share-card", timeout=15000)
            collect(page, "file")

            page.click("[data-report]")
            page.wait_for_selector(".share-modal", timeout=10000)
            collect(page, "report")

            page.click(".share-modal [data-cancel]")
            page.click("[data-save-drive]")
            page.wait_for_selector(".share-modal", timeout=10000)
            collect(page, "login-required")
            page.mouse.click(8, 8)  # infoModal 没有确认按钮，点遮罩关掉
            ctx.close()

            # ---------- 提取码 ----------
            ctx, page = new_page(False)
            page.goto(f"{base}/s/{tokens['gate']}", wait_until="domcontentloaded")
            page.wait_for_selector(".share-gate", timeout=15000)
            collect(page, "gate")
            page.click("[data-submit]")
            page.wait_for_timeout(300)
            collect(page, "gate-empty-error", shot=False)
            page.fill("[data-code]", "0000")
            page.click("[data-submit]")
            page.wait_for_timeout(800)
            collect(page, "gate-wrong-error", shot=False)
            page.fill("[data-code]", "K7M2")
            page.click("[data-submit]")
            page.wait_for_selector(".share-file__name", timeout=15000)
            collect(page, "gate-unlocked")
            ctx.close()

            # ---------- 合集 ----------
            if "collection" in tokens:
                ctx, page = new_page(False)
                page.goto(f"{base}/s/{tokens['collection']}", wait_until="domcontentloaded")
                page.wait_for_selector(".share-item", timeout=15000)
                collect(page, "collection")
                page.check("[data-pick-all]")
                page.wait_for_timeout(300)
                collect(page, "collection-picked")
                ctx.close()

            # ---------- 文件夹 ----------
            if "folder" in tokens:
                ctx, page = new_page(False)
                page.goto(f"{base}/s/{tokens['folder']}", wait_until="domcontentloaded")
                page.wait_for_selector(".share-folder__head", timeout=15000)
                collect(page, "folder")
                back = page.query_selector("[data-enter]")
                if back:
                    page.click("[data-enter]")
                    page.wait_for_timeout(900)
                    collect(page, "folder-inner")
                ctx.close()

            # ---------- 已登录：转存弹窗 ----------
            ctx, page = new_page(True)
            page.goto(f"{base}/s/{tokens['file']}", wait_until="domcontentloaded")
            page.wait_for_selector(".share-card", timeout=15000)
            page.click("[data-save-drive]")
            page.wait_for_selector(".share-modal", timeout=10000)
            collect(page, "save-modal")
            page.click(".share-modal [data-cancel]")
            ctx.close()

            print(f"\n语言: {lang}")
            total = 0
            for name, items in report.items():
                total += len(items)
                print(f"  [{name}] 残留中文 {len(items)} 条")
                for item in items:
                    print("    - " + item)
            # 未登录访客请求 /api/me 必然 401，属于预期噪声
            noisy = [e for e in errors if "401 (Unauthorized)" not in e]
            if noisy:
                print("\nJS 错误：")
                for e in noisy[:8]:
                    print("  " + e)
            else:
                print("\n没有 JS 报错")
            print(f"\n合计残留 {total} 条；截图目录: {OUT}")
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
