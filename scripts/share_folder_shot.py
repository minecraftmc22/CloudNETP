# -*- coding: utf-8 -*-
"""文件夹分享页截图：转存按钮 + 勾选批量转存，1280 / 390 两档。

用法：python scripts/share_folder_shot.py <token> [port]
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

TOKEN = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)


def main() -> None:
    profile = Path(tempfile.mkdtemp(prefix="cdp_fold_"))
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=9448",
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
                    for t in json.load(urllib.request.urlopen("http://127.0.0.1:9448/json/list", timeout=2))
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
            time.sleep(0.35)

        send("Page.enable")
        send("Runtime.enable")
        send(
            "Page.addScriptToEvaluateOnNewDocument",
            source="window.__errs=[];addEventListener('error',e=>window.__errs.push(String(e.message)));",
        )

        for label, width, height, scale in (("desktop", 1280, 980, 1), ("mobile", 390, 844, 2)):
            send("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=scale, mobile=False)
            send("Page.navigate", url=f"{BASE}/s/{TOKEN}")
            assert wait_for("document.querySelectorAll('.share-item').length >= 1", 25), f"{label} 列表没渲染"
            time.sleep(0.9)
            info = js(
                "(() => { const a = document.querySelector('.share-card__actions');"
                " const r = a.getBoundingClientRect();"
                " return {right: Math.round(r.right), top: Math.round(r.top),"
                "  btns: [...a.querySelectorAll('.btn')].map(x => [x.textContent.trim(),"
                "   Math.round(x.getBoundingClientRect().width), Math.round(x.getBoundingClientRect().top)])}; })()"
            )
            print(f"{label} 按钮区:", info)
            boxes = js(
                "(() => [...document.querySelectorAll('.share-item [data-check]')].slice(0,1).map(el => {"
                " const r = el.getBoundingClientRect();"
                " return {x: r.left + r.width/2, y: r.top + r.height/2}; }))()"
            )
            for b in boxes:
                click(b["x"], b["y"])
            time.sleep(0.4)
            print(f"{label} 勾选后:", js(
                "(() => { const b = document.querySelector('[data-save-picked]');"
                " return {text: b.textContent.trim(), disabled: b.disabled}; })()"
            ))
            shot(f"share_folder_{label}.png")
            print(f"{label} 页面错误:", js("window.__errs"))

        ws.close()
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
