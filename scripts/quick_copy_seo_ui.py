# -*- coding: utf-8 -*-
"""取件码自动复制分享文案 + 管理后台 SEO 卡片的界面验收。

  [A] 默认模板：生成取件码后剪贴板里就是那段「我在…分享了…了喵~」，
      取件码、文件名、取件链接都填好了，占位符一个不剩
  [B] 卡片上的「复制分享文案」按钮能再复制一次
  [C] 后台改模板：保存后用户端重新进快传，生成时复制的是新文案
  [D] 关掉「生成后自动复制」：剪贴板保持原样，按钮仍可手动复制
  [E] 后台 SEO 卡片：默认不收录、预览跟着输入变、保存后首页 title/description 真的变了
  [F] 390px：取件码卡片不溢出、后台 SEO 卡片可读
  [G] 全程无 JS 异常

    ./.venv/Scripts/python.exe scripts/quick_copy_seo_ui.py
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
    if data is not None:
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


def upload(base: str, cookie: str, name: str, content: bytes) -> dict:
    status, data = http(base, "POST", "/api/uploads/init", {"name": name, "size": len(content)}, cookie)
    if status != 200:
        raise SystemExit(f"init 失败 {status} {data}")
    sid = data["sessionId"]
    chunk = data.get("chunkSize") or len(content)
    for index in data.get("missing") or []:
        start = index * chunk
        req = urllib.request.Request(
            f"{base}/api/uploads/{sid}/{index}", data=content[start : start + chunk], method="PUT"
        )
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("Cookie", cookie)
        urllib.request.urlopen(req, timeout=30).read()
    status, done = http(base, "POST", f"/api/uploads/{sid}/complete", None, cookie)
    if status != 200:
        raise SystemExit(f"complete 失败 {status} {done}")
    return done["node"]


def main() -> int:
    from app.config import settings
    from app.security import hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-copyseoui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)

    con = sqlite3.connect(str(db_copy))
    for stmt in (
        "delete from quick_transfers",
        "delete from system_settings where k like 'seo_%' or k like 'quick_%'",
    ):
        try:
            con.execute(stmt)
        except sqlite3.OperationalError:
            pass
    users = con.execute(
        "select id, username, token_version, role from users order by role asc, id limit 1"
    ).fetchall()
    admins = con.execute(
        "select id, username, token_version, role from users where role=1 order by id limit 1"
    ).fetchall()
    if not admins:
        con.execute(
            "insert into users (username, password_hash, display_name, quota_bytes, used_bytes, role,"
            " token_version, created_at) values (?,?,?,?,?,?,?,datetime('now'))",
            ("copyuiadmin", "x", "copyuiadmin", 10 * 1024 ** 3, 0, 1, 0),
        )
        con.commit()
        admins = con.execute(
            "select id, username, token_version, role from users where role=1 order by id limit 1"
        ).fetchall()
    hashed = hash_password(PASSWORD)
    for row in users + admins:
        con.execute("update users set password_hash=? where id=?", (hashed, row[0]))
    ids = sorted({row[0] for row in users + admins})
    con.execute(
        f"update user_settings set quick_terms_at=datetime('now') where user_id in ({','.join('?' * len(ids))})",
        ids,
    )
    con.commit()
    con.close()

    user_name = users[0][1]
    admin_name = admins[0][1]

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

    def login_cookie(name: str) -> str:
        req = urllib.request.Request(
            base + "/api/auth/login",
            data=json.dumps({"username": name, "password": PASSWORD}).encode(),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return (resp.headers.get("Set-Cookie") or "").split(";")[0]

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来，看日志：" + str(log_path))

        cookie = login_cookie(user_name)
        token = cookie.split("=", 1)[1]
        upload(base, cookie, "自动复制测试.txt", b"copy-seo-body" * 120)

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)

            def new_page(cookie_value: str, token_value: str, width: int = 1440, height: int = 1000):
                ctx = browser.new_context(
                    viewport={"width": width, "height": height}, device_scale_factor=2
                )
                ctx.add_cookies([{"name": "cloudnetp_session", "value": token_value, "url": base}])
                ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
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

            def open_pickup(page):
                # goto 到同一个 hash 不会重新加载文档，这里显式 reload，
                # 否则会拿到上一轮缓存在内存里的 limits（模板 / 自动复制开关）
                page.goto(base + "/#/quick", wait_until="domcontentloaded")
                page.reload(wait_until="domcontentloaded")
                page.evaluate(
                    "() => { const m = document.getElementById('mascot'); if (m) m.style.visibility = 'hidden'; }"
                )
                page.wait_for_selector(".quick-tabs", timeout=20000)
                page.locator('.quick-tab[data-tab="pickup"]').click()
                page.wait_for_selector('[data-act="pick"]', timeout=20000)

            def pick_file(page, name: str):
                page.locator('[data-act="pick"]').click()
                page.wait_for_selector(".picker .picker__row", timeout=15000)
                row = page.locator(".picker__row", has_text=name).first
                row.click()
                page.wait_for_function(
                    "() => { const b = document.querySelector('.picker [data-act=\"ok\"]');"
                    " return b && !b.disabled; }",
                    timeout=10000,
                )
                page.locator('.picker [data-act="ok"]').click()
                page.wait_for_selector(".picker", state="detached", timeout=10000)

            def generate(page, wait_text: str = "取件码已生成"):
                page.locator('[data-act="new"]').click()
                page.wait_for_selector("[data-last] .quick-share", timeout=20000)
                # 等这一轮的提示出现，别拿到上一条 toast 的文案
                page.wait_for_function(
                    "text => [...document.querySelectorAll('.toast')]"
                    ".some(el => el.textContent.includes(text))",
                    arg=wait_text,
                    timeout=20000,
                )
                code = page.locator("[data-last] [data-code]").inner_text().strip()
                return code

            def clipboard(page) -> str:
                page.bring_to_front()
                return page.evaluate("() => navigator.clipboard.readText()")

            def clear_clipboard(page):
                page.evaluate("() => navigator.clipboard.writeText('')")

            # ---------------- A 默认模板 ----------------
            print("\n[A] 默认模板：生成即复制")
            ctx, page, errors = new_page(cookie, token)
            open_pickup(page)
            pick_file(page, "自动复制测试.txt")
            check("选中的文件进了待生成列表", page.locator("[data-items] .quick-item-row").count() >= 1)
            page.evaluate("() => navigator.clipboard.writeText('before-generate')")
            code = generate(page)
            text = clipboard(page)
            check("剪贴板被改写", "before-generate" != text, text[:40])
            check("第一句是「我在…分享了」", text.startswith('我在"'), text[:40])
            check("文案里带了口号", "了喵~" in text and "来取件吧喵awa!" in text, text[-30:])
            check("取件码填进去了", code and code in text, f"{code} / {text[:60]}")
            check("文件名填进去了", "自动复制测试.txt" in text)
            check("链接填进去了", "/pickup?code=" in text and code in text.split("/pickup?code=")[-1])
            check("没有残留占位符", "{" not in text and "}" not in text, text)
            check("两行结构：换行在码前", len(text.splitlines()) >= 2, str(len(text.splitlines())))
            toast = page.locator(".toast").inner_text()
            check("提示里说明已复制", "已复制" in toast, toast)
            page.screenshot(path=str(OUT / "quick_copy_autocopy.png"))

            # ---------------- B 手动再复制 ----------------
            print("\n[B] 手动复制按钮")
            page.evaluate("() => navigator.clipboard.writeText('cleared')")
            page.locator('[data-last] [data-act="copy-msg"]').click()
            page.wait_for_function(
                "() => document.querySelector('.toast')?.textContent.includes('分享文案已复制')",
                timeout=10000,
            )
            again = clipboard(page)
            check("按钮复制的内容跟自动复制一致", again == text, again[:40])

            # ---------------- C 后台改模板 ----------------
            print("\n[C] 后台改模板后用户端跟着变")
            admin_cookie = login_cookie(admin_name)
            actx, apage, aerrors = new_page(admin_cookie, admin_cookie.split("=", 1)[1])
            apage.goto(base + "/#/admin", wait_until="domcontentloaded")
            apage.wait_for_selector(".admin-tabs [data-tab='settings']", timeout=20000)
            apage.locator(".admin-tabs [data-tab='settings']").click()
            apage.wait_for_selector('[data-card="quick"]', timeout=20000)
            field = apage.locator('[data-card="quick"] textarea[data-key="quickCopyTemplate"]')
            field.scroll_into_view_if_needed()
            default_tpl = field.input_value()
            check("后台预填了默认模板", default_tpl.startswith('我在"{site}"'), default_tpl[:30])
            new_tpl = "来取 {file} 的码：{Code}\n{link} —— 来自 {site}"
            field.fill(new_tpl)
            apage.locator('[data-card="quick"] [data-save="quick"]').click()
            apage.wait_for_function(
                "() => document.querySelector('.toast')?.textContent.includes('设置已保存')",
                timeout=15000,
            )
            status, data = http(base, "GET", "/api/quick/limits", cookie=cookie)
            check("接口吐出新模板", (data.get("limits") or {}).get("copyTemplate") == new_tpl)

            open_pickup(page)
            pick_file(page, "自动复制测试.txt")
            page.evaluate("() => navigator.clipboard.writeText('old')")
            code2 = generate(page)
            text2 = clipboard(page)
            check("复制的是新模板", text2.startswith("来取 自动复制测试.txt 的码："), text2[:40])
            check("占位符大小写都换掉了", code2 in text2 and "{" not in text2, text2)
            check("站点名也换了", "CloudNETP" in text2 or "来自" in text2, text2[-30:])

            # ---------------- D 关掉自动复制 ----------------
            print("\n[D] 关掉自动复制")
            apage.locator('[data-card="quick"] input[data-key="quickAutoCopy"]').evaluate(
                "el => { el.checked = false; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            apage.locator('[data-card="quick"] [data-save="quick"]').click()
            apage.wait_for_function(
                "() => document.querySelector('.toast')?.textContent.includes('设置已保存')",
                timeout=15000,
            )
            open_pickup(page)
            pick_file(page, "自动复制测试.txt")
            page.evaluate("() => navigator.clipboard.writeText('keep-me')")
            generate(page)
            kept = clipboard(page)
            check("剪贴板没被动", kept == "keep-me", kept)
            check("提示只说生成成功", "已复制" not in page.locator(".toast").inner_text())
            page.locator('[data-last] [data-act="copy-msg"]').click()
            page.wait_for_function(
                "() => document.querySelector('.toast')?.textContent.includes('分享文案已复制')",
                timeout=10000,
            )
            check("手动按钮仍能复制", clipboard(page).startswith("来取 自动复制测试.txt"), clipboard(page)[:30])

            # ---------------- E SEO 卡片 ----------------
            print("\n[E] 后台 SEO")
            card = apage.locator('[data-card="seo"]')
            card.scroll_into_view_if_needed()
            check("SEO 卡片在", card.count() == 1)
            check("默认不收录（预览带标签）", card.locator("[data-seo-tag]").is_visible())
            preview_title = card.locator("[data-seo-title]").inner_text()
            check("预览先给出站点默认标题", "CloudNETP" in preview_title, preview_title)
            apage.fill('[data-card="seo"] input[data-key="seoTitle"]', "小云的私有盘")
            apage.fill('[data-card="seo"] textarea[data-key="seoDescription"]', "自建网盘，文件快传不用注册。")
            apage.fill('[data-card="seo"] input[data-key="seoBaseUrl"]', "https://pan.example.com")
            apage.wait_for_function(
                "() => document.querySelector('[data-card=\"seo\"] [data-seo-title]')"
                ".textContent.includes('小云的私有盘')",
                timeout=10000,
            )
            check(
                "预览标题跟着输入变",
                card.locator("[data-seo-title]").inner_text() == "小云的私有盘",
            )
            check(
                "预览描述跟着输入变",
                "自建网盘" in card.locator("[data-seo-desc]").inner_text(),
                card.locator("[data-seo-desc]").inner_text(),
            )
            check(
                "预览地址用填的公开地址",
                card.locator("[data-seo-url]").inner_text() == "https://pan.example.com/",
                card.locator("[data-seo-url]").inner_text(),
            )
            apage.locator('[data-card="seo"] input[data-key="seoIndex"]').evaluate(
                "el => { el.checked = true; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            check("勾上收录后「不收录」标签消失", not card.locator("[data-seo-tag]").is_visible())
            apage.screenshot(path=str(OUT / "admin_seo_card.png"))
            # 浅色主题也看一眼：预览块的底色与标题色在浅色下别糊成一片
            apage.locator("#theme-toggle").click()
            apage.wait_for_timeout(500)
            apage.locator('[data-card="seo"]').scroll_into_view_if_needed()
            apage.wait_for_timeout(300)
            apage.screenshot(path=str(OUT / "admin_seo_card_light.png"))
            apage.locator("#theme-toggle").click()
            apage.wait_for_timeout(400)
            apage.locator('[data-card="seo"] [data-save="seo"]').click()
            apage.wait_for_function(
                "() => document.querySelector('.toast')?.textContent.includes('设置已保存')",
                timeout=15000,
            )
            status, data = http(base, "GET", "/api/admin/settings", cookie=admin_cookie)
            saved = data.get("settings") or {}
            check("后台存下 SEO 标题", saved.get("seoTitle") == "小云的私有盘", str(saved.get("seoTitle")))
            check("后台存下收录开关", saved.get("seoIndex") is True, str(saved.get("seoIndex")))

            probe = apage.context.new_page()
            probe.goto(base + "/", wait_until="domcontentloaded")
            check("首页标题被服务端换掉", probe.title() == "小云的私有盘", probe.title())
            html = probe.content()
            check("首页有 description", 'name="description"' in html and "自建网盘" in html)
            check("首页有 canonical", 'rel="canonical" href="https://pan.example.com/"' in html)
            probe.close()

            # ---------------- F 390px ----------------
            print("\n[F] 390px 窄屏")
            open_pickup(page)
            pick_file(page, "自动复制测试.txt")
            generate(page)
            page.set_viewport_size({"width": 390, "height": 780})
            page.wait_for_timeout(400)
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            check("取件码页没有横向滚动", overflow <= 1, str(overflow))
            box = page.locator("[data-last] .quick-share").bounding_box()
            check("取件码卡片没超出视口", box and box["x"] >= 0 and box["x"] + box["width"] <= 391, str(box))
            page.locator("[data-last]").scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            page.screenshot(path=str(OUT / "quick_copy_mobile.png"))

            apage.set_viewport_size({"width": 390, "height": 780})
            apage.locator('[data-card="seo"]').scroll_into_view_if_needed()
            apage.wait_for_timeout(400)
            overflow_admin = apage.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            check("后台设置页没有横向滚动", overflow_admin <= 1, str(overflow_admin))
            apage.screenshot(path=str(OUT / "admin_seo_mobile.png"))

            # ---------------- G 控制台 ----------------
            print("\n[G] 控制台")
            check("用户端没有 JS 异常", not errors, str(errors[:3]))
            check("后台没有 JS 异常", not aerrors, str(aerrors[:3]))

            browser.close()

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    print(f"\n通过 {passed} 项，失败 {failed} 项")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
