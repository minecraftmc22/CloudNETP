# -*- coding: utf-8 -*-
"""快传 UI 冒烟：应用内三个标签页 + 两个公开页。

用库副本起独立实例，Playwright 驱动本机 Chrome：

  [A] 侧栏有「快传」入口，点进去是三个标签页
  [B] 扫码直传：生成后出现二维码 SVG 与可复制链接
  [C] 取件码：选文件 → 生成 6 位码 → 列表里能看到这个码
  [D] 公开取件页：输入码后列出文件，下载链接可用
  [E] 扫码直传页（390px 宽）：选文件真的传上去，文件出现在目标目录
  [F] 390px 窄屏：标签页与卡片不横向溢出

    ./.venv/Scripts/python.exe scripts/quick_ui.py
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
import urllib.error
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


def http(base: str, method: str, path: str, body=None, cookie: str = ""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Origin", base)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except Exception:
            return exc.code, {"raw": raw[:200].decode("utf-8", "replace")}


def main() -> int:
    from app.config import settings
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-quick-ui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)

    con = sqlite3.connect(str(db_copy))
    try:
        con.execute("delete from quick_transfers")
    except sqlite3.OperationalError:
        pass
    admin = con.execute(
        "select id, username, token_version from users where role = 1 order by id limit 1"
    ).fetchone()
    if not admin:
        raise SystemExit("库里没有管理员，先手动建一个再来跑这个脚本")
    con.execute("update users set password_hash=? where id=?", (hash_password(PASSWORD), admin[0]))
    con.commit()
    con.close()

    session_token = create_token(admin[0], admin[2])
    cookie = f"cloudnetp_session={session_token}"

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

        # 准备：一个目标目录 + 一个可交付的文件
        st, d = http(base, "POST", "/api/nodes/folder", {"name": "快传接收"}, cookie)
        assert st in (200, 201), (st, d)
        folder_id = d["node"]["id"]
        payload = b"quick-ui-payload" * 50
        st, init = http(
            base,
            "POST",
            "/api/uploads/init",
            {"name": "取件样本.txt", "size": len(payload), "parentId": folder_id},
            cookie,
        )
        assert st == 200, (st, init)
        sid = init["sessionId"]
        req = urllib.request.Request(
            f"{base}/api/uploads/{sid}/0", data=payload, method="PUT"
        )
        req.add_header("Cookie", cookie)
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("Origin", base)
        with urllib.request.urlopen(req, timeout=30) as resp:
            assert resp.status == 200
        st, done = http(base, "POST", f"/api/uploads/{sid}/complete", None, cookie)
        assert st == 200, (st, done)
        sample_id = done["node"]["id"]

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)

            def make_page(cookie_value: str = "", width: int = 1440, height: int = 1000):
                ctx = browser.new_context(
                    viewport={"width": width, "height": height}, device_scale_factor=2
                )
                if cookie_value:
                    ctx.add_cookies([{"name": "cloudnetp_session", "value": cookie_value, "url": base}])
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.on(
                    "console",
                    lambda msg: errors.append(msg.text)
                    if msg.type == "error" and "Failed to load resource" not in msg.text
                    else None,
                )
                return ctx, page, errors

            print("\n[A] 侧栏入口与三个标签页")
            ctx, page, errors = make_page(session_token)
            page.goto(base + "/#/quick", wait_until="domcontentloaded")
            page.wait_for_selector(".quick-tabs .quick-tab", timeout=20000)
            nav_item = page.locator('.nav-item[data-route="quick"]')
            check("侧栏有快传", nav_item.count() == 1, str(nav_item.count()))
            check("导航高亮", "is-active" in (nav_item.get_attribute("class") or ""))
            tabs = page.locator(".quick-tab").all_inner_texts()
            check("三个标签页", len(tabs) == 3, str(tabs))
            check("标签顺序", [x.strip() for x in tabs] == ["扫码直传", "发给用户", "取件码"], str(tabs))
            check("标题是快传", page.locator(".view-title span").first.inner_text().strip() == "快传")

            print("\n[B] 扫码直传：生成二维码")
            page.locator('[data-act="new-drop"]').click()
            page.wait_for_selector(".quick-qr svg", timeout=20000)
            check("出现二维码", page.locator(".quick-qr svg").count() >= 1)
            url_value = page.locator(".quick-link input").first.input_value()
            check("链接指向 /drop/", "/drop/" in url_value, url_value[:60])
            check("列表里有一条会话", page.locator(".quick-list .quick-row").count() >= 1)
            drop_url = url_value.replace(base, "") if url_value.startswith(base) else url_value
            page.screenshot(path=str(OUT / "quick_drop.png"), full_page=False)

            print("\n[C] 取件码：选文件并生成")
            page.locator('.quick-tab[data-tab="pickup"]').click()
            page.wait_for_selector('[data-act="pick"]', timeout=10000)
            page.locator('[data-act="pick"]').click()
            page.wait_for_selector(".picker__row", timeout=10000)
            # 只列文件：进「快传接收」目录选那个样本文件
            page.locator(".picker__row", has_text="快传接收").locator("[data-enter]").first.click()
            page.wait_for_selector('.picker__row:has-text("取件样本.txt")', timeout=10000)
            page.locator('.picker__row:has-text("取件样本.txt")').click()
            page.locator('.picker__foot [data-act="ok"]').click()
            page.wait_for_selector(".quick-chip", timeout=10000)
            check("选中文件变成 chip", page.locator(".quick-chip").count() == 1)
            page.locator('[data-act="new"]').click()
            page.wait_for_selector("[data-code]", timeout=15000)
            code = page.locator("[data-code]").inner_text().strip()
            check("取件码 6 位", len(code) == 6 and code.isalnum(), code)
            check("列表里出现这个码", page.locator(".quick-list", has_text=code).count() >= 1)
            page.screenshot(path=str(OUT / "quick_pickup.png"), full_page=False)

            print("\n[D] 公开取件页")
            ctx2, page2, errors2 = make_page(width=390, height=844)
            page2.goto(f"{base}/pickup?code={code}", wait_until="domcontentloaded")
            page2.wait_for_selector(".quick-item", timeout=20000)
            check("列出文件", page2.locator(".quick-item").count() == 1)
            check("显示文件名", "取件样本.txt" in page2.locator(".quick-item").first.inner_text())
            href = page2.locator('.quick-item a[href*="/download"]').first.get_attribute("href") or ""
            with urllib.request.urlopen(base + href, timeout=20) as resp:
                body = resp.read()
            check("下载链接可用", resp.status == 200 and body == payload, f"{resp.status} {len(body)}")
            page2.screenshot(path=str(OUT / "quick_pickup_public.png"), full_page=False)

            print("\n[E] 扫码直传页：真的传一个文件")
            local = tmp / "手机照片.txt"
            local.write_bytes(b"from-phone" * 300)
            ctx3, page3, errors3 = make_page(width=390, height=844)
            page3.goto(base + drop_url, wait_until="domcontentloaded")
            page3.wait_for_selector(".quick-drop", timeout=20000)
            title = page3.locator(".quick-pub__title").inner_text()
            check("显示目标目录", "快传接收" in title or "我的文件" in title, title)
            page3.set_input_files("[data-input]", str(local))
            page3.wait_for_selector(".quick-item__name", timeout=30000)
            check("页面显示已传文件", "手机照片.txt" in page3.locator("[data-done]").inner_text())
            page3.screenshot(path=str(OUT / "quick_drop_public.png"), full_page=False)

            # 这一步用的是默认目标（根目录），所以去根目录里找
            st, listing = http(base, "GET", "/api/nodes", None, cookie)
            names = [item["name"] for item in listing.get("items") or []]
            check("文件真的落进网盘", "手机照片.txt" in names, str(names))

            print("\n[F] 390px 窄屏不溢出")
            ctx4, page4, errors4 = make_page(session_token, width=390, height=844)
            page4.goto(base + "/#/quick", wait_until="domcontentloaded")
            page4.wait_for_selector(".quick-tabs", timeout=20000)
            metrics = page4.evaluate(
                """() => ({
                    doc: document.documentElement.scrollWidth,
                    win: innerWidth,
                    tabs: document.querySelector('.quick-tabs').scrollWidth,
                    tabsBox: document.querySelector('.quick-tabs').clientWidth,
                })"""
            )
            check("页面不横向溢出", metrics["doc"] <= metrics["win"] + 1, str(metrics))
            check("标签栏自身可滚动不撑破", metrics["tabs"] >= metrics["tabsBox"], str(metrics))
            page4.screenshot(path=str(OUT / "quick_mobile.png"), full_page=False)

            print("\n[G] 控制台无报错")
            for name, errs in (("应用内", errors), ("取件页", errors2), ("直传页", errors3), ("窄屏", errors4)):
                check(f"{name}无 JS 异常", not errs, "; ".join(errs[:2]))

            browser.close()

        print(f"\n结果：{passed} 通过，{failed} 失败")
        return 0 if failed == 0 else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
