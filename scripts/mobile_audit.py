# -*- coding: utf-8 -*-
"""手机端适配体检：按 390x844（iPhone 14 竖屏）逐页截图，并报告横向溢出元素。

用法（服务需已用隔离库启动）：
    python scripts/mobile_audit.py [port] [outdir] [only]

only 可用逗号分隔，例如 `admin,files`；默认跑全部。
Chrome 掉线会自动重启会话，单个页面失败不影响其它页面。
"""
from __future__ import annotations

import base64
import json
import os
import random
import socket
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
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(tempfile.gettempdir()) / "cn_mobile"
ONLY = [s for s in (sys.argv[3].split(",") if len(sys.argv) > 3 else []) if s]
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

W = int(os.environ.get("MOBILE_W", 390))
H = int(os.environ.get("MOBILE_H", 844))

OVERFLOW_JS = r"""
(() => {
  const W = window.innerWidth;
  const out = [];
  const skip = (el) => {
    const cs = getComputedStyle(el);
    return cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0';
  };
  const cls = (el) => {
    let c = el.tagName.toLowerCase();
    if (el.id) c += '#' + el.id;
    if (typeof el.className === 'string' && el.className.trim()) {
      c += '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.');
    }
    return c;
  };
  const inScroller = (el) => {
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX;
      if (o === 'auto' || o === 'scroll' || o === 'hidden') return true;
    }
    return false;
  };
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    if (r.right <= W + 1 && r.left >= -1) continue;
    if (skip(el)) continue;
    // 纯装饰（背景光斑、噪点）不算
    if (el.closest('.backdrop, .auth-art, #mascot')) continue;
    // 被横向滚动条裁掉的内容（页签条滚动后左侧那些）不算溢出
    if (inScroller(el)) continue;
    out.push({
      sel: cls(el),
      l: Math.round(r.left), r: Math.round(r.right), w: Math.round(r.width),
      txt: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40),
    });
  }
  return {
    vw: W,
    docScrollW: document.documentElement.scrollWidth,
    n: out.length,
    items: out.slice(0, 24),
  };
})()
"""


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Session:
    """一次 Chrome 会话；崩了就重启。"""

    def __init__(self, token: str) -> None:
        self.token = token
        self.proc = None
        self.ws = None
        self.mid = 0
        self.start()

    def start(self) -> None:
        self.stop()
        self.port = free_port()
        profile = Path(tempfile.mkdtemp(prefix="cdp_mobile_"))
        self.proc = subprocess.Popen(
            [
                CHROME,
                "--headless=new",
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--hide-scrollbars",
                "--disable-features=CalculateNativeWinOcclusion",
                f"--window-size={W},{H}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        target = None
        for _ in range(100):
            try:
                pages = [
                    t
                    for t in json.load(
                        urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=2)
                    )
                    if t.get("type") == "page"
                ]
                if pages:
                    target = pages[0]
                    break
            except Exception:
                time.sleep(0.3)
        if not target:
            raise RuntimeError("无法连接 Chrome CDP")
        self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=90, suppress_origin=True)
        self.mid = 0
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Network.enable")
        self.send("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=2, mobile=True)
        self.send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        self.send(
            "Network.setCookie",
            name=settings.COOKIE_NAME,
            value=self.token,
            domain="127.0.0.1",
            path="/",
            httpOnly=True,
        )
        # 首屏必须真正导航一次：只改 location.hash 在 about:blank 上不起作用
        self.send("Page.navigate", url=f"{BASE}/#/files")
        self.wait_for("!!document.querySelector('#view-root')", 30)

    def stop(self) -> None:
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=8)
            except Exception:
                pass
        self.proc = None
        time.sleep(0.4)

    def send(self, method, **params):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True, userGesture=True)
        return r["result"].get("value")

    def wait_for(self, expr, timeout=25):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.js(expr):
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    def goto(self, hash_: str, ready: str, settle=1.3) -> bool:
        self.js(f"location.hash = {json.dumps(hash_)}")
        ok = self.wait_for(f"!!document.querySelector({json.dumps(ready)})", 25)
        time.sleep(settle)
        return ok

    def shot(self, name: str) -> Path:
        res = self.send("Page.captureScreenshot", format="png")
        path = OUT / name
        path.write_bytes(base64.b64decode(res["data"]))
        return path

    def report(self, label: str) -> None:
        info = self.js(OVERFLOW_JS)
        print(f"\n=== {label}  溢出元素 {info['n']} 个 · vw={info['vw']} docScrollW={info['docScrollW']}")
        for it in info["items"]:
            print(f"    {it['sel']:<58} l={it['l']:>5} r={it['r']:>5} w={it['w']:>4}  {it['txt'][:26]}")


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


