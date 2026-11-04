# -*- coding: utf-8 -*-
"""管理后台「用户」页截图：验证头像与昵称的展示效果。

用项目自身的 SECRET_KEY 直接签一个管理员会话 Cookie，避免依赖账号密码。
"""
from __future__ import annotations

import base64
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

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)


def admin_token() -> str:
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    try:
        admin = db.query(User).filter(User.role == 1).order_by(User.id.asc()).first()
        assert admin is not None, "没有管理员账号"
        print(f"用管理员 #{admin.id} {admin.username} 的会话")
        return create_token(admin.id, admin.token_version)
    finally:
        db.close()


def main() -> None:
    token = admin_token()

    profile = Path(tempfile.mkdtemp(prefix="cdp_admin_users_"))
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=9443",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--hide-scrollbars",
            "--window-size=1280,980",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(60):
            try:
                pages = [
                    t
                    for t in json.load(urllib.request.urlopen("http://127.0.0.1:9443/json/list", timeout=2))
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

        def shot(name):
            res = send("Page.captureScreenshot", format="png")
            path = OUT / name
            path.write_bytes(base64.b64decode(res["data"]))
            print("截图", path)

        def wait_for(expr, timeout=20):
            end = time.time() + timeout
            while time.time() < end:
                if js(expr):
                    return True
                time.sleep(0.3)
            return False

        send("Page.enable")
        send("Runtime.enable")
        send("Network.enable")
        send("Emulation.setDeviceMetricsOverride", width=1280, height=980, deviceScaleFactor=1, mobile=False)

        send(
            "Network.setCookie",
            name=settings.COOKIE_NAME,
            value=token,
            domain="127.0.0.1",
            path="/",
            httpOnly=True,
        )

        send("Page.navigate", url=f"{BASE}/#/admin")
        assert wait_for("!!document.querySelector('[data-tab=\"users\"]')", 25), "后台没渲染出来"
        js("document.querySelector('[data-tab=\"users\"]').click()")
        ok = wait_for("document.querySelectorAll('.admin-row[data-id]').length > 0", 20)
        if not ok:
            print("页面文字:", js("document.body.innerText.slice(0, 600)"))
            print("表格 HTML:", js("(document.querySelector('[data-table]')||{}).innerHTML?.slice(0,600)"))
        assert ok, "用户列表没加载"

        time.sleep(0.8)
        shot("admin_users.png")

        # 头像加载情况：src 是否为 /api/avatar/...，以及图片是否真的解码成功
        report = js(
            """
            (() => {
              const rows = [...document.querySelectorAll('.admin-row[data-id]')];
              return rows.map((row) => {
                const img = row.querySelector('.admin-avatar img');
                const name = row.querySelector('.admin-user__name')?.textContent.trim() || '';
                const meta = row.querySelector('.admin-user__meta')?.textContent.trim() || '';
                return {
                  id: row.dataset.id,
                  name,
                  meta,
                  hasImg: !!img,
                  src: img ? img.getAttribute('src') : null,
                  loaded: img ? (img.complete && img.naturalWidth > 0) : null,
                  initial: row.querySelector('.avatar-initial')?.textContent.trim() || null,
                };
              });
            })()
            """
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        ws.close()
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
