# -*- coding: utf-8 -*-
"""上传面板 UI 冒烟：开关上传列表 + 失败重试。

用库副本起独立实例，Playwright 驱动本机 Chrome：

  [A] 上传后出现面板；点「开关上传列表」只留标题栏，再点展开
  [B] 让 init 接口失败 → 条目变红并出现重试按钮 → 恢复后点重试，文件真的进网盘
  [C] 收起状态按设备记住（localStorage + 重新进页面后新上传仍是从收起开始）
      注：上传失败时会自动展开一次，所以这里用「成功的上传」验证记忆
  [D] 390px 窄屏：面板不横向溢出，开关按钮仍可点

    ./.venv/Scripts/python.exe scripts/upload_panel_ui.py
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

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-upload-ui-")).resolve()
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
        # 每次跑都用不同内容，避免秒传把上传流程跳过
        ok_file = tmp / f"面板样本-{stamp}.txt"
        ok_file.write_bytes(f"panel-payload-{stamp}".encode() * 200)
        retry_file = tmp / f"重试样本-{stamp}.txt"
        retry_file.write_bytes(f"retry-payload-{stamp}".encode() * 200)
        keep_file = tmp / f"收起样本-{stamp}.txt"
        keep_file.write_bytes(f"collapse-payload-{stamp}".encode() * 200)
        keep2_file = tmp / f"收起样本2-{stamp}.txt"
        keep2_file.write_bytes(f"collapse2-payload-{stamp}".encode() * 200)

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

            fail_route = "**/api/uploads/init"
            fail_body = json.dumps({"detail": "模拟上传失败"})

            def fail_init(page):
                page.route(
                    fail_route,
                    lambda route: route.fulfill(
                        status=500, content_type="application/json", body=fail_body
                    ),
                )

            # 用「上传失败」的条目来测面板：失败条目会一直留在列表里，
            # 而成功的条目 2.6 秒后会被收走，测点击容易扑空。
            print("\n[A] 上传面板与开关")
            ctx, page, errors = make_page(session_token)
            fail_init(page)
            page.goto(base + "/#/files", wait_until="domcontentloaded")
            page.wait_for_selector("[data-file-input]", state="attached", timeout=20000)
            page.set_input_files("[data-file-input]", str(ok_file))
            page.wait_for_selector(".upload-item.is-error", timeout=30000)
            toggle = page.locator(".upload-panel [data-toggle]")
            check("面板有开关按钮", toggle.count() == 1, str(toggle.count()))
            check("开关不带文字", toggle.inner_text().strip() == "", repr(toggle.inner_text()))
            check(
                "默认展开（列表可见）",
                page.locator(".upload-panel__list").is_visible()
                and toggle.get_attribute("aria-expanded") == "true",
            )
            page.screenshot(path=str(OUT / "upload_panel_open.png"), full_page=False)

            toggle.click()
            page.wait_for_selector(".upload-panel.is-collapsed", timeout=5000)
            check("点一下收起", not page.locator(".upload-panel__list").is_visible())
            check("收起后 aria 变成 false", toggle.get_attribute("aria-expanded") == "false")
            check("收起后标题还在", page.locator("[data-upload-title]").inner_text().strip() != "")
            check("按钮提示改成展开", "展开" in (toggle.get_attribute("title") or ""), toggle.get_attribute("title"))
            page.screenshot(path=str(OUT / "upload_panel_collapsed.png"), full_page=False)

            toggle.click()
            page.wait_for_selector(".upload-panel:not(.is-collapsed)", timeout=5000)
            check("再点展开", page.locator(".upload-panel__list").is_visible())

            print("\n[B] 失败重试")
            row = page.locator(".upload-item.is-error").first
            check("失败条目变红", row.count() == 1)
            retry = row.locator("[data-retry]")
            check("失败条目出现重试按钮", retry.count() == 1 and retry.is_visible())
            page.screenshot(path=str(OUT / "upload_panel_error.png"), full_page=False)

            page.unroute(fail_route)
            retry.click()
            page.wait_for_selector(".upload-item.is-done", timeout=60000)
            check("重试后变成已完成", page.locator(".upload-item.is-done").count() >= 1)
            page.wait_for_selector(".upload-item.is-error", state="detached", timeout=15000)

            st, listing = http(base, "GET", "/api/nodes", None, cookie)
            names = [item["name"] for item in listing.get("items") or []]
            check("重试后文件真的进网盘", ok_file.name in names, str(names[:6]))

            print("\n[C] 收起状态按设备记住")
            fail_init(page)
            page.set_input_files("[data-file-input]", str(keep_file))
            page.wait_for_selector(".upload-item.is-error", timeout=30000)
            page.locator(".upload-panel [data-toggle]").click()
            page.wait_for_selector(".upload-panel.is-collapsed", timeout=5000)
            stored = page.evaluate("() => localStorage.getItem('cloudnetp.uploadCollapsed')")
            check("收起写进 localStorage", stored == "1", str(stored))

            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("[data-file-input]", state="attached", timeout=20000)
            page.unroute(fail_route)
            page.set_input_files("[data-file-input]", str(keep2_file))
            page.wait_for_selector(".upload-panel", timeout=30000)
            check(
                "重进页面后新上传仍是从收起开始",
                "is-collapsed" in (page.locator(".upload-panel").get_attribute("class") or ""),
                page.locator(".upload-panel").get_attribute("class"),
            )
            check(
                "记忆里的箭头状态也对",
                page.locator(".upload-panel [data-toggle]").get_attribute("aria-expanded") == "false",
            )
            page.locator(".upload-panel [data-toggle]").click()
            page.wait_for_selector(".upload-panel:not(.is-collapsed)", timeout=5000)
            stored = page.evaluate("() => localStorage.getItem('cloudnetp.uploadCollapsed')")
            check("展开也写回 localStorage", stored == "0", str(stored))

            print("\n[D] 390px 窄屏")
            ctx4, page4, errors4 = make_page(session_token, width=390, height=844)
            page4.goto(base + "/#/files", wait_until="domcontentloaded")
            page4.wait_for_selector("[data-file-input]", state="attached", timeout=20000)
            page4.evaluate("() => localStorage.setItem('cloudnetp.uploadCollapsed', '0')")
            fail_init(page4)
            page4.set_input_files("[data-file-input]", str(keep_file))
            page4.wait_for_selector(".upload-panel .upload-item", timeout=30000)
            metrics = page4.evaluate(
                """() => {
                    const panel = document.querySelector('.upload-panel');
                    const box = panel.getBoundingClientRect();
                    return {
                        doc: document.documentElement.scrollWidth,
                        win: innerWidth,
                        panelRight: box.right,
                        panelLeft: box.left,
                    };
                }"""
            )
            check(
                "面板在视口内",
                metrics["panelLeft"] >= 0 and metrics["panelRight"] <= metrics["win"] + 1,
                str(metrics),
            )
            check("页面不横向溢出", metrics["doc"] <= metrics["win"] + 1, str(metrics))
            page4.locator(".upload-panel [data-toggle]").click()
            page4.wait_for_selector(".upload-panel.is-collapsed", timeout=5000)
            check("窄屏也能收起", not page4.locator(".upload-panel__list").is_visible())
            page4.screenshot(path=str(OUT / "upload_panel_mobile.png"), full_page=False)

            print("\n[E] 控制台无报错")
            for name, errs in (("宽屏", errors), ("窄屏", errors4)):
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
