# -*- coding: utf-8 -*-
"""站内公告 + 看板娘滚轮缩放的 UI 冒烟。

用库副本起独立实例，Playwright 驱动本机 Chrome：
  1. 后台填公告 → 主界面标题栏下方立刻出现（不用刷新）
  2. 切视图、刷新都还在；点叉关掉后不再出现
  3. 改了公告内容会重新出现；清空则消失
  4. 登录页卡片里也能看到；长公告给「展开」
  5. 悬停看板娘滚轮改大小，带百分比提示、到边界不越界
  6. 设置里关掉后滚轮不再改大小，滑块会跟着滚轮走

    ./.venv/Scripts/python.exe scripts/announce_wheel_ui.py
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)
PASSWORD = "Test123456"

TEXT1 = "本周六 22:00 起例行维护，期间上传会中断"
TEXT2 = "维护已结束，谢谢等待"
LONG = "这是一条很长的公告，用来验证三行以上会被折叠起来。" + (
    "维护期间上传、下载、在线预览、分享链接与 WebDAV 都会短暂不可用，恢复后会自动重连，不需要重新配置。"
) * 7 + "如果你能看到这一句，说明展开功能是好的。"
assert len(LONG) <= 500, len(LONG)

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

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-ann-ui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    row = con.execute("select id, token_version from users where role = 1 order by id limit 1").fetchone()
    if not row:
        raise SystemExit("库里没有管理员，先手动建一个再来跑这个脚本")
    con.execute("update users set password_hash=? where id=?", (hash_password(PASSWORD), row[0]))
    con.commit()
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
            "DB_MIRROR_INTERVAL": "0",
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
            raise SystemExit("服务没起来，看日志：" + str(log_path))

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)

            ctx = browser.new_context(viewport={"width": 1440, "height": 980}, device_scale_factor=2)
            ctx.add_cookies([{"name": "cloudnetp_session", "value": cookie, "url": base}])
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on(
                "console",
                lambda msg: errors.append(msg.text)
                if msg.type == "error" and "Failed to load resource" not in msg.text
                else None,
            )

            def open_admin_site() -> None:
                page.goto(f"{base}/#/admin", wait_until="domcontentloaded")
                page.wait_for_selector('[data-tab="settings"]', timeout=20000)
                page.click('[data-tab="settings"]')
                page.wait_for_selector('[data-card="site"] [data-key="siteAnnouncement"]', timeout=20000)

            def save_site(text: str) -> None:
                box = page.locator('[data-card="site"] [data-key="siteAnnouncement"]')
                box.fill(text)
                page.click('[data-save="site"]')
                page.wait_for_selector(".toast", timeout=15000)
                page.wait_for_timeout(900)

            print("\n[A] 后台填公告：主界面立刻出现")
            open_admin_site()
            save_site(TEXT1)
            bar = page.locator("#announce")
            check("公告条出现了", bar.is_visible())
            check("文字就是后台填的", bar.inner_text().strip().startswith(TEXT1), bar.inner_text()[:40])
            geo = page.evaluate(
                """() => {
                    const bar = document.querySelector('#announce');
                    const tb = document.querySelector('.titlebar');
                    const body = document.querySelector('.app-body');
                    return {
                        barTop: bar.getBoundingClientRect().top,
                        barBottom: bar.getBoundingClientRect().bottom,
                        tbBottom: tb.getBoundingClientRect().bottom,
                        bodyTop: body.getBoundingClientRect().top,
                        close: !!bar.querySelector('[data-announce-close]'),
                    };
                }"""
            )
            check("在标题栏下面", geo["barTop"] >= geo["tbBottom"] - 1, str(geo))
            check("内容区被推到公告下面", geo["bodyTop"] >= geo["barBottom"] - 1, str(geo))
            check("有关闭按钮", geo["close"])
            page.screenshot(path=str(OUT / "announce_main.png"), full_page=False)

            print("\n[B] 切到别的地方也还在")
            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_timeout(900)
            check("我的文件页也有公告", page.locator("#announce").is_visible())
            page.goto(f"{base}/#/about", wait_until="domcontentloaded")
            page.wait_for_timeout(700)
            check("关于页也有公告", page.locator("#announce").is_visible())

            print("\n[C] 关掉之后不再打扰")
            page.click("[data-announce-close]")
            page.wait_for_timeout(300)
            check("关掉就收起了", page.locator("#announce").is_hidden())
            stored = page.evaluate("localStorage.getItem('cloudnetp.announce-dismissed')")
            check("本机记下了这条", bool(stored), str(stored))
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            check("刷新后仍然不显示", page.locator("#announce").is_hidden())

            print("\n[D] 改了公告内容会重新出现")
            open_admin_site()
            save_site(TEXT2)
            check("新公告又出现了", page.locator("#announce").is_visible())
            check("显示的是新内容", TEXT2 in page.locator("#announce").inner_text())

            print("\n[E] 登录页也能看到")
            guest = browser.new_context(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
            gpage = guest.new_page()
            gerrors: list[str] = []
            gpage.on("pageerror", lambda exc: gerrors.append(str(exc)))
            gpage.goto(base, wait_until="domcontentloaded")
            gpage.wait_for_selector(".auth-card [data-auth-announce]", timeout=20000)
            card = gpage.locator("[data-auth-announce]")
            check("登录卡片里有公告", card.is_visible())
            check("内容和后台一致", TEXT2 in card.inner_text(), card.inner_text()[:40])
            foot = gpage.evaluate(
                "() => document.querySelector('.auth-foot')?.innerText || ''"
            )
            check("页脚不再重复一遍", TEXT2 not in foot, foot.replace("\n", " / "))
            gpage.screenshot(path=str(OUT / "announce_login.png"), full_page=False)

            print("\n[F] 长公告给「展开」")
            open_admin_site()
            save_site(LONG)
            more = page.locator("#announce [data-announce-more]")
            page.wait_for_timeout(400)
            check("长公告出现展开按钮", more.is_visible())
            before = page.evaluate(
                "() => document.querySelector('#announce .announce__text').clientHeight"
            )
            more.click()
            page.wait_for_timeout(350)
            after = page.evaluate(
                "() => document.querySelector('#announce .announce__text').clientHeight"
            )
            check("展开后变高", after > before, f"{before} -> {after}")
            check("按钮变成收起", more.inner_text().strip() == "收起", more.inner_text())
            check("能看全最后一句", "展开功能是好的" in page.locator("#announce").inner_text())
            more.click()
            page.wait_for_timeout(300)
            check(
                "再点一次收回去",
                page.evaluate("() => document.querySelector('#announce .announce__text').clientHeight") <= before + 2,
            )

            print("\n[F2] 375px 窄屏：公告条不溢出")
            page.set_viewport_size({"width": 375, "height": 760})
            page.wait_for_timeout(500)
            metrics = page.evaluate(
                """() => {
                    const bar = document.querySelector('#announce');
                    const close = bar.querySelector('[data-announce-close]');
                    const box = close.getBoundingClientRect();
                    return {
                        over: bar.scrollWidth - bar.clientWidth,
                        left: box.left,
                        right: box.right,
                        vw: window.innerWidth,
                        h: Math.round(bar.getBoundingClientRect().height),
                    };
                }"""
            )
            check("公告条没有横向溢出", metrics["over"] <= 1, str(metrics))
            check(
                "关闭按钮在屏幕内",
                metrics["right"] <= metrics["vw"] + 1 and metrics["left"] >= 0,
                str(metrics),
            )
            page.screenshot(path=str(OUT / "announce_mobile.png"), full_page=False)
            page.set_viewport_size({"width": 1440, "height": 980})
            page.wait_for_timeout(400)

            print("\n[G] 清空公告就消失")
            open_admin_site()
            save_site("")
            check("空公告不占位", page.locator("#announce").is_hidden())

            print("\n[H] 悬停看板娘滚轮改大小")
            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_selector('[data-mascot-act="chat"]', timeout=20000)
            page.wait_for_function(
                "document.getElementById('mascot-actions')?.dataset.bound === '1'", timeout=20000
            )
            page.evaluate("localStorage.removeItem('mascot-size'); localStorage.removeItem('mascot-wheel');")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1600)
            box = page.locator("#mascot").bounding_box()
            check("看板娘在页面上", bool(box))
            cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.5

            def size_now() -> int:
                return int(page.evaluate("localStorage.getItem('mascot-size') || '100'"))

            page.mouse.move(cx, cy)
            page.mouse.wheel(0, -120)
            page.wait_for_timeout(320)
            bigger = size_now()
            check("往上滚变大", bigger > 100, f"100 -> {bigger}")
            scale = page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--mascot-scale').trim()"
            )
            check("CSS 变量跟着变", abs(float(scale) - bigger / 100) < 0.011, f"{scale} vs {bigger}")
            tag = page.evaluate(
                """() => {
                    const el = document.querySelector('.mascot__size');
                    return el ? { text: el.textContent, on: el.classList.contains('is-on') } : null;
                }"""
            )
            check("弹出百分比提示", bool(tag and tag["on"] and tag["text"] == f"{bigger}%"), str(tag))
            tagbox = page.evaluate(
                """() => {
                    const el = document.querySelector('.mascot__size');
                    const r = el.getBoundingClientRect();
                    return { l: r.left, t: r.top, r: r.right, b: r.bottom, vw: innerWidth, vh: innerHeight };
                }"""
            )
            check(
                "提示完整落在屏幕里",
                tagbox["l"] >= 0 and tagbox["t"] >= 0
                and tagbox["r"] <= tagbox["vw"] and tagbox["b"] <= tagbox["vh"],
                str(tagbox),
            )
            page.screenshot(path=str(OUT / "mascot_wheel.png"), full_page=False)

            page.mouse.wheel(0, 120)
            page.wait_for_timeout(320)
            check("往下滚变小", size_now() < bigger, f"{bigger} -> {size_now()}")

            for _ in range(40):
                page.mouse.wheel(0, 120)
            page.wait_for_timeout(500)
            check("最小到 60 就不动了", size_now() == 60, str(size_now()))
            for _ in range(40):
                page.mouse.wheel(0, -120)
            page.wait_for_timeout(500)
            check("最大到 180 就不动了", size_now() == 180, str(size_now()))

            print("\n[I] 设置里可以关掉")
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector("[data-mascot-wheel]", state="attached", timeout=20000)
            check("设置里有滚轮缩放开关", page.locator("[data-mascot-wheel]").is_checked())
            page.evaluate(
                """() => {
                    const box = document.querySelector('[data-mascot-wheel]');
                    box.checked = false;
                    box.dispatchEvent(new Event('change', { bubbles: true }));
                }"""
            )
            page.wait_for_timeout(400)
            check(
                "关掉后记到本机",
                page.evaluate("localStorage.getItem('mascot-wheel')") == "0",
                str(page.evaluate("localStorage.getItem('mascot-wheel')")),
            )
            box = page.locator("#mascot").bounding_box()
            cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.5
            before_off = size_now()
            page.mouse.move(cx, cy)
            for _ in range(3):
                page.mouse.wheel(0, 120)
            page.wait_for_timeout(400)
            check("关掉后滚轮不改大小", size_now() == before_off, f"{before_off} -> {size_now()}")

            print("\n[J] 重新打开：滑块跟着滚轮走")
            page.evaluate(
                """() => {
                    const box = document.querySelector('[data-mascot-wheel]');
                    box.checked = true;
                    box.dispatchEvent(new Event('change', { bubbles: true }));
                }"""
            )
            page.wait_for_timeout(400)
            slider = page.locator("[data-mascot-size]")
            before_val = int(slider.input_value())
            box = page.locator("#mascot").bounding_box()
            cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.5
            page.mouse.move(cx, cy)
            page.mouse.wheel(0, 120)
            page.wait_for_timeout(450)
            after_val = int(slider.input_value())
            check("滑块跟着变小", after_val < before_val, f"{before_val} -> {after_val}")
            check(
                "百分比文字同步",
                page.locator("[data-mascot-size-val]").inner_text().strip() == f"{after_val}%",
                page.locator("[data-mascot-size-val]").inner_text(),
            )

            check("全程没有 JS 报错", not errors, str(errors[:2]))
            check("登录页没有 JS 报错", not gerrors, str(gerrors[:2]))
            guest.close()
            ctx.close()
            browser.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            pass
        log_file.close()

    print(f"\n通过 {passed} 项，失败 {failed} 项")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
