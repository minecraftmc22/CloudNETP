# -*- coding: utf-8 -*-
"""看板娘拖动 / 大小设置冒烟：拖动位置、位置持久化、尺寸滑块、宽度生效。

用法：python scripts/mascot_drag_smoke.py [port]
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

ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {label} {extra}")
    else:
        fail += 1
        print(f"  FAIL {label} {extra}")


def main():
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    admin = db.query(User).filter(User.role == 1).order_by(User.id).first()
    token = create_token(admin.id, admin.token_version)
    db.close()

    profile = Path(tempfile.mkdtemp(prefix="cdp_mascot_"))
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=9477",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--hide-scrollbars",
            "--window-size=1280,1000",
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
                    for t in json.load(urllib.request.urlopen("http://127.0.0.1:9477/json/list", timeout=2))
                    if t.get("type") == "page"
                ]
                if pages:
                    target = pages[0]
                    break
            except Exception:
                time.sleep(0.4)
        assert target, "无法连接 Chrome CDP"

        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=90, suppress_origin=True)
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
            res = send("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True,
                       userGesture=True)
            return res["result"].get("value")

        def shot(name):
            res = send("Page.captureScreenshot", format="png")
            p = OUT / name
            p.write_bytes(base64.b64decode(res["data"]))
            print("截图", p)

        def wait_for(expr, timeout=40):
            end = time.time() + timeout
            while time.time() < end:
                if js(expr):
                    return True
                time.sleep(0.3)
            return False

        def mouse(kind, x, y, buttons=0):
            button = "left" if kind in ("mousePressed", "mouseReleased") or buttons else "none"
            send(
                "Input.dispatchMouseEvent",
                type=kind,
                x=int(x),
                y=int(y),
                button=button,
                buttons=buttons,
                clickCount=1 if kind in ("mousePressed", "mouseReleased") else 0,
            )

        send("Page.enable")
        send("Runtime.enable")
        send("Emulation.setDeviceMetricsOverride", width=1280, height=1000, deviceScaleFactor=1, mobile=False)
        send("Network.setCookie", name=settings.COOKIE_NAME, value=token,
             domain="127.0.0.1", path="/", httpOnly=True)
        send("Page.addScriptToEvaluateOnNewDocument",
             source="window.__errs=[];addEventListener('error',e=>window.__errs.push(String(e.message)));")

        send("Page.navigate", url=f"{BASE}/#/files")
        assert wait_for("!!document.querySelector('#mascot canvas')", 45), "看板娘没渲染"
        time.sleep(2.5)

        state = js("""(() => {
          const host = document.querySelector('#mascot');
          const r = host.getBoundingClientRect();
          const cs = getComputedStyle(document.documentElement);
          return { rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
                   scale: cs.getPropertyValue('--mascot-scale').trim(),
                   dx: cs.getPropertyValue('--mascot-dx').trim(),
                   dy: cs.getPropertyValue('--mascot-dy').trim(),
                   saved: localStorage.getItem('mascot-pos'),
                   size: localStorage.getItem('mascot-size') };
        })()""")
        print("初始:", state)
        check("默认缩放 1", state["scale"] in ("", "1.00", "1"), state["scale"])
        w0 = state["rect"][2]
        h0 = state["rect"][3]
        check("Live2D 默认宽度 232", w0 == 232, w0)
        check("默认位置无偏移", state["dx"] in ("", "0px") and state["dy"] in ("", "0px"), [state["dx"], state["dy"]])
        shot("mascot_before.png")

        # 1) 拖动：从身体中间往左上拖 120, -80
        x0, y0 = state["rect"][0] + w0 / 2, state["rect"][1] + h0 / 2
        mouse("mousePressed", x0, y0, 1)
        time.sleep(0.2)
        for i in range(1, 6):
            mouse("mouseMoved", x0 - 24 * i, y0 - 16 * i, 1)
            time.sleep(0.08)
        mouse("mouseReleased", x0 - 120, y0 - 80, 0)
        time.sleep(0.6)

        after = js("""(() => {
          const host = document.querySelector('#mascot');
          const r = host.getBoundingClientRect();
          const cs = getComputedStyle(document.documentElement);
          return { rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
                   dx: cs.getPropertyValue('--mascot-dx').trim(),
                   dy: cs.getPropertyValue('--mascot-dy').trim(),
                   saved: localStorage.getItem('mascot-pos'),
                   dragging: host.classList.contains('is-dragging'),
                   errs: window.__errs };
        })()""")
        print("拖动后:", after)
        check("位置向左移动约 120", abs(after["rect"][0] - (state["rect"][0] - 120)) <= 2, after["rect"][0])
        check("位置向上移动约 80", abs(after["rect"][1] - (state["rect"][1] - 80)) <= 2, after["rect"][1])
        check("偏移写入 localStorage", bool(after["saved"]), after["saved"])
        check("拖动后松开状态", after["dragging"] is False, "")
        shot("mascot_dragged.png")

        # 2) 刷新后位置保持
        send("Page.reload")
        assert wait_for("!!document.querySelector('#mascot canvas')", 45), "刷新后看板娘没渲染"
        time.sleep(2.0)
        reload_state = js("""(() => {
          const r = document.querySelector('#mascot').getBoundingClientRect();
          return [Math.round(r.x), Math.round(r.y)];
        })()""")
        check("刷新后位置保持", reload_state == after["rect"][:2], reload_state)

        # 3) 点击不拖动 → 戳一戳气泡（先手动关掉自动冒泡，避免误判）
        js("document.querySelector('#mascot-bubble').classList.remove('is-on')")
        rx, ry = js("""(() => {
          const r = document.querySelector('#mascot').getBoundingClientRect();
          return [r.x + r.width/2, r.y + r.height*0.6];
        })()""")
        mouse("mousePressed", rx, ry, 1)
        time.sleep(0.15)
        mouse("mouseReleased", rx, ry, 0)
        time.sleep(0.6)
        check("点击触发气泡", js("document.querySelector('#mascot-bubble').classList.contains('is-on')") is True, "")

        # 4) 设置页调大小到 150%
        js("location.hash = '#/settings'")
        assert wait_for("!!document.querySelector('[data-mascot-size]')", 30), "大小滑块没渲染"
        time.sleep(0.8)
        js("""(() => {
          const el = document.querySelector('[data-mascot-size]');
          el.value = 150;
          el.dispatchEvent(new Event('input', { bubbles: true }));
        })()""")
        time.sleep(1.5)
        size_state = js("""(() => {
          const host = document.querySelector('#mascot');
          const r = host.getBoundingClientRect();
          const cs = getComputedStyle(document.documentElement);
          return { w: Math.round(r.width), h: Math.round(r.height),
                   scale: cs.getPropertyValue('--mascot-scale').trim(),
                   val: document.querySelector('[data-mascot-size-val]').textContent,
                   saved: localStorage.getItem('mascot-size'),
                   canvasW: document.querySelector('#mascot canvas').width,
                   errs: window.__errs };
        })()""")
        print("150%:", size_state)
        check("缩放变量 1.5", size_state["scale"] in ("1.50", "1.5"), size_state["scale"])
        check("宽度变为 348", size_state["w"] == 348, size_state["w"])
        check("高度按比例 480", size_state["h"] == 480, size_state["h"])
        check("滑块读数 150%", size_state["val"] == "150%", size_state["val"])
        check("尺寸写入 localStorage", size_state["saved"] == "150", size_state["saved"])
        shot("mascot_settings_size.png")

        # 5) 刷新后尺寸保持
        send("Page.reload")
        assert wait_for("!!document.querySelector('#mascot canvas')", 45), "刷新后没渲染"
        time.sleep(2.0)
        size_after = js("""(() => {
          const r = document.querySelector('#mascot').getBoundingClientRect();
          return { w: Math.round(r.width), h: Math.round(r.height),
                   scale: getComputedStyle(document.documentElement).getPropertyValue('--mascot-scale').trim() };
        })()""")
        check("刷新后尺寸保持", size_after["w"] == 348 and size_after["h"] == 480, size_after)

        print("最终错误:", js("window.__errs"))
        ws.close()
    finally:
        proc.terminate()

    print(f"\n通过 {ok} 项，失败 {fail} 项")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
