# -*- coding: utf-8 -*-
"""上传队列 UI 冒烟：切页继续传、暂停/继续、加载图标。

用库副本起独立实例，Playwright 驱动本机 Chrome，并用 CDP 限速把上传拖慢到能观察：

  [A] 上传中点侧栏切到别的页面：面板还在、进度继续，传完后回「我的文件」能看到文件
  [B] 暂停：条目变成「已暂停」、进度停住；继续后接着传完，文件真的进网盘
  [B2] 标题栏「全部暂停 / 全部继续」一次处理整队
  [C] 进入子目录时列表上出现加载图标，加载完消失
  [D] 控制台无 JS 异常

    ./.venv/Scripts/python.exe scripts/upload_background_ui.py
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


def throttle(cdp, upload_bps: int = -1, latency: int = 0) -> None:
    cdp.send(
        "Network.emulateNetworkConditions",
        {
            "offline": False,
            "latency": latency,
            "downloadThroughput": -1,
            "uploadThroughput": upload_bps,
            "connectionType": "cellular3g",
        },
    )


def main() -> int:
    from app.config import settings
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-bg-ui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)

    con = sqlite3.connect(str(db_copy))
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

        stamp = str(int(time.time()))
        # 每次内容都不同，避免秒传跳过上传流程
        bg_file = tmp / f"后台样本-{stamp}.bin"
        bg_file.write_bytes(os.urandom(1500 * 1024))
        pause_file = tmp / f"暂停样本-{stamp}.bin"
        pause_file.write_bytes(os.urandom(1200 * 1024))
        batch_a = tmp / f"整队样本A-{stamp}.bin"
        batch_a.write_bytes(os.urandom(900 * 1024))
        batch_b = tmp / f"整队样本B-{stamp}.bin"
        batch_b.write_bytes(os.urandom(900 * 1024))

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

            cdp = ctx.new_cdp_session(page)
            cdp.send("Network.enable")

            page.goto(base + "/#/files", wait_until="domcontentloaded")
            page.wait_for_selector("[data-file-input]", state="attached", timeout=20000)

            # ---------------- A 切页继续上传 ----------------
            print("\n[A] 切到别的页面仍在上传")
            throttle(cdp, upload_bps=260 * 1024)
            page.set_input_files("[data-file-input]", str(bg_file))
            page.wait_for_selector(".upload-item.is-active", timeout=20000)
            started = page.locator("[data-upload-title]").inner_text()
            check("面板标题显示正在上传", "正在上传" in started, started)

            page.click('[data-route="shares"]')
            page.wait_for_selector(".view-title", timeout=20000)
            time.sleep(1.6)
            panel_on_other = page.locator(".upload-panel").count()
            check("切页后面板还在", panel_on_other == 1, str(panel_on_other))
            check(
                "切页后仍在传",
                page.locator(".upload-item.is-active").count() >= 1,
                page.locator("[data-upload-title]").inner_text(),
            )
            check(
                "侧栏已切到我的分享",
                "分享" in page.locator(".view-title").inner_text(),
                page.locator(".view-title").inner_text(),
            )
            page.screenshot(path=str(OUT / "upload_bg_other_view.png"), full_page=False)

            page.wait_for_selector(".upload-item.is-done, .upload-item.is-error", timeout=90000)
            done_state = page.locator(".upload-item [data-state]").first.inner_text()
            check("在别的页面也会传完", done_state.strip() in ("完成", "秒传"), done_state)
            throttle(cdp, upload_bps=-1)

            page.click('[data-route="files"]')
            page.wait_for_selector(".tile, .frow", timeout=20000)
            page.wait_for_timeout(800)
            st, listing = http(base, "GET", "/api/nodes", None, cookie)
            names = [item["name"] for item in listing.get("items") or []]
            check("回到我的文件能看到文件", bg_file.name in names, str(names[:6]))
            check("列表里也渲染出来了", page.locator(f"text={bg_file.name}").count() >= 1)

            # ---------------- B 暂停 / 继续 ----------------
            print("\n[B] 暂停与继续")
            throttle(cdp, upload_bps=240 * 1024)
            page.set_input_files("[data-file-input]", str(pause_file))
            page.wait_for_selector(".upload-item.is-active [data-pause]", timeout=20000)
            page.wait_for_timeout(1200)
            row = page.locator(".upload-item").filter(has_text=pause_file.name).first
            row.locator("[data-pause]").click()
            page.wait_for_selector(".upload-item.is-paused", timeout=15000)
            check("条目变成已暂停", row.locator("[data-state]").inner_text().strip() == "已暂停")
            check("标题显示已暂停", "已暂停" in page.locator("[data-upload-title]").inner_text(),
                  page.locator("[data-upload-title]").inner_text())
            frozen = row.locator("[data-state]").inner_text()
            pct_before = row.evaluate(
                "el => (el.querySelector('[data-fill]')?.style.width) || ''"
            )
            page.screenshot(path=str(OUT / "upload_bg_paused.png"), full_page=False)

            time.sleep(2.0)
            pct_after = row.evaluate(
                "el => (el.querySelector('[data-fill]')?.style.width) || ''"
            )
            check("暂停后进度不再增长", pct_before == pct_after, f"{pct_before} -> {pct_after}")
            check("暂停后仍是已暂停", row.locator("[data-state]").inner_text() == frozen, frozen)

            row.locator("[data-pause]").click()
            page.wait_for_selector(".upload-item.is-active", timeout=15000)
            check("点继续后在传", row.locator("[data-state]").inner_text() not in ("已暂停", "排队中"))
            page.wait_for_selector(".upload-item.is-done", timeout=90000)
            page.wait_for_timeout(400)
            st, listing = http(base, "GET", "/api/nodes", None, cookie)
            names = [item["name"] for item in listing.get("items") or []]
            check("继续后文件真的进网盘", pause_file.name in names, str(names[:6]))
            throttle(cdp, upload_bps=-1)

            # ---------------- B2 全部暂停 / 全部继续 ----------------
            print("\n[B2] 全部暂停与全部继续")
            throttle(cdp, upload_bps=240 * 1024)
            page.set_input_files("[data-file-input]", [str(batch_a), str(batch_b)])
            page.wait_for_selector(".upload-item.is-active", timeout=20000)
            page.wait_for_timeout(600)
            page.locator("[data-pause-all]").click()
            page.wait_for_selector(".upload-item.is-paused", timeout=20000)
            page.wait_for_function(
                "() => document.querySelectorAll('.upload-item.is-paused').length === 2", timeout=20000
            )
            check("全部暂停后两条都停住", page.locator(".upload-item.is-paused").count() == 2)
            check(
                "按钮变成全部继续",
                page.locator("[data-pause-all]").inner_text().strip() == "全部继续",
                page.locator("[data-pause-all]").inner_text(),
            )
            page.locator("[data-pause-all]").click()
            page.wait_for_selector(".upload-item.is-done", timeout=90000)
            page.wait_for_function(
                "() => document.querySelectorAll('.upload-item.is-done').length === 2", timeout=90000
            )
            check("全部继续后两个都传完", page.locator(".upload-item.is-done").count() == 2)
            st, listing = http(base, "GET", "/api/nodes", None, cookie)
            names = [item["name"] for item in listing.get("items") or []]
            check(
                "两个文件都进网盘",
                batch_a.name in names and batch_b.name in names,
                str(names[:8]),
            )
            throttle(cdp, upload_bps=-1)

            # ---------------- C 加载图标 ----------------
            print("\n[C] 切换目录时的加载图标")
            page.wait_for_selector(".tile[data-type='folder']", timeout=20000)
            first_folder = page.locator(".tile[data-type='folder']").first
            folder_name = first_folder.locator(".tile__name").inner_text()
            throttle(cdp, latency=1500)
            first_folder.dblclick()
            page.wait_for_selector(".files-loading:not([hidden])", timeout=5000)
            check("进目录时出现加载图标", page.locator(".files-loading .spinner").count() == 1)
            page.screenshot(path=str(OUT / "upload_bg_loading.png"), full_page=False)
            throttle(cdp, latency=0)
            page.wait_for_selector(".files-loading[hidden]", state="attached", timeout=20000)
            check("加载完图标消失", page.locator(".files-loading").get_attribute("hidden") is not None)
            check("已经进入目录", page.locator(".crumb__item.is-current").inner_text() == folder_name,
                  page.locator(".crumb__item.is-current").inner_text())

            # ---------------- D 控制台 ----------------
            print("\n[D] 控制台")
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
