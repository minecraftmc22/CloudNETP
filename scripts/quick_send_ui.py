# -*- coding: utf-8 -*-
"""「快传 → 发给用户」接收方同意流程的 UI 冒烟。

  [A] 接收方打开快传：待接收角标、条目高亮、三个操作按钮
  [B] 双击查看内容：弹层能进文件夹、能双击预览文件（viewer 真打开了）
  [C] 同意：条目变「已存入」，文件真的落到 我的文件/快传的文件
  [D] 拒绝：确认后条目变「被拒绝」，暂存文件被回收
  [E] 发送方：「文件被拒绝」提示条 + 点「知道了」消失，发件箱标签变「被拒绝」
  [F] 设置 → 快传：两个开关、选择收件目录、恢复默认
  [G] 390px：条目换行、按钮仍可点
  [H] 控制台无 JS 异常

    ./.venv/Scripts/python.exe scripts/quick_send_ui.py
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


def upload(base: str, cookie: str, name: str, content: bytes, parent_id=None) -> dict:
    status, data = http(
        base, "POST", "/api/uploads/init",
        {"name": name, "size": len(content), "parentId": parent_id}, cookie,
    )
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
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-sendui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)

    con = sqlite3.connect(str(db_copy))
    for stmt in ("delete from quick_transfers",):
        try:
            con.execute(stmt)
        except sqlite3.OperationalError:
            pass
    users = con.execute(
        "select id, username, token_version from users order by role desc, id limit 2"
    ).fetchall()
    while len(users) < 2:
        name = f"senduib{len(users)}"
        con.execute(
            "insert into users (username, password_hash, display_name, quota_bytes, used_bytes, role,"
            " token_version, created_at) values (?,?,?,?,?,?,?,datetime('now'))",
            (name, "x", name, 10 * 1024 ** 3, 0, 0, 0),
        )
        con.commit()
        users = con.execute(
            "select id, username, token_version from users order by role desc, id limit 2"
        ).fetchall()
    ids = [row[0] for row in users]
    hashed = hash_password(PASSWORD)
    for row in users:
        con.execute("update users set password_hash=? where id=?", (hashed, row[0]))
    try:
        # 只改老 schema 里就有的列：新列由服务启动时的补列逻辑补上（默认允许接收、不自动同意）
        con.execute(
            "update user_settings set quick_terms_at=datetime('now') where user_id in (?,?)",
            ids,
        )
    except sqlite3.OperationalError:
        pass
    con.commit()
    con.close()

    a_id, a_name, a_ver = users[0]
    b_id, b_name, b_ver = users[1]
    a_token = create_token(a_id, a_ver)
    b_token = create_token(b_id, b_ver)
    a_cookie = f"cloudnetp_session={a_token}"
    b_cookie = f"cloudnetp_session={b_token}"

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

        # 造数据：A 发一个文件 + 一个文件夹给 B
        status, folder = http(
            base, "POST", "/api/nodes/folder", {"parentId": None, "name": "收件文件夹"}, a_cookie
        )
        folder_id = folder["node"]["id"]
        text_node = upload(base, a_cookie, "同意看看.txt", b"send-ui-body" * 200)
        upload(base, a_cookie, "里面一.txt", b"inner-one" * 50, folder_id)
        upload(base, a_cookie, "里面二.txt", b"inner-two" * 50, folder_id)

        status, data = http(
            base, "POST", "/api/quick/sends", {
                "nodeIds": [text_node["id"], folder_id],
                "toUsername": b_name,
                "message": "两个东西给你",
            }, a_cookie,
        )
        if status != 200:
            raise SystemExit(f"发送失败 {status} {data}")
        send_id = data["send"]["id"]

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)

            def make_page(token_value: str, width: int = 1440, height: int = 1000):
                ctx = browser.new_context(
                    viewport={"width": width, "height": height}, device_scale_factor=2
                )
                ctx.add_cookies([{"name": "cloudnetp_session", "value": token_value, "url": base}])
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.on(
                    "console",
                    lambda msg: errors.append(msg.text)
                    if msg.type == "error" and "Failed to load resource" not in msg.text
                    else None,
                )
                page.goto(base + "/#/quick", wait_until="domcontentloaded")
                page.evaluate(
                    "() => { const m = document.getElementById('mascot'); if (m) m.style.visibility = 'hidden'; }"
                )
                page.wait_for_selector(".quick-tabs", timeout=20000)
                page.locator('.quick-tab[data-tab="send"]').click()
                page.wait_for_selector("[data-inbox]", timeout=20000)
                return ctx, page, errors

            # ---------------- A 接收方列表 ----------------
            print("\n[A] 接收方：待接收列表")
            ctx, page, errors = make_page(b_token)
            page.wait_for_selector(".quick-row[data-send]", timeout=20000)
            rows = page.locator(".quick-row[data-send]")
            check("收到 1 条记录（一次发两项）", rows.count() == 1, str(rows.count()))
            check("条目是待接收状态", page.locator(".quick-row.is-pending").count() == 1)
            tag_text = page.locator(".quick-row.is-pending .quick-row__tag").inner_text()
            check("标签写「待接收」", "待接收" in tag_text, tag_text)
            head_text = page.locator(".quick-card").nth(1).inner_text()
            check("卡片头带待接收角标", "待接收" in head_text, head_text[:60])
            buttons = page.locator('.quick-row.is-pending [data-act="browse"], .quick-row.is-pending [data-act="accept"], .quick-row.is-pending [data-act="reject"]')
            check("三个操作按钮都在", buttons.count() == 3, str(buttons.count()))
            page.screenshot(path=str(OUT / "send_inbox_pending.png"))

            # ---------------- B 双击查看内容 ----------------
            print("\n[B] 双击查看内容")
            page.locator(".quick-row.is-pending").dblclick()
            page.wait_for_selector(".picker.send-browse", timeout=15000)
            rows_browse = page.locator(".send-browse .send-row")
            check("弹层列出 2 项", rows_browse.count() == 2, str(rows_browse.count()))
            page.locator('.send-browse .send-row[data-type="folder"]').dblclick()
            page.wait_for_function(
                "() => document.querySelectorAll('.send-browse .send-row').length === 2 &&"
                " document.querySelector('.send-browse .picker__crumb').textContent.includes('收件文件夹')",
                timeout=15000,
            )
            names = page.locator(".send-browse .send-row__name").all_inner_texts()
            check("进文件夹看到里面两个文件", set(names) == {"里面一.txt", "里面二.txt"}, str(names))
            page.screenshot(path=str(OUT / "send_browse_folder.png"))
            page.locator('.send-browse .send-row[data-type="file"]').first.dblclick()
            page.wait_for_selector(".viewer", timeout=15000)
            check("双击文件打开了预览器", page.locator(".viewer").count() == 1)
            page.keyboard.press("Escape")
            page.wait_for_selector(".viewer", state="detached", timeout=10000)
            page.locator('.send-browse [data-act="close"]').click()
            page.wait_for_selector(".send-browse", state="detached", timeout=10000)

            # ---------------- C 拒绝 ----------------
            print("\n[C] 拒绝")
            page.locator('.quick-row.is-pending [data-act="reject"]').click()
            page.wait_for_selector(".dialog [data-act='ok']", timeout=10000)
            page.locator(".dialog [data-act='ok']").click()
            page.wait_for_function(
                "() => { const tag = document.querySelector('.quick-row[data-send] .quick-row__tag');"
                " return tag && tag.textContent.includes('被拒绝'); }",
                timeout=20000,
            )
            check("标签变成「被拒绝」", True)
            check("拒绝后不再显示操作按钮", page.locator('.quick-row[data-send] [data-act="accept"]').count() == 0)
            page.screenshot(path=str(OUT / "send_rejected.png"))

            # ---------------- D 重新发一条，走同意 ----------------
            print("\n[D] 同意存入")
            node2 = upload(base, a_cookie, "点头同意.txt", b"accept-me" * 300)
            status, data = http(
                base, "POST", "/api/quick/sends",
                {"nodeIds": [node2["id"]], "toUsername": b_name}, a_cookie,
            )
            accept_id = data["send"]["id"]
            page.reload(wait_until="domcontentloaded")
            page.evaluate(
                "() => { const m = document.getElementById('mascot'); if (m) m.style.visibility = 'hidden'; }"
            )
            page.wait_for_selector(".quick-tabs", timeout=20000)
            page.locator('.quick-tab[data-tab="send"]').click()
            page.wait_for_selector(".quick-row.is-pending", timeout=20000)
            page.locator('.quick-row.is-pending [data-act="accept"]').click()
            page.wait_for_selector(".toast", timeout=15000)
            page.wait_for_function(
                "() => { const rows = [...document.querySelectorAll('.quick-row[data-send]')];"
                " const row = rows.find(r => r.textContent.includes('点头同意.txt'));"
                " return row && row.textContent.includes('已存入'); }",
                timeout=20000,
            )
            check("条目变成「已存入」", True)
            status, listing = http(base, "GET", "/api/nodes", None, b_cookie)
            inbox_folder = next(
                (i["id"] for i in listing.get("items") or [] if i["name"] == "快传的文件"), None
            )
            status, listing = http(base, "GET", f"/api/nodes?parentId={inbox_folder}", None, b_cookie)
            names = [i["name"] for i in listing.get("items") or []]
            check("文件落进 我的文件/快传的文件", "点头同意.txt" in names, str(names))
            page.screenshot(path=str(OUT / "send_accepted.png"))

            # ---------------- E 手机窄屏 ----------------
            print("\n[E] 390px")
            # 再造一条待接收的，验证窄屏下三个按钮还能点
            node_m = upload(base, a_cookie, "手机上点同意.txt", b"mobile-body" * 40)
            http(
                base, "POST", "/api/quick/sends",
                {"nodeIds": [node_m["id"]], "toUsername": b_name}, a_cookie,
            )
            ctx_m, page_m, errors_m = make_page(b_token, 390, 844)
            page_m.wait_for_selector(".quick-row[data-send]", timeout=20000)
            pending_rows = page_m.locator(".quick-row.is-pending")
            check("窄屏有一条待接收", pending_rows.count() == 1, str(pending_rows.count()))
            mobile_buttons = pending_rows.locator(
                '[data-act="browse"], [data-act="accept"], [data-act="reject"]'
            )
            visible = [mobile_buttons.nth(i).is_visible() for i in range(mobile_buttons.count())]
            check("窄屏三个按钮都可见", len(visible) == 3 and all(visible), str(visible))
            overflow = page_m.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            check("窄屏不横向溢出", overflow <= 1, str(overflow))
            page_m.screenshot(path=str(OUT / "send_mobile.png"))
            check("[H1] 接收方无 JS 异常", not errors, "; ".join(errors[:3]))
            check("[H2] 手机端无 JS 异常", not errors_m, "; ".join(errors_m[:3]))
            ctx_m.close()
            ctx.close()

            # ---------------- F 发送方看到被拒绝 ----------------
            print("\n[F] 发送方：被拒绝提示")
            ctx_a, page_a, errors_a = make_page(a_token)
            page_a.wait_for_selector("[data-notice]", timeout=20000)
            notice = page_a.locator("[data-notice]").inner_text()
            check("提示条写明被拒绝", "被拒绝" in notice or "没有收下" in notice, notice)
            outbox_tags = page_a.locator("[data-outbox] .quick-row__tag").all_inner_texts()
            check("发件箱里有「被拒绝」标签", any("被拒绝" in text for text in outbox_tags), str(outbox_tags))
            check("发件箱里有「已存入」标签", any("已存入" in text for text in outbox_tags), str(outbox_tags))
            page_a.screenshot(path=str(OUT / "send_outbox_notice.png"))
            page_a.locator('[data-act="ack"]').click()
            page_a.wait_for_selector("[data-notice]", state="detached", timeout=15000)
            check("点「知道了」后提示消失", page_a.locator("[data-notice]").count() == 0)
            status, outbox = http(base, "GET", "/api/quick/outbox", None, a_cookie)
            check("服务端也不再提示", outbox.get("unseenResults") == 0, str(outbox.get("unseenResults")))

            # ---------------- G 设置 → 快传 ----------------
            print("\n[G] 设置 → 快传")
            page_a.goto(base + "/#/settings", wait_until="domcontentloaded")
            page_a.wait_for_selector("[data-settings-tab]", timeout=20000)
            page_a.locator('[data-settings-tab="quick"]').click()
            page_a.wait_for_selector('[data-settings-pane="quick"]', timeout=15000)
            pane = page_a.locator('[data-settings-pane="quick"]')
            check("允许接收开关默认开", pane.locator('[data-toggle="quickReceiveEnabled"]').is_checked())
            check("自动同意默认关", not pane.locator('[data-toggle="quickAutoAccept"]').is_checked())
            path_text = pane.locator("[data-quick-folder-path]").inner_text()
            check("默认路径是 我的文件 / 快传的文件", "快传的文件" in path_text, path_text)
            page_a.screenshot(path=str(OUT / "settings_quick.png"))

            pane.locator('[data-act="quick-folder"]').click()
            page_a.wait_for_selector(".picker", timeout=15000)
            row = page_a.locator(".picker__row", has_text="收件文件夹").first
            if row.count():
                row.click()
                page_a.wait_for_timeout(300)
            page_a.locator('.picker [data-act="ok"]').click()
            page_a.wait_for_timeout(800)
            path_text = pane.locator("[data-quick-folder-path]").inner_text()
            check("选完目录后路径更新", "收件文件夹" in path_text or "快传的文件" in path_text, path_text)
            if pane.locator('[data-act="quick-folder-reset"]').is_visible():
                pane.locator('[data-act="quick-folder-reset"]').click()
                page_a.wait_for_timeout(800)
                check(
                    "恢复默认后按钮重新隐藏",
                    not pane.locator('[data-act="quick-folder-reset"]').is_visible(),
                )
            else:
                check("选中目录后出现「恢复默认」", False, "按钮没出现")

            # 关掉接收开关（开关本体是隐藏的 input，点 label 才触发）
            page_a.locator('label.switch:has([data-toggle="quickReceiveEnabled"])').click()
            page_a.wait_for_timeout(500)
            status, pref = http(base, "GET", "/api/quick/receive", None, a_cookie)
            check("关掉接收后服务端同步", pref["receive"]["enabled"] is False, str(pref["receive"]))
            page_a.locator('label.switch:has([data-toggle="quickReceiveEnabled"])').click()
            page_a.wait_for_timeout(500)
            check("[H3] 设置页无 JS 异常", not errors_a, "; ".join(errors_a[:3]))
            page_a.screenshot(path=str(OUT / "settings_quick_custom.png"))
            ctx_a.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果：{passed} 通过，{failed} 失败")
    print(f"截图：{OUT}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
