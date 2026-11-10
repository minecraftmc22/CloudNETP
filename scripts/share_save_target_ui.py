# -*- coding: utf-8 -*-
"""分享页 UI 冒烟：右上角登录态 + 转存时可视化选保存位置。

用库副本起独立实例，Playwright 驱动本机 Chrome：
  [A] 未登录：右上角出现「登录 / 注册」，登录链接带回当前分享地址
  [B] 点「登录」→ 登录页，登录成功自动跳回分享页，右上角变成自己的名字
  [C] 转存弹窗里能浏览自己的文件夹，选到子目录后确认，真的存进那个目录
  [D] 375px 窄屏：弹窗不溢出，选择区可滚动

    ./.venv/Scripts/python.exe scripts/share_save_target_ui.py
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

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-share-ui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)

    con = sqlite3.connect(str(db_copy))
    admin = con.execute(
        "select id, username, token_version from users where role = 1 order by id limit 1"
    ).fetchone()
    if not admin:
        raise SystemExit("库里没有管理员，先手动建一个再来跑这个脚本")
    con.execute("update users set password_hash=? where id=?", (hash_password(PASSWORD), admin[0]))
    shared = con.execute(
        "select id, name from nodes where type = 'file' and deleted_at is null order by id limit 1"
    ).fetchone()
    if not shared:
        raise SystemExit("库里没有可分享的文件，先传一个再来跑这个脚本")
    con.commit()
    con.close()

    session_token = create_token(admin[0], admin[2])
    cookie = f"cloudnetp_session={session_token}"
    file_id, file_name = shared
    admin_name = admin[1]

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

        # ---- 准备：自己的两个文件夹 + 一条文件分享 ----
        st, d = http(base, "POST", "/api/nodes/folder", {"name": "接收文件夹", "parentId": None}, cookie)
        assert st in (200, 201), (st, d)
        folder_a = d["node"]["id"]
        st, d = http(base, "POST", "/api/nodes/folder", {"name": "子目录", "parentId": folder_a}, cookie)
        assert st in (200, 201), (st, d)
        folder_b = d["node"]["id"]
        st, d = http(base, "POST", "/api/shares", {"nodeIds": [file_id], "expiresDays": 7}, cookie)
        assert st == 200, (st, d)
        token = d["share"]["token"]
        share_id = d["share"]["id"]
        share_url = f"/s/{token}"

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
                    # 未登录访客的 /api/me 必然 401，浏览器会记一条资源加载失败，属预期
                    if msg.type == "error" and "Failed to load resource" not in msg.text
                    else None,
                )
                return ctx, page, errors

            print("\n[A] 未登录访客：右上角「登录 / 注册」")
            ctx, page, errors = make_page()
            page.goto(base + share_url, wait_until="domcontentloaded")
            page.wait_for_selector("[data-auth] a", timeout=20000)
            check("右上角登录区可见", page.locator("[data-auth]").is_visible())
            login_link = page.locator("[data-auth-login]")
            reg_link = page.locator("[data-auth-register]")
            check("有「登录」按钮", login_link.is_visible())
            check("有「注册」按钮", reg_link.is_visible())
            check("登录按钮文字", login_link.inner_text().strip() == "登录", login_link.inner_text().strip())
            check("注册按钮文字", reg_link.inner_text().strip() == "注册", reg_link.inner_text().strip())
            href = login_link.get_attribute("href") or ""
            check("登录链接带回当前分享页", href.endswith(f"next=%2F{share_url.lstrip('/')}") or "next=" in href, href)
            reg_href = reg_link.get_attribute("href") or ""
            check("注册链接带 mode=register", "mode=register" in reg_href, reg_href)
            geo = page.evaluate(
                """() => {
                    const bar = document.querySelector('.share-brand').getBoundingClientRect();
                    const auth = document.querySelector('[data-auth]').getBoundingClientRect();
                    const brand = document.querySelector('.share-brand__id').getBoundingClientRect();
                    return {
                        barRight: Math.round(bar.right), authRight: Math.round(auth.right),
                        authTop: Math.round(auth.top), brandTop: Math.round(brand.top),
                        vw: innerWidth,
                    };
                }"""
            )
            check(
                "登录区贴在右上角",
                abs(geo["authRight"] - geo["barRight"]) <= 2 and abs(geo["authTop"] - geo["brandTop"]) <= 6,
                str(geo),
            )
            page.screenshot(path=str(OUT / "share_auth_guest.png"), full_page=False)
            check("无 JS 报错", not errors, str(errors[:3]))

            print("\n[A2] 点「注册」落在注册页签")
            page.goto(base + reg_href, wait_until="domcontentloaded")
            page.wait_for_selector("[data-form]", timeout=20000)
            check(
                "注册页签被选中",
                page.locator('[data-mode="register"]').evaluate("el => el.classList.contains('is-active')"),
            )
            check("注册才有的确认密码可见", page.locator("[data-f-confirm]").is_visible())

            print("\n[B] 登录后自动跳回分享页")
            page.goto(base + f"/?next={share_url}", wait_until="domcontentloaded")
            page.wait_for_selector('[data-form] [name="account"]', timeout=20000)
            page.fill('[data-form] [name="account"]', admin_name)
            page.fill('[data-form] [name="password"]', PASSWORD)
            # 原生 checkbox 被样式藏起来了，直接置状态（提交时读 .checked）
            page.evaluate(
                """() => {
                    const box = document.querySelector("[data-agree]");
                    box.checked = true;
                    box.dispatchEvent(new Event("change", { bubbles: true }));
                }"""
            )
            page.click("[data-submit]")
            page.wait_for_url(f"**{share_url}", timeout=20000)
            page.wait_for_selector("[data-auth] a", timeout=20000)
            check("登录后回到分享页", page.url.endswith(share_url), page.url)
            who = page.locator("[data-auth-me]")
            check("右上角变成自己的账号", who.count() == 1 and who.is_visible())
            check("账号 chip 指向网盘", (who.get_attribute("href") or "").endswith("/#/files"), who.get_attribute("href") or "")
            check("不再显示登录按钮", page.locator("[data-auth-login]").count() == 0)
            page.screenshot(path=str(OUT / "share_auth_user.png"), full_page=False)
            check("全程无 JS 报错", not errors, str(errors[:3]))
            ctx.close()

            st, root_before = http(base, "GET", "/api/nodes", cookie=cookie)
            root_names_before = [i["name"] for i in root_before.get("items", [])]

            print("\n[C] 转存：可视化选保存位置")
            ctx, page, errors = make_page(session_token)
            page.goto(base + share_url, wait_until="domcontentloaded")
            page.wait_for_selector("[data-auth-me]", timeout=20000)
            page.click("[data-save-drive]")
            page.wait_for_selector(".save-tree", timeout=20000)
            page.wait_for_function(
                "() => !document.querySelector('.save-tree').textContent.includes('加载中')",
                timeout=20000,
            )
            crumb = page.locator("[data-save-crumb]").inner_text().strip()
            check("默认位置是「我的文件」", crumb == "我的文件", crumb)
            names = page.locator(".save-tree__row span").all_inner_texts()
            check("列出自己的文件夹", "接收文件夹" in names, str(names))
            check("弹窗里没有文件（只能选目录）", "content.pdf" not in names, str(names))
            page.screenshot(path=str(OUT / "share_save_picker.png"), full_page=False)

            page.click('.save-tree__row:has-text("接收文件夹")')
            page.wait_for_function(
                "() => document.querySelector('.save-crumb').textContent.includes('接收文件夹')",
                timeout=20000,
            )
            crumb = page.locator("[data-save-crumb]").inner_text().strip()
            check("面包屑走到第二层", "我的文件" in crumb and "接收文件夹" in crumb, crumb)
            page.wait_for_function(
                "() => document.querySelector('.save-tree').textContent.includes('子目录')",
                timeout=20000,
            )
            check("子目录出现在列表里", page.locator(".save-tree__row span").all_inner_texts() == ["子目录"])

            print("\n[C2] 再进一层，存到「子目录」")
            page.click('.save-tree__row:has-text("子目录")')
            page.wait_for_function(
                "() => document.querySelector('.save-crumb').textContent.includes('子目录')",
                timeout=20000,
            )
            page.wait_for_function(
                "() => !document.querySelector('.save-tree').textContent.includes('加载中')",
                timeout=20000,
            )
            tree_text = page.locator(".save-tree").inner_text()
            check("空目录给提示", "没有子文件夹" in tree_text, tree_text.strip()[:40])
            page.screenshot(path=str(OUT / "share_save_picker_deep.png"), full_page=False)
            page.click("[data-ok]")
            page.wait_for_function(
                "() => document.querySelector('.share-modal__title')?.textContent.includes('转存成功')",
                timeout=20000,
            )
            done = page.locator(".share-modal__body").inner_text()
            check("成功提示写明目标位置", "子目录" in done and "接收文件夹" in done, done.replace("\n", " / ")[:80])
            page.click(".share-modal__foot [data-ok]")

            st, listing = http(base, "GET", f"/api/nodes?parentId={folder_b}", cookie=cookie)
            saved = [i["name"] for i in listing.get("items", [])]
            check("文件真的存进了所选目录", file_name in saved, str(saved))
            st, root_after = http(base, "GET", "/api/nodes", cookie=cookie)
            root_names = [i["name"] for i in root_after.get("items", [])]
            check(
                "根目录没有多出副本",
                root_names.count(file_name) == root_names_before.count(file_name),
                f"{root_names_before.count(file_name)} → {root_names.count(file_name)}",
            )

            print("\n[C3] 面包屑回退到根目录再存一次")
            page.click("[data-save-drive]")
            page.wait_for_selector(".save-tree__row", timeout=20000)
            page.click('.save-tree__row:has-text("接收文件夹")')
            page.wait_for_function(
                "() => document.querySelector('.save-crumb').textContent.includes('接收文件夹')",
                timeout=20000,
            )
            page.click("[data-save-crumb] button:first-child")
            page.wait_for_function(
                "() => document.querySelector('.save-crumb').textContent.trim() === '我的文件'",
                timeout=20000,
            )
            check("点面包屑回到根目录", page.locator("[data-save-crumb]").inner_text().strip() == "我的文件")
            page.click(".share-modal__foot [data-cancel]", timeout=5000)
            check("取消后弹窗关闭", page.locator(".share-modal").count() == 0)
            check("全程无 JS 报错", not errors, str(errors[:3]))
            ctx.close()

            print("\n[D] 375px 窄屏")
            ctx, page, errors = make_page(session_token, width=375, height=780)
            page.goto(base + share_url, wait_until="domcontentloaded")
            page.wait_for_selector("[data-auth-me]", timeout=20000)
            page.click("[data-save-drive]")
            page.wait_for_selector(".save-tree", timeout=20000)
            page.wait_for_timeout(600)
            box = page.evaluate(
                """() => {
                    const m = document.querySelector('.share-modal').getBoundingClientRect();
                    const bar = document.querySelector('.share-brand').getBoundingClientRect();
                    return {
                        left: Math.round(m.left), right: Math.round(m.right),
                        vw: innerWidth, barOver: Math.round(bar.width - innerWidth),
                        bodyOver: document.documentElement.scrollWidth - innerWidth,
                    };
                }"""
            )
            check("弹窗在视口内", box["left"] >= 0 and box["right"] <= box["vw"] + 1, str(box))
            check("页面没有横向滚动", box["bodyOver"] <= 1, str(box))
            page.screenshot(path=str(OUT / "share_save_mobile.png"), full_page=False)
            check("窄屏无 JS 报错", not errors, str(errors[:3]))
            ctx.close()
            browser.close()

            # 收尾：删掉这次建的东西
            http(base, "DELETE", f"/api/shares/{share_id}", cookie=cookie)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        finally:
            log_file.close()

    print(f"\n通过 {passed} · 失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
