# -*- coding: utf-8 -*-
"""手机端布局探针：在 390px 视口下量关键容器宽度，找出谁把页面撑宽。"""
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
HASH = sys.argv[2] if len(sys.argv) > 2 else "#/admin"
TAB = sys.argv[3] if len(sys.argv) > 3 else ""
W, H = 390, 844
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

PROBE = r"""
(() => {
  const W = window.innerWidth;
  const rows = [];
  const walk = (el, depth) => {
    if (depth > 12) return;
    const r = el.getBoundingClientRect();
    if (r.width > W + 1) {
      const cs = getComputedStyle(el);
      rows.push({
        d: depth,
        sel: el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
             (typeof el.className === 'string' && el.className.trim()
               ? '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.') : ''),
        w: Math.round(r.width),
        sw: el.scrollWidth,
        ovx: cs.overflowX,
        disp: cs.display,
        minw: cs.minWidth,
      });
    }
    for (const c of el.children) walk(c, depth + 1);
  };
  walk(document.documentElement, 0);
  return { vw: W, docSW: document.documentElement.scrollWidth, rows: rows.slice(0, 24) };
})()
"""

if not db_failover._started:
    db_failover.startup()
_db = db_failover.current_sessionmaker()()
try:
    _admin = _db.query(User).filter(User.role == 1).order_by(User.id.asc()).first()
    token = create_token(_admin.id, _admin.token_version)
finally:
    _db.close()

profile = Path(tempfile.mkdtemp(prefix="cdp_probe_"))
proc = subprocess.Popen(
    [
        CHROME,
        "--headless=new",
        "--remote-debugging-port=9466",
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
                for t in json.load(urllib.request.urlopen("http://127.0.0.1:9466/json/list", timeout=2))
                if t.get("type") == "page"
            ]
            if pages:
                target = pages[0]
                break
        except Exception:
            time.sleep(0.4)
    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
    mid = 0

    def send(method, **params):
        global mid
        mid += 1
        ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    def js(expr):
        return send("Runtime.evaluate", expression=expr, returnByValue=True, userGesture=True)["result"].get("value")

    send("Page.enable")
    send("Runtime.enable")
    send("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=2, mobile=True)
    send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
    send("Network.setCookie", name=settings.COOKIE_NAME, value=token, domain="127.0.0.1", path="/", httpOnly=True)
    send("Page.navigate", url=f"{BASE}/#/files")
    time.sleep(3.5)
    import os

    if os.environ.get("PROBE_CSS"):
        js(
            "(() => { const s = document.createElement('style'); s.textContent = "
            + json.dumps(os.environ["PROBE_CSS"])
            + "; document.head.appendChild(s); })()"
        )
    # HASH 支持逗号分隔的路径序列：逐个跳，最后停在最后一个
    for hop in [h for h in HASH.split(",") if h]:
        js(f"location.hash = {json.dumps(hop)}")
        time.sleep(1.8)
        print("hop", hop, "innerWidth", js("window.innerWidth"), "docSW", js("document.documentElement.scrollWidth"))
    time.sleep(1.2)
    if os.environ.get("PROBE_RESET"):
        # 重新下发一次设备参数，把「缩放自适应」后的视口拉回 390 再量
        send("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=2, mobile=True)
        time.sleep(1.2)
        print("after reset innerWidth", js("window.innerWidth"))
    if TAB:
        js(f"document.querySelector('[data-tab=\"{TAB}\"]')?.click()")
        time.sleep(2.5)
    info = js(PROBE)
    if os.environ.get("PROBE_EVAL"):
        print("EVAL:", json.dumps(js(os.environ["PROBE_EVAL"]), ensure_ascii=False, indent=1))
    print("viewport", info["vw"], "docScrollWidth", info["docSW"])
    for r in info["rows"]:
        print(f"  d{r['d']:>2} {r['sel']:<52} w={r['w']:>4} sw={r['sw']:>4} ovx={r['ovx']:<8} disp={r['disp']:<12} minw={r['minw']}")
    ws.close()
finally:
    proc.terminate()
