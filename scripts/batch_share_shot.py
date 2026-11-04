# -*- coding: utf-8 -*-
"""批量分享截图：合集分享页 + 进入文件夹 + 主站多选批量栏。

用法：python scripts/batch_share_shot.py <token> [port]
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

TOKEN = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
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

    profile = Path(tempfile.mkdtemp(prefix="cdp_batch_"))
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=9445",
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
                    for t in json.load(urllib.request.urlopen("http://127.0.0.1:9445/json/list", timeout=2))
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

        def click(x, y, modifiers=0):
            for kind in ("mousePressed", "mouseReleased"):
                send(
                    "Input.dispatchMouseEvent",
                    type=kind,
                    x=x,
                    y=y,
                    button="left",
                    buttons=1 if kind == "mousePressed" else 0,
                    clickCount=1,
                    modifiers=modifiers,
                )
            time.sleep(0.35)

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

        # 1. 合集分享页
        send("Page.navigate", url=f"{BASE}/s/{TOKEN}")
        assert wait_for("document.querySelectorAll('.share-item').length >= 3", 25), "合集列表没渲染"
        time.sleep(0.6)
        print("合集条目:", js("[...document.querySelectorAll('.share-item__name')].map(e=>e.textContent)"))
        shot("batch_share_collection.png")

        # 2. 点进合集里的文件夹
        print("文件夹按钮坐标:", js(
            "(() => { const b = document.querySelector('.share-item [data-enter]');"
            " if (!b) return null; const r = b.getBoundingClientRect();"
            " return {x: r.left + r.width/2, y: r.top + r.height/2}; })()"
        ))
        pos = js(
            "(() => { const b = document.querySelector('.share-item [data-enter]');"
            " if (!b) return null; const r = b.getBoundingClientRect();"
            " return {x: r.left + r.width/2, y: r.top + r.height/2}; })()"
        )
        if pos:
            click(pos["x"], pos["y"])
            wait_for("!!document.querySelector('[data-back]')", 10)
            time.sleep(0.6)
            shot("batch_share_folder_inside.png")

        # 3. 主站多选批量栏
        send("Page.navigate", url=f"{BASE}/#/files")
        assert wait_for("document.querySelectorAll('.tile, .frow').length > 2", 25), "文件列表没加载"
        time.sleep(1.0)
        boxes = js(
            "(() => [...document.querySelectorAll('[data-id]')].slice(0,2).map(el => {"
            " const r = el.getBoundingClientRect();"
            " return {x: r.left + r.width/2, y: r.top + r.height/2}; }))()"
        )
        assert boxes and len(boxes) == 2, f"找不到两张卡片: {boxes}"
        click(boxes[0]["x"], boxes[0]["y"], modifiers=2)
        click(boxes[1]["x"], boxes[1]["y"], modifiers=2)
        wait_for("!!document.querySelector('.selbar.is-on')", 10)
        time.sleep(0.5)
        print("批量栏按钮:", js("[...document.querySelectorAll('.selbar .btn')].map(b=>b.textContent.trim())"))
        shot("batch_share_selbar.png")

        # 4. 点多选栏的「分享」，看创建对话框
        share_btn = js(
            "(() => { const b = document.querySelector('.selbar [data-act=\"sel-share\"]');"
            " if (!b) return null; const r = b.getBoundingClientRect();"
            " return {x: r.left + r.width/2, y: r.top + r.height/2}; })()"
        )
        if share_btn:
            click(share_btn["x"], share_btn["y"])
            if wait_for("!!document.querySelector('.dialog, [role=dialog], .modal')", 10):
                time.sleep(0.5)
                print("对话框文字:", js("document.body.innerText.split('取消')[0].slice(-160)"))
                shot("batch_share_dialog.png")

        # 5. 窄屏（390）下四按钮的批量栏
        send("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=False)
        send("Page.navigate", url=f"{BASE}/#/files")
        wait_for("document.querySelectorAll('[data-id]').length > 2", 20)
        time.sleep(1.2)
        boxes = js(
            "(() => [...document.querySelectorAll('[data-id]')].slice(0,2).map(el => {"
            " const r = el.getBoundingClientRect();"
            " return {x: r.left + r.width/2, y: r.top + r.height/2}; }))()"
        )
        if boxes and len(boxes) == 2:
            click(boxes[0]["x"], boxes[0]["y"], modifiers=2)
            click(boxes[1]["x"], boxes[1]["y"], modifiers=2)
            wait_for("!!document.querySelector('.selbar.is-on')", 10)
            time.sleep(0.5)
            print("窄屏批量栏:", js("(() => { const b = document.querySelector('.selbar');"
                                 " const r = b.getBoundingClientRect();"
                                 " return {w: Math.round(r.width), right: Math.round(r.right),"
                                 " btns: [...b.querySelectorAll('.btn')].map(x => Math.round(x.getBoundingClientRect().width))}; })()"))
            shot("batch_share_selbar_mobile.png")

        ws.close()
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
