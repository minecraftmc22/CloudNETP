# -*- coding: utf-8 -*-
"""三处模糊搜索的界面回归。

  [A] 设置 → 搜索：结果面板、命中项定位、点击跳到对应页签并高亮
  [B] 设置 → 无匹配提示 / Esc 清空
  [C] 我的文件 → 多词与忽略分隔符
  [D] 管理后台 → 用户列表按昵称、审计日志按中文动作名
  [E] 390px：搜索面板不出界
  [F] 无 JS 异常

    ./.venv/Scripts/python.exe scripts/fuzzy_search_ui.py
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)
PASSWORD = "Test123456"
CJK = re.compile(r"[\u4e00-\u9fa5]")

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


def upload(base: str, cookie: str, name: str, content: bytes, parent_id=None) -> dict:
    status, data = http(
        base, "POST", "/api/uploads/init",
        {"name": name, "size": len(content), "parentId": parent_id}, cookie,
    )
    assert status == 200, (status, data)
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
    assert status == 200, (status, done)
    return done["node"]


def login_cookie(base: str, username: str, password: str) -> str:
    req = urllib.request.Request(
        base + "/api/auth/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", base)
    with urllib.request.urlopen(req, timeout=30) as resp:
        for item in resp.headers.get_all("Set-Cookie") or []:
            if item.startswith("cloudnetp_session="):
                return item.split(";", 1)[0].split("=", 1)[1]
    raise SystemExit("登录没拿到 cookie")


def main() -> int:
    from app.config import settings
    from app.security import hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-fuzzyui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)

    con = sqlite3.connect(str(db_copy))
    row = con.execute(
        "select id, username, token_version from users order by role desc, id limit 1"
    ).fetchone()
    if row is None:
        raise SystemExit("副本库里没有用户")
    admin_id, admin_name, _ver = row
    con.execute(
        "update users set password_hash=?, role=1, is_super=1, display_name=? where id=?",
        (hash_password(PASSWORD), "模糊管理员", admin_id),
    )
    con.execute(
        "insert into users (username, password_hash, display_name, email, quota_bytes, used_bytes,"
        " role, banned, token_version, created_at) values (?,?,?,?,?,?,?,?,?,datetime('now'))",
        ("fuzzyuiprobe", "x", "张三丰", "zhang@example.com", 10 * 1024**3, 0, 0, 0, 0),
    )
    con.execute(
        "insert into audit_logs (user_id, action, detail, ip, created_at)"
        " values (?,?,?,?,datetime('now'))",
        (admin_id, "quick_send_reject", '{"name": "拒收的合同.pdf"}', "127.0.0.1"),
    )
    con.commit()
    con.close()

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
            raise SystemExit("服务没起来：" + str(log_path))
        session = login_cookie(base, admin_name, PASSWORD)
        cookie = f"cloudnetp_session={session}"

        # 造几个文件名各异的文件
        for name in ["2024-年度_报告.pdf", "2024-合同草案.docx", "report-final.txt"]:
            upload(base, cookie, name, b"fuzzy-ui-" + name.encode("utf-8"))

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True, args=["--force-device-scale-factor=1"])
            errors: list[str] = []

            def make_page(width=1440, height=1000):
                ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=2)
                ctx.add_cookies([{"name": "cloudnetp_session", "value": session, "url": base}])
                page = ctx.new_page()
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.on(
                    "console",
                    lambda msg: errors.append(msg.text)
                    if msg.type == "error" and "Failed to load resource" not in msg.text
                    else None,
                )
                return ctx, page

            ctx, page = make_page()

            # ---------------- A 设置页搜索 ----------------
            print("\n[A] 设置 → 搜索")
            page.goto(base + "/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector("[data-settings-search]", timeout=25000)
            box = page.locator("[data-settings-search]")

            box.click()
            box.fill("自动同意")
            page.wait_for_selector(".settings-hits:not([hidden])", timeout=10000)
            hits = page.locator(".settings-hit")
            labels = [hits.nth(i).inner_text().replace("\n", " ").strip() for i in range(hits.count())]
            check("有结果", hits.count() >= 1, str(labels[:3]))
            check("命中「自动同意，直接存进我的文件」", any("自动同意" in text for text in labels), str(labels[:3]))
            check("结果显示所属页签", any("快传" in text for text in labels), str(labels[:3]))
            check("命中词有高亮", page.locator(".settings-hit mark").count() >= 1,
                  str(page.locator(".settings-hit mark").count()))
            page.screenshot(path=str(OUT / "fuzzy_settings_hits.png"))

            page.locator(".settings-hit").first.click()
            # 先在高亮最亮的阶段断言，再截图（截图本身耗时不定，会把时序拖出高亮期）
            page.wait_for_timeout(500)
            active_tab = page.evaluate(
                "() => document.querySelector('[data-settings-tab].is-active')?.dataset.settingsTab"
            )
            check("跳到快传页签", active_tab == "quick", str(active_tab))
            visible_pane = page.evaluate(
                "() => [...document.querySelectorAll('[data-settings-pane]')].filter((p) => !p.hidden).map((p) => p.dataset.settingsPane)"
            )
            check("只显示目标页签", visible_pane == ["quick"], str(visible_pane))
            hit_rows = page.locator(".setting-row.is-hit")
            check("目标行有高亮", hit_rows.count() == 1, str(hit_rows.count()))
            check("高亮的正是自动同意那行", "自动同意" in (hit_rows.first.inner_text() if hit_rows.count() else ""))
            check("跳转后搜索框清空", box.input_value() == "", box.input_value())
            check("跳转后结果面板收起", page.locator(".settings-hits").is_hidden())
            page.screenshot(path=str(OUT / "fuzzy_settings_jump.png"))

            # 跨页签：搜「壁纸」应定位到外观
            box.fill("壁纸")
            page.wait_for_selector(".settings-hits:not([hidden])", timeout=10000)
            where = page.locator(".settings-hit").first.inner_text().replace("\n", " ")
            check("「壁纸」在外观页签", "外观" in where, where)

            # ---------------- B 无结果 + Esc ----------------
            print("\n[B] 无结果与 Esc")
            box.fill("这个设置肯定不存在zzz")
            page.wait_for_selector(".settings-hits:not([hidden])", timeout=10000)
            empty = page.locator(".settings-hits__empty")
            check("显示空结果提示", empty.count() == 1, empty.inner_text() if empty.count() else "")
            box.press("Escape")
            page.wait_for_timeout(300)
            check("Esc 清空输入", box.input_value() == "", box.input_value())
            check("Esc 收起面板", page.locator(".settings-hits").is_hidden())

            # 键盘：↓ 选第二条，Enter 进入
            box.fill("主题")
            page.wait_for_selector(".settings-hits:not([hidden])", timeout=10000)
            box.press("ArrowDown")
            page.wait_for_timeout(200)
            active_index = page.evaluate(
                "() => [...document.querySelectorAll('.settings-hit')].findIndex((el) => el.classList.contains('is-active'))"
            )
            check("↓ 把选中移到第二条", active_index == 1, str(active_index))
            box.press("Enter")
            page.wait_for_timeout(700)
            check("Enter 后跳转并清空", box.input_value() == "", box.input_value())

            # ---------------- C 我的文件 ----------------
            print("\n[C] 我的文件")
            page.goto(base + "/#/files", wait_until="domcontentloaded")
            page.wait_for_selector("[data-search]", timeout=25000)
            files_search = page.locator("[data-search]")
            # 列表 / 网格两种视图都要能读到名字
            def shown_names() -> list[str]:
                return page.locator(".frow__name, .tile__name").all_inner_texts()

            files_search.fill("2024 报告")
            page.wait_for_timeout(1800)
            names = shown_names()
            check("搜索有结果", bool(names), str(names[:5]))
            check("多词命中报告", any("2024-年度_报告.pdf" in n for n in names), str(names[:5]))
            check("多词排除合同", not any("合同" in n for n in names), str(names[:5]))

            files_search.fill("年度报告")
            page.wait_for_timeout(1800)
            names = shown_names()
            check("忽略分隔符命中", any("2024-年度_报告.pdf" in n for n in names), str(names[:5]))
            page.screenshot(path=str(OUT / "fuzzy_files_search.png"))

            # ---------------- D 管理后台 ----------------
            print("\n[D] 管理后台")
            page.goto(base + "/#/admin", wait_until="domcontentloaded")
            page.wait_for_selector(".admin-tabs [data-tab]", timeout=25000)
            page.locator('[data-tab="users"]').click()
            page.wait_for_selector(".admin-toolbar [data-search]", timeout=25000)
            user_search = page.locator(".admin-toolbar [data-search]")
            user_search.fill("三丰")
            page.wait_for_timeout(1200)
            body = page.locator(".admin-body").inner_text()
            check("按昵称搜到账号", "fuzzyuiprobe" in body, body[:80].replace("\n", " "))

            page.locator('[data-tab="logs"]').click()
            page.wait_for_selector(".log-row", timeout=25000)
            log_search = page.locator(".admin-toolbar [data-search]")
            log_search.fill("拒绝快传")
            page.wait_for_timeout(1200)
            titles = page.locator(".log-title").all_inner_texts()
            check("中文动作名命中", any("拒绝快传" in t for t in titles), str(titles[:3]))
            page.screenshot(path=str(OUT / "fuzzy_admin_logs.png"))

            # ---------------- E 390px ----------------
            print("\n[E] 390px")
            ctx_m, page_m = make_page(390, 844)
            page_m.goto(base + "/#/settings", wait_until="domcontentloaded")
            page_m.wait_for_selector("[data-settings-search]", timeout=25000)
            search_m = page_m.locator("[data-settings-search]")
            search_m.click()
            search_m.fill("自动同意")
            page_m.wait_for_selector(".settings-hits:not([hidden])", timeout=10000)
            overflow = page_m.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            check("窄屏不横向溢出", overflow <= 1, str(overflow))
            hits_box = page_m.locator(".settings-hits").bounding_box()
            vw = page_m.evaluate("() => window.innerWidth")
            check("结果面板不出右边界", hits_box and hits_box["x"] + hits_box["width"] <= vw + 1,
                  f"{hits_box} vw={vw}")
            page_m.screenshot(path=str(OUT / "fuzzy_settings_mobile.png"))
            ctx_m.close()

            check("控制台无 JS 异常", not errors, str(errors[:3]))
            ctx.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    print(f"\n结果：{passed} 项通过，{failed} 项失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
