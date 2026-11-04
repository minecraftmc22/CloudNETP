# -*- coding: utf-8 -*-
"""管理后台举报审核截图：列表缩略图 + 详情里的文件清单。

用法：python scripts/admin_reports_shot.py [port]
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

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)


def main() -> None:
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    admin = db.query(User).filter(User.role == 1).order_by(User.id.asc()).first()
    token = create_token(admin.id, admin.token_version)
    db.close()

    profile = Path(tempfile.mkdtemp(prefix="cdp_reports_"))
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=9449",
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
                    for t in json.load(urllib.request.urlopen("http://127.0.0.1:9449/json/list", timeout=2))
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

        def wait_for(expr, timeout=25):
            end = time.time() + timeout
            while time.time() < end:
                if js(expr):
                    return True
                time.sleep(0.3)
            return False

        def click(x, y):
            for kind in ("mousePressed", "mouseReleased"):
                send(
                    "Input.dispatchMouseEvent",
                    type=kind,
                    x=x,
                    y=y,
                    button="left",
                    buttons=1 if kind == "mousePressed" else 0,
                    clickCount=1,
                )
            time.sleep(0.4)

        send("Page.enable")
        send("Runtime.enable")
        send("Network.enable")
        send("Emulation.setDeviceMetricsOverride", width=1280, height=1080, deviceScaleFactor=1, mobile=False)
        send(
            "Network.setCookie",
            name=settings.COOKIE_NAME,
            value=token,
            domain="127.0.0.1",
            path="/",
            httpOnly=True,
        )
        send(
            "Page.addScriptToEvaluateOnNewDocument",
            source="window.__errs=[];addEventListener('error',e=>window.__errs.push(String(e.message)));",
        )

        send("Page.navigate", url=f"{BASE}/#/admin")
        assert wait_for("!!document.querySelector('[data-tab=\"reports\"]')", 25), "后台没渲染"
        js("document.querySelector('[data-tab=\"reports\"]').click()")
        assert wait_for("document.querySelectorAll('.admin-row[data-id]').length > 0", 20), "举报列表没加载"
        time.sleep(1.2)  # 等缩略图解码
        print("行数:", js("document.querySelectorAll('.admin-row[data-id]').length"))
        print("缩略图:", js("[...document.querySelectorAll('.admin-thumb')].map(i=>({w:i.naturalWidth,h:i.naturalHeight}))"))
        print("首行文字:", js("document.querySelector('.admin-row[data-id]').innerText.replace(/\\n+/g,' | ')"))
        print("错误:", js("window.__errs"))
        shot("admin_reports_list.png")

        # 打开第一条举报详情
        box = js(
            "(() => { const r = document.querySelector('.admin-row[data-id]').getBoundingClientRect();"
            " return {x: r.left + 120, y: r.top + r.height/2}; })()"
        )
        click(box["x"], box["y"])
        assert wait_for("!!document.querySelector('.dialog-host.is-open .dialog')", 10), "详情没弹出"
        time.sleep(1.6)  # 等详情里的缩略图
        print("详情文字:", js("document.querySelector('.dialog').innerText.replace(/\\n+/g,' | ')")[:400])
        print("详情缩略图:", js(
            "[...document.querySelectorAll('.report-file__thumb')].map(i=>({w:i.naturalWidth,h:i.naturalHeight}))"
        ))
        shot("admin_report_detail.png")

        # 再点开一条合集举报（第二行，通常是「N 个项目」）
        send("Runtime.evaluate", expression="document.querySelector('.dialog-host [data-act=ok]').click()")
        time.sleep(0.6)
        box = js(
            "(() => { const rows=[...document.querySelectorAll('.admin-row[data-id]')];"
            " const row = rows.find(r => r.innerText.includes('个项目')) || rows[1];"
            " if (!row) return null; const r = row.getBoundingClientRect();"
            " return {x: r.left + 120, y: r.top + r.height/2}; })()"
        )
        if box:
            click(box["x"], box["y"])
            if wait_for("!!document.querySelector('.dialog-host.is-open .dialog')", 10):
                time.sleep(1.6)
                print("合集详情:", js("document.querySelector('.dialog').innerText.replace(/\\n+/g,' | ')")[:420])
                shot("admin_report_detail_multi.png")

        # 窄屏
        send("Emulation.setDeviceMetricsOverride", width=390, height=900, deviceScaleFactor=2, mobile=False)
        send("Page.navigate", url=f"{BASE}/#/admin")
        wait_for("!!document.querySelector('[data-tab=\"reports\"]')", 25)
        js("document.querySelector('[data-tab=\"reports\"]').click()")
        wait_for("document.querySelectorAll('.admin-row[data-id]').length > 0", 20)
        time.sleep(1.2)
        shot("admin_reports_mobile.png")

        ws.close()
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
