# -*- coding: utf-8 -*-
"""分享相关 UI 回归截图：创建分享后的结果弹窗是否残留、合集页勾选转存。

用法：python scripts/share_ui_shot.py [port]
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

    profile = Path(tempfile.mkdtemp(prefix="cdp_share_"))
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=9447",
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
                    for t in json.load(urllib.request.urlopen("http://127.0.0.1:9447/json/list", timeout=2))
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

        def center(selector):
            return js(
                "(() => { const b = document.querySelector(%s);"
                " if (!b) return null; const r = b.getBoundingClientRect();"
                " return {x: r.left + r.width/2, y: r.top + r.height/2}; })()" % json.dumps(selector)
            )

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
        # 页面加载前就装好错误收集器
        send(
            "Page.addScriptToEvaluateOnNewDocument",
            source="window.__errs=[];addEventListener('error',e=>window.__errs.push(String(e.message)));"
            "addEventListener('unhandledrejection',e=>window.__errs.push('rej:'+String(e.reason)));",
        )

        # ---------- 1. 多选 → 分享 → 点「创建链接」 ----------
        send("Page.navigate", url=f"{BASE}/#/files")
        assert wait_for("document.querySelectorAll('[data-id]').length > 2", 25), "文件列表没加载"
        time.sleep(1.0)
        boxes = js(
            "(() => [...document.querySelectorAll('[data-id]')].slice(0,2).map(el => {"
            " const r = el.getBoundingClientRect();"
            " return {x: r.left + r.width/2, y: r.top + r.height/2}; }))()"
        )
        click(boxes[0]["x"], boxes[0]["y"], modifiers=2)
        click(boxes[1]["x"], boxes[1]["y"], modifiers=2)
        wait_for("!!document.querySelector('.selbar.is-on')", 10)
        pos = center('.selbar [data-act="sel-share"]')
        assert pos, "批量栏没有分享按钮"
        click(pos["x"], pos["y"])
        assert wait_for("!!document.querySelector('.dialog-host.is-open .dialog')", 10), "创建对话框没出现"
        time.sleep(0.4)

        pos = center('.dialog-host [data-act="ok"]')
        click(pos["x"], pos["y"])
        # 关键：跨过 180ms 的关闭动画定时器，看新弹窗是否被清空
        time.sleep(1.2)
        state = js(
            "(() => { const h = document.querySelector('.dialog-host');"
            " return {open: h.classList.contains('is-open'),"
            "  hasDialog: !!h.querySelector('.dialog'),"
            "  title: (h.querySelector('.dialog__title span')||{}).textContent || '',"
            "  textLen: h.innerText.trim().length}; })()"
        )
        print("结果弹窗状态:", state)
        assert state["hasDialog"] and state["textLen"] > 10, f"结果弹窗被清空了: {state}"
        # 弹窗外的按钮应当点得到（说明没有残留遮罩挡住）
        print("弹窗存在时页面错误:", js("window.__errs"))
        shot("share_created_dialog.png")

        share_url = js("(document.querySelector('.share-result__url')||{}).textContent || ''")
        print("创建的链接:", share_url)

        # 关掉弹窗
        pos = center('.dialog-host [data-act="ok"]')
        click(pos["x"], pos["y"])
        time.sleep(0.6)
        print("关闭后宿主:", js(
            "(() => { const h = document.querySelector('.dialog-host');"
            " return {open: h.classList.contains('is-open'), empty: !h.innerHTML.trim()}; })()"
        ))

        # ---------- 2. 合集页勾选 + 转存 ----------
        token_part = share_url.rsplit("/s/", 1)[-1] if "/s/" in share_url else ""
        if token_part:
            send("Page.navigate", url=f"{BASE}/s/{token_part}")
            assert wait_for("document.querySelectorAll('.share-item').length >= 2", 25), "合集列表没渲染"
            time.sleep(0.8)
            print("勾选框数量:", js("document.querySelectorAll('.share-item [data-check]').length"))
            print("转存按钮初始:", js(
                "(() => { const b = document.querySelector('[data-save-picked]');"
                " return b ? {text: b.textContent.trim(), disabled: b.disabled} : null; })()"
            ))
            boxes = js(
                "(() => [...document.querySelectorAll('.share-item [data-check]')].slice(0,2).map(el => {"
                " const r = el.getBoundingClientRect();"
                " return {x: r.left + r.width/2, y: r.top + r.height/2}; }))()"
            )
            for b in boxes:
                click(b["x"], b["y"])
            time.sleep(0.4)
            print("勾选后转存按钮:", js(
                "(() => { const b = document.querySelector('[data-save-picked]');"
                " return {text: b.textContent.trim(), disabled: b.disabled}; })()"
            ))
            print("全选框:", js("(document.querySelector('[data-pick-all]')||{}).checked"))
            print("选中计数:", js("(document.querySelector('[data-picked-count]')||{}).textContent"))
            shot("share_collection_picked.png")

            # 点转存 → 确认框
            pos = center("[data-save-picked]")
            click(pos["x"], pos["y"])
            if wait_for("!!document.querySelector('.share-modal')", 10):
                time.sleep(0.5)
                print("转存弹窗:", js("document.querySelector('.share-modal').innerText.replace(/\\n+/g,' | ')"))
                shot("share_save_multi_modal.png")
            print("分享页错误:", js("window.__errs"))

        # ---------- 3. 窄屏 ----------
        send("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=False)
        if token_part:
            send("Page.navigate", url=f"{BASE}/s/{token_part}")
            wait_for("document.querySelectorAll('.share-item').length >= 2", 20)
            time.sleep(1.0)
            boxes = js(
                "(() => [...document.querySelectorAll('.share-item [data-check]')].slice(0,1).map(el => {"
                " const r = el.getBoundingClientRect();"
                " return {x: r.left + r.width/2, y: r.top + r.height/2}; }))()"
            )
            for b in boxes:
                click(b["x"], b["y"])
            time.sleep(0.4)
            print("窄屏按钮区:", js(
                "(() => { const a = document.querySelector('.share-card__actions');"
                " const r = a.getBoundingClientRect();"
                " return {w: Math.round(r.width), right: Math.round(r.right),"
                "  btns: [...a.querySelectorAll('.btn')].map(x => Math.round(x.getBoundingClientRect().width))}; })()"
            ))
            shot("share_collection_mobile.png")

        ws.close()
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
