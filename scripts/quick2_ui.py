# -*- coding: utf-8 -*-
"""快传取件码新交互 + 我的分享分页 的 UI 冒烟。

  [A] 快传 → 取件码：选本机文件上传（多选）、逐文件设置有效期/次数、生成取件码；
      上传的快传文件不进「我的文件」，也不占配额
  [B] 登录状态下输入取件码 → 看到清单 → 全部转存到网盘
  [C] 我的分享：默认每页 10 条、可翻页、可改每页条数；失效记录能「重新分享 / 删除记录」
  [D] 控制台无 JS 异常

    ./.venv/Scripts/python.exe scripts/quick2_ui.py
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
        req.add_header("Cookie", f"cloudnetp_session={cookie}")
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

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-quick2-ui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)

    con = sqlite3.connect(str(db_copy))
    for stmt in ("delete from quick_transfers", "delete from quick_pickup_items"):
        try:
            con.execute(stmt)
        except sqlite3.OperationalError:
            pass
    admin = con.execute(
        "select id, username, token_version from users where role = 1 order by id limit 1"
    ).fetchone()
    if not admin:
        raise SystemExit("库里没有管理员")
    con.execute("update users set password_hash=? where id=?", (hash_password(PASSWORD), admin[0]))
    try:
        con.execute("update user_settings set quick_terms_at=datetime('now') where user_id=?", (admin[0],))
    except sqlite3.OperationalError:
        pass
    con.execute("delete from shares")
    con.commit()
    con.close()

    session_token = create_token(admin[0], admin[2])

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

        stamp = str(int(time.time()))
        sample_a = tmp / f"取件上传A-{stamp}.txt"
        sample_a.write_bytes(f"pickup-upload-a-{stamp}".encode() * 400)
        sample_b = tmp / f"取件上传B-{stamp}.txt"
        sample_b.write_bytes(f"pickup-upload-b-{stamp}".encode() * 300)

        # 先造几条分享，供分页/失效记录使用
        st, listing = http(base, "GET", "/api/nodes", None, session_token)
        target = listing["items"][0]
        share_ids = []
        for _ in range(12):
            st, data = http(base, "POST", "/api/shares", {"nodeId": target["id"], "expiresDays": 7}, session_token)
            if st == 200:
                share_ids.append(data["share"]["id"])
        dead_id = share_ids[0]
        http(base, "DELETE", f"/api/shares/{dead_id}", None, session_token)

        st, me = http(base, "GET", "/api/me", None, session_token)
        used_before = me["user"]["usedBytes"]

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)

            ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
            ctx.add_cookies([{"name": "cloudnetp_session", "value": session_token, "url": base}])
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on(
                "console",
                lambda msg: errors.append(msg.text)
                if msg.type == "error" and "Failed to load resource" not in msg.text
                else None,
            )

            # ---------------- A 上传本机文件生成取件码 ----------------
            print("\n[A] 取件码：上传本机文件 + 逐文件设置")
            page.goto(base + "/#/quick", wait_until="domcontentloaded")
            # 看板娘固定在右下角，会挡住卡片右缘的按钮；交互测试里先藏起来
            # （不能 remove：live2d 还拿着它的引用，删了会报错）
            page.evaluate("() => { const m = document.getElementById('mascot'); if (m) m.style.visibility = 'hidden'; }")
            page.wait_for_selector(".quick-gate, .quick-tabs", timeout=20000)
            if page.locator(".quick-gate").count():
                page.locator(".quick-agree").click()
                page.locator("[data-accept]").click()
                page.wait_for_selector(".quick-tabs", timeout=20000)
            page.wait_for_selector('.quick-tab[data-tab="pickup"]', timeout=20000)
            page.locator('.quick-tab[data-tab="pickup"]').click()
            page.wait_for_selector(".quick-pick-drop", timeout=20000)
            page.set_input_files("[data-file-input]", [str(sample_a), str(sample_b)])
            page.wait_for_selector(".quick-item-row", timeout=30000)
            page.wait_for_function(
                "() => document.querySelectorAll('.quick-item-row').length === 2", timeout=30000
            )
            check("两个文件都进了待生成列表", page.locator(".quick-item-row").count() == 2)
            check("上传区提示不占空间", "不占你的网盘空间" in page.locator(".quick-pick-drop").inner_text())
            rows = page.locator(".quick-item-row")
            check(
                "每一行都有自己的设置",
                rows.first.locator("[data-item-days]").count() == 1
                and rows.nth(1).locator("[data-item-uses]").count() == 1,
            )
            rows.first.locator("[data-item-days]").select_option("1")
            rows.first.locator("[data-item-uses]").select_option("1")
            page.screenshot(path=str(OUT / "quick2_upload.png"), full_page=False)

            page.locator('[data-act="new"]').click()
            page.wait_for_selector("[data-code]", timeout=20000)
            code = page.locator("[data-code]").inner_text().strip()
            check("取件码 6 位", len(code) == 6 and code.isalnum(), code)
            page.screenshot(path=str(OUT / "quick2_created.png"), full_page=False)

            st, me2 = http(base, "GET", "/api/me", None, session_token)
            check("快传上传不占配额", me2["user"]["usedBytes"] == used_before, f"{used_before} -> {me2['user']['usedBytes']}")
            st, nodes = http(base, "GET", "/api/nodes", None, session_token)
            names = [item["name"] for item in nodes.get("items") or []]
            check("快传文件不在我的文件里", sample_a.name not in names and sample_b.name not in names)

            # ---------------- B 登录后输入取件码取件 ----------------
            print("\n[B] 登录取件与转存")
            page.fill("[data-code-input]", code)
            page.locator('[data-act="lookup"]').click()
            page.wait_for_selector(".quick-lookup", timeout=20000)
            lookup_text = page.locator(".quick-lookup").inner_text()
            check("列出来源与文件", "来自" in lookup_text and sample_a.name in lookup_text)
            check("显示逐文件剩余次数", "还能取 1 次" in lookup_text or "下载次数已用完" in lookup_text)
            page.screenshot(path=str(OUT / "quick2_lookup.png"), full_page=False)

            page.locator('.quick-lookup [data-act="save"]').click()
            page.wait_for_selector(".picker", timeout=15000)
            page.locator('.picker__foot [data-act="ok"]').click()
            page.wait_for_selector(".toast", timeout=20000)
            toast = " ".join(page.locator(".toast").all_inner_texts())
            check("转存提示", "已转存" in toast, toast[:60])
            st, nodes = http(base, "GET", "/api/nodes", None, session_token)
            names = [item["name"] for item in nodes.get("items") or []]
            check("文件进了我的文件", sample_a.name in names and sample_b.name in names, str(names[:6]))

            # ---------------- C 我的分享分页 ----------------
            print("\n[C] 我的分享分页与失效记录")
            page.locator('[data-route="shares"]').click()
            page.wait_for_selector(".share-row", timeout=20000)
            check("默认每页 10 条", page.locator(".share-row").count() == 10, str(page.locator(".share-row").count()))
            check("有分页信息", "第 1/" in page.locator(".share-pager__info").inner_text(), page.locator(".share-pager__info").inner_text())
            page.screenshot(path=str(OUT / "quick2_shares.png"), full_page=False)

            page.locator('[data-page="next"]').click()
            page.wait_for_function(
                "() => document.querySelector('.share-pager__info')?.textContent.includes('第 2/')",
                timeout=20000,
            )
            check("可以翻到第 2 页", "第 2/" in page.locator(".share-pager__info").inner_text())
            page.locator("[data-page-size]").select_option("20")
            page.wait_for_function(
                "() => document.querySelectorAll('.share-row').length > 10", timeout=20000
            )
            check("每页改成 20 条生效", page.locator(".share-row").count() > 10, str(page.locator(".share-row").count()))
            stored = page.evaluate("() => localStorage.getItem('cloudnetp.sharePageSize')")
            check("每页条数被记住", stored == "20", str(stored))

            # 改每页条数会回到第 1 页，这里直接找那条已取消的记录
            page.wait_for_function(
                "() => document.querySelector('.share-pager__info')?.textContent.includes('第 1/')",
                timeout=20000,
            )
            page.wait_for_timeout(600)
            dead_row = page.locator(".share-row.is-dead").first
            check("失效记录有标记", dead_row.count() >= 1)
            check("失效记录能重新分享", dead_row.locator('[data-act="renew"]').count() == 1)
            check("失效记录能删除", dead_row.locator('[data-act="forget"]').count() == 1)
            page.screenshot(path=str(OUT / "quick2_shares_dead.png"), full_page=False)

            # ---------------- D 后台快传设置卡片 ----------------
            print("\n[D] 后台快传设置")
            page.goto(base + "/#/admin", wait_until="domcontentloaded")
            page.wait_for_selector('[data-tab="settings"]', timeout=30000)
            page.click('[data-tab="settings"]')
            page.wait_for_selector('[data-card="quick"]', timeout=30000)
            card = page.locator('[data-card="quick"]')
            check("有快传设置卡片", card.count() == 1)
            check(
                "三个上限输入都在",
                card.locator('[data-key="quickMaxDays"]').count() == 1
                and card.locator('[data-key="quickMaxUses"]').count() == 1
                and card.locator('[data-key="quickRetentionDays"]').count() == 1,
            )
            check("有立即清理按钮", card.locator('[data-act="quick-cleanup"]').count() == 1)
            check("显示当前快传概况", "个快传文件" in card.inner_text(), card.inner_text()[:80])
            card.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            page.screenshot(path=str(OUT / "quick2_admin.png"), full_page=False)

            # ---------------- E 控制台 ----------------
            print("\n[E] 控制台")
            check("无 JS 异常", not errors, "; ".join(errors[:3]))
            ctx.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_file.close()

    print(f"\n结果：{passed} 通过，{failed} 失败")
    if failed == 0:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print("临时目录保留：", tmp)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