PAGES = [
    ("01_files", "files", "#/files", ".file-grid, .frow, .empty"),
    ("02_shares", "shares", "#/shares", ".share-row, .empty, .view-body .card"),
    ("03_trash", "trash", "#/trash", ".frow, .empty, .view-body"),
    ("04_settings", "settings", "#/settings", ".settings"),
    ("05_about", "about", "#/about", ".about-hero"),
    ("06_feedback", "feedback", "#/feedback", ".view-body"),
    ("07_quick", "quick", "#/quick", ".view-body"),
    ("08_notices", "notices", "#/notices", ".notice-list, .empty"),
    ("09_friends", "friends", "#/friends", ".friend-list, .empty"),
    ("20_mascot", "mascot", "#/mascot", ".mascot-hero"),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    token = admin_token()
    ses = Session(token)
    try:
        if os.environ.get("NO_COOKIE"):
            # 未登录状态：登录 / 注册页
            ses.js("document.cookie = '__none=1'")
            ses.send("Network.clearBrowserCookies")
            ses.send("Page.reload")
            ses.wait_for("!!document.querySelector('#auth-root .auth-card')", 25)
            time.sleep(1.5)
            ses.shot("00_auth.png")
            ses.report("登录页 #/files（未登录）")
            return
        for name, key, hash_, ready in PAGES:
            if ONLY and key not in ONLY and "all" not in ONLY:
                continue
            try:
                ok = ses.goto(hash_, ready)
                ses.shot(f"{name}.png")
                ses.report(f"{name} {hash_} ready={ok}")
            except Exception as exc:  # noqa: BLE001
                print(f"\n!! {name} 失败：{exc}（重启会话）")
                ses.start()

        if not ONLY or "admin" in ONLY or "all" in ONLY:
            ses.goto("#/admin", "[data-tab]", settle=1.4)
            tabs = ses.js("[...document.querySelectorAll('[data-tab]')].map(b => b.dataset.tab)") or []
            print("\n后台页签:", tabs)
            for i, tab in enumerate(tabs):
                try:
                    ses.js(f"document.querySelector('[data-tab=\"{tab}\"]').click()")
                    time.sleep(2.2)
                    ses.shot(f"1{i}_admin_{tab}.png")
                    ses.report(f"admin/{tab}")
                    if tab == "reports":
                        # 顺手看一眼手机上的底部抽屉详情
                        ses.js("document.querySelector('.admin-row--report[data-id]')?.click()")
                        time.sleep(1.8)
                        ses.shot("1i_admin_report_detail.png")
                        ses.report("admin/举报详情弹层")
                        ses.js(
                            "document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))"
                        )
                        time.sleep(0.9)
                except Exception as exc:  # noqa: BLE001
                    print(f"\n!! admin/{tab} 失败：{exc}（重启会话）")
                    try:
                        ses.start()
                        ses.goto("#/admin", "[data-tab]", settle=1.4)
                    except Exception:
                        pass
    finally:
        ses.stop()
    print("\n截图目录:", OUT)


if __name__ == "__main__":
    main()
