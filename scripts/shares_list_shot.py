# -*- coding: utf-8 -*-
"""「我的分享」列表截图：确认合集条目的标识（合集 N 项）。"""
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


def main() -> None:
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    admin = db.query(User).filter(User.role == 1).order_by(User.id.asc()).first()
    token = create_token(admin.id, admin.token_version)
    db.close()

    profile = Path(tempfile.mkdtemp(prefix="cdp_shares_"))
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--remote-debugging-port=9447", f"--user-data-dir={profile}",
         "--no-first-run", "--hide-scrollbars", "--window-size=1280,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(60):
            try:
                pages = [t for t in json.load(urllib.request.urlopen("http://127.0.0.1:9447/json/list", timeout=2))
                         if t.get("type") == "page"]
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
            return send("Runtime.evaluate", expression=expr, returnByValue=True)["result"].get("value")

        send("Page.enable")
        send("Runtime.enable")
        send("Network.enable")
        send("Emulation.setDeviceMetricsOverride", width=1280, height=900, deviceScaleFactor=1, mobile=False)
        send("Network.setCookie", name=settings.COOKIE_NAME, value=token, domain="127.0.0.1", path="/", httpOnly=True)
        send("Page.navigate", url=f"{BASE}/#/shares")

        end = time.time() + 20
        while time.time() < end and not js("document.querySelectorAll('.share-row').length"):
            time.sleep(0.3)
        time.sleep(0.8)
        print("列表行:", js("[...document.querySelectorAll('.share-row__meta')].slice(0,3).map(e=>e.textContent)"))
        res = send("Page.captureScreenshot", format="png")
        path = OUT / "shares_list.png"
        path.write_bytes(base64.b64decode(res["data"]))
        print("截图", path)
        ws.close()
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
