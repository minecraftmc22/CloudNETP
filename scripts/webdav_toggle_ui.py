# -*- coding: utf-8 -*-
"""WebDAV 开关的 UI 冒烟（用户自己的开关 + 管理员总开关）。

用库副本起独立实例，Playwright 驱动本机 Chrome：
  1. 设置 → WebDAV：默认开启，地址与凭据可见
  2. 用户关掉自己的开关：地址与凭据收起，/api/me 里的值变 false；再打开恢复
  3. 管理后台「站点」关掉总开关：用户的设置页换成「管理员已关闭 WebDAV」提示

    ./.venv/Scripts/python.exe scripts/webdav_toggle_ui.py
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
PASSWORD = "Test123456"

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


def main() -> None:
    from app.config import settings
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-dav-ui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    admin = con.execute("select id, token_version from users where role = 1 order by id limit 1").fetchone()
    plain = con.execute("select id, token_version from users where role != 1 order by id limit 1").fetchone()
    if not plain:
        con.execute(
            "insert into users (username, password_hash, display_name, quota_bytes, used_bytes, role,"
            " token_version, created_at) values (?,?,?,?,?,?,?,datetime('now'))",
            ("davui", "x", "DavUI", 10 * 1024 ** 3, 0, 0, 0),
        )
        con.commit()
        plain = con.execute("select id, token_version from users where username = 'davui'").fetchone()
    if not admin:
        raise SystemExit("库里没有管理员，先手动建一个再来跑这个脚本")
    hashed = hash_password(PASSWORD)
    for row in (admin, plain):
        con.execute("update users set password_hash=? where id=?", (hashed, row[0]))
    con.commit()
    con.close()

    admin_cookie = create_token(admin[0], admin[1])
    plain_cookie = create_token(plain[0], plain[1])

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
            "DB_FALLBACK_ENABLED": "0",
            "SECRET_KEY": settings.SECRET_KEY,
            "WEBDAV_ENABLED": "true",
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

            def make_page(cookie: str):
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
                return ctx, page, errors

            print("\n[A] 默认开启")
            ctx, page, errors = make_page(plain_cookie)
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector('[data-toggle="webdavEnabled"]', state="attached", timeout=20000)
            check("开关存在且默认打开", page.locator('[data-toggle="webdavEnabled"]').is_checked())
            check("地址区可见", page.locator("[data-dav-url]").is_visible())
            url_text = page.locator("[data-dav-url]").inner_text()
            check("挂载地址是 /dav/", url_text.endswith("/dav/"), url_text)
            page.locator('[data-card="site"]').count()  # no-op，保持页面稳定

            print("\n[B] 用户关掉自己的 WebDAV")
            page.evaluate(
                """() => {
                    const box = document.querySelector('[data-toggle="webdavEnabled"]');
                    box.checked = false;
                    box.dispatchEvent(new Event("change", { bubbles: true }));
                }"""
            )
            page.wait_for_timeout(1200)
            check("地址与凭据收起", page.locator("[data-dav-body]").is_hidden())
            data = page.evaluate(
                "fetch('/api/me', {credentials:'same-origin'}).then(r => r.json())"
            )
            check(
                "/api/me 里是 false",
                ((data.get("user") or {}).get("settings") or {}).get("webdavEnabled") is False,
                str(data.get("user", {}).get("settings", {}).get("webdavEnabled")),
            )
            page.screenshot(path=str(OUT / "webdav_self_off.png"), full_page=False)

            print("\n[C] 用户重新打开")
            page.evaluate(
                """() => {
                    const box = document.querySelector('[data-toggle="webdavEnabled"]');
                    box.checked = true;
                    box.dispatchEvent(new Event("change", { bubbles: true }));
                }"""
            )
            page.wait_for_timeout(1200)
            check("地址与凭据恢复", page.locator("[data-dav-body]").is_visible())
            check("设置页没有 JS 报错", not errors, str(errors[:2]))
            ctx.close()

            print("\n[D] 管理员关掉总开关")
            actx, apage, aerrors = make_page(admin_cookie)
            apage.goto(f"{base}/#/admin", wait_until="domcontentloaded")
            apage.wait_for_selector('[data-tab="settings"]', timeout=20000)
            apage.click('[data-tab="settings"]')
            apage.wait_for_selector('[data-card="site"]', timeout=20000)
            site = apage.locator('[data-card="site"]')
            check("后台有 WebDAV 开关", site.locator('[data-key="webdavEnabled"]').count() == 1)
            check("后台开关默认打开", site.locator('[data-key="webdavEnabled"]').is_checked())
            # 开关的原生 input 被样式隐藏（0 尺寸），直接改状态即可（保存时读的是 .checked）
            apage.evaluate(
                """() => {
                    const box = document.querySelector('[data-card="site"] [data-key="webdavEnabled"]');
                    box.checked = false;
                }"""
            )
            apage.click('[data-save="site"]')
            apage.wait_for_timeout(1500)
            check("保存后有提示", apage.locator(".toast").count() > 0)
            check("后台没有 JS 报错", not aerrors, str(aerrors[:2]))
            site.screenshot(path=str(OUT / "webdav_admin_switch.png"))
            actx.close()

            print("\n[E] 用户端看到「管理员已关闭」")
            ctx2, page2, errors2 = make_page(plain_cookie)
            page2.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page2.wait_for_selector("[data-ai-card]", timeout=20000)
            page2.wait_for_timeout(600)
            text = page2.locator(".settings").inner_text()
            check("提示管理员已关闭", "管理员已关闭 WebDAV" in text, text[-200:])
            check("不再渲染开关", page2.locator('[data-toggle="webdavEnabled"]').count() == 0)
            check("关闭态没有 JS 报错", not errors2, str(errors2[:2]))
            page2.screenshot(path=str(OUT / "webdav_admin_off.png"), full_page=False)
            ctx2.close()
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
    if failed:
        print(f"失败 {failed} 项，通过 {passed} 项")
        raise SystemExit(1)
    print(f"全部通过（{passed} 项）")


if __name__ == "__main__":
    main()
