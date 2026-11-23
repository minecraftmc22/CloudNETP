# -*- coding: utf-8 -*-
"""手机端布局回归：把「后台在窄屏被撑宽 / 页签挤成一团 / 举报行没有操作按钮」
这几类问题钉死。

用法：
    python scripts/mobile_admin_ui.py [port]

需要一个已启动的实例（建议用隔离库），脚本自己签管理员会话。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from app import db_failover  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import User  # noqa: E402
from app.security import create_token  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
W, H = 390, 844

CHECKS: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> None:
    CHECKS.append((bool(ok), msg))


def admin_token() -> str:
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    try:
        admin = db.query(User).filter(User.role == 1).order_by(User.id.asc()).first()
        assert admin is not None, "没有管理员账号"
        return create_token(admin.id, admin.token_version)
    finally:
        db.close()


def main() -> None:
    token = admin_token()
    profile = Path(tempfile.mkdtemp(prefix="cdp_mobile_ui_"))
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=9477",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--window-size={W},{H}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(80):
            try:
                pages = [
                    t
                    for t in json.load(urllib.request.urlopen("http://127.0.0.1:9477/json/list", timeout=2))
                    if t.get("type") == "page"
                ]
                if pages:
                    target = pages[0]
                    break
            except Exception:
                time.sleep(0.4)
        assert target, "无法连接 Chrome CDP"
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
        mid = 0

        def send(method, **params):
            nonlocal mid
            mid += 1
            ws.send(json.dumps({"id": mid, "method": method, "params": params}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error']}")
                    return msg.get("result", {})

        def js(expr):
            return send("Runtime.evaluate", expression=expr, returnByValue=True, userGesture=True)["result"].get("value")

        def wait_for(expr, timeout=25):
            end = time.time() + timeout
            while time.time() < end:
                try:
                    if js(expr):
                        return True
                except Exception:
                    pass
                time.sleep(0.3)
            return False

        send("Page.enable")
        send("Runtime.enable")
        send("Network.enable")
        send("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=2, mobile=True)
        send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        send("Network.setCookie", name=settings.COOKIE_NAME, value=token, domain="127.0.0.1", path="/", httpOnly=True)
        send("Page.navigate", url=f"{BASE}/#/files")
        assert wait_for("!!document.querySelector('#view-root')", 30), "首页没渲染出来"
        time.sleep(1.5)

        # 1) 每个视图都不许横向撑宽（innerWidth 被撑大 = Chrome 判定内容超宽，肉眼就是「页面能左右拖」）
        width_js = "({ iw: innerWidth, sw: document.documentElement.scrollWidth })"
        for name, hash_, ready in (
            ("我的文件", "#/files", ".file-grid, .frow, .empty"),
            ("我的分享", "#/shares", ".share-row, .empty, .view-body .card"),
            ("回收站", "#/trash", ".frow, .empty, .view-body"),
            ("设置", "#/settings", ".settings"),
            ("关于", "#/about", ".about-hero"),
            ("意见反馈", "#/feedback", ".view-body"),
            ("快传", "#/quick", ".view-body"),
        ):
            js(f"location.hash = {json.dumps(hash_)}")
            wait_for(f"!!document.querySelector({json.dumps(ready)})")
            time.sleep(1.0)
            info = js(width_js)
            check(
                info["iw"] == W and info["sw"] <= W + 1,
                f"{name} 未横向溢出（innerWidth={info['iw']} scrollWidth={info['sw']}）",
            )

        js("location.hash = '#/admin'")
        assert wait_for("!!document.querySelector('[data-tab]')", 30), "后台没渲染"
        time.sleep(1.5)

        info = js(width_js)
        check(
            info["iw"] == W and info["sw"] <= W + 1,
            f"后台概览未横向溢出（innerWidth={info['iw']} scrollWidth={info['sw']}）",
        )

        # 2) 页签：文字不能被压到溢出按钮（等分压缩时「举报审核」会叠在一起）
        tabs = js(
            """
            (() => {
              const btns = [...document.querySelectorAll('.admin-tabs [data-tab]')];
              const clipped = btns.filter((b) => b.scrollWidth > b.clientWidth + 1).map((b) => b.textContent.trim());
              const overlap = [];
              for (let i = 1; i < btns.length; i += 1) {
                if (btns[i].getBoundingClientRect().left < btns[i - 1].getBoundingClientRect().right - 1) {
                  overlap.push(btns[i].textContent.trim());
                }
              }
              const wrap = document.querySelector('.admin-tabs');
              return {
                n: btns.length,
                clipped,
                overlap,
                scrollable: wrap.scrollWidth > wrap.clientWidth + 1,
                thumbVisible: (() => {
                  const t = document.querySelector('.segmented__thumb');
                  return !!t && t.getBoundingClientRect().width > 10;
                })(),
              };
            })()
            """
        )
        check(tabs["n"] >= 9, f"后台页签齐全（{tabs['n']} 个）")
        check(not tabs["clipped"], f"页签文字未被压窄（{tabs['clipped']}）")
        check(not tabs["overlap"], f"页签互不重叠（{tabs['overlap']}）")
        check(tabs["scrollable"], "页签条可横向滚动（窄屏放不下时）")
        check(tabs["thumbVisible"], "页签滑块仍然可见")

        # 3) 页签切换后当前项要滚进视野
        js("document.querySelector('[data-tab=\"settings\"]').click()")
        time.sleep(1.2)
        in_view = js(
            """
            (() => {
              const b = document.querySelector('[data-tab="settings"]');
              const w = document.querySelector('.admin-tabs');
              const rb = b.getBoundingClientRect();
              const rw = w.getBoundingClientRect();
              return rb.left >= rw.left - 1 && rb.right <= rw.right + 1;
            })()
            """
        )
        check(in_view, "切到「系统设置」后页签自动滚进视野")

        # 4) 用户行：配额与状态不能整块消失
        js("document.querySelector('[data-tab=\"users\"]').click()")
        time.sleep(2.0)
        users = js(
            """
            (() => {
              const row = document.querySelector('.admin-row[data-id]');
              if (!row) return { empty: true };
              const vis = (sel) => {
                const el = row.querySelector(sel);
                if (!el) return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && getComputedStyle(el).display !== 'none';
              };
              const status = row.children[4];
              const sr = status ? status.getBoundingClientRect() : null;
              return {
                empty: false,
                space: vis('.admin-space'),
                status: !!sr && sr.width > 0 && getComputedStyle(status).display !== 'none',
                menu: vis('[data-menu]'),
                overflowX: document.documentElement.scrollWidth,
              };
            })()
            """
        )
        if users.get("empty"):
            print("（用户表为空，跳过用户行检查）")
        else:
            check(users["space"], "用户行显示配额进度")
            check(users["status"], "用户行显示状态徽章")
            check(users["menu"], "用户行保留更多操作按钮")

        # 5) 举报行：手机上没有操作按钮等于不能审核
        js("document.querySelector('[data-tab=\"reports\"]').click()")
        time.sleep(2.2)
        reports = js(
            """
            (() => {
              const row = document.querySelector('.admin-row--report[data-id]');
              if (!row) return { empty: true };
              const btn = row.querySelector('[data-approve]');
              const hash = row.children[1];
              const reason = row.children[2];
              const r = btn ? btn.getBoundingClientRect() : null;
              return {
                empty: false,
                approveVisible: !!r && r.width > 0 && r.height > 0,
                hashVisible: !!hash && getComputedStyle(hash).display !== 'none',
                reasonVisible: !!reason && getComputedStyle(reason).display !== 'none',
                overflowX: document.documentElement.scrollWidth,
              };
            })()
            """
        )
        if reports.get("empty"):
            print("（举报列表为空，跳过举报行检查）")
        else:
            check(reports["approveVisible"], "举报行有可见的「通过并拉黑」按钮")
            check(reports["hashVisible"], "举报行显示哈希列")
            check(reports["reasonVisible"], "举报行显示举报理由")
            check(reports["overflowX"] <= W + 1, f"举报行不撑宽页面（scrollWidth={reports['overflowX']}）")

        # 6) 审计日志：时间不能只剩「2026-10-29 18…」
        js("document.querySelector('[data-tab=\"logs\"]').click()")
        time.sleep(2.2)
        logs = js(
            """
            (() => {
              const el = document.querySelector('.log-time');
              if (!el) return { empty: true };
              return {
                empty: false,
                clipped: el.scrollWidth > el.clientWidth + 1,
                text: el.textContent.trim(),
              };
            })()
            """
        )
        if logs.get("empty"):
            print("（审计日志为空，跳过日志行检查）")
        else:
            check(not logs["clipped"], f"日志时间完整显示（{logs['text']}）")

        ws.close()
    finally:
        proc.terminate()

    bad = [c for c in CHECKS if not c[0]]
    for ok, msg in CHECKS:
        print(("  ✓ " if ok else "  ✗ ") + msg)
    print(f"\n{len(CHECKS) - len(bad)}/{len(CHECKS)} 通过")
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
