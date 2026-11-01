"""手机布局实测：用无头 Chrome 的 CDP 以手机视口渲染并截图。

用法：python scripts/mobile_layout_probe.py [宽] [高] [输出前缀]
默认 390x844。需要服务已在 127.0.0.1:8000 运行。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import websocket

# 绕过系统代理（本机 http_proxy 指向的代理连不上 127.0.0.1 目标时会回 502）
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

BASE = "http://127.0.0.1:8000"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
USER = "mobileprobe"
PASS = "Mobile#2026"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"

WIDTH = int(sys.argv[1]) if len(sys.argv) > 1 else 390
HEIGHT = int(sys.argv[2]) if len(sys.argv) > 2 else 844
TAG = sys.argv[3] if len(sys.argv) > 3 else f"{WIDTH}"


def http(method: str, path: str, body=None, cookie: str = ""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", BASE)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {"raw": raw.decode("utf-8", "ignore")}
        return exc.code, payload, exc.headers.get_all("Set-Cookie") or []


def ensure_user() -> str:
    """注册（已存在则登录）测试账号，返回 Cookie 头。"""
    status, body, cookies = http("POST", "/api/auth/register", {"username": USER, "password": PASS})
    if status not in (200, 201):
        status, body, cookies = http("POST", "/api/auth/login", {"username": USER, "password": PASS})
    if status not in (200, 201):
        raise SystemExit(f"账号准备失败：{status} {body}")
    for raw in cookies:
        if raw.startswith("cloudnetp_session="):
            return raw.split(";")[0]
    raise SystemExit(f"没拿到会话 Cookie：{cookies}")


class CDP:
    def __init__(self, port: int, cookie: str):
        self.port = port
        self.cookie = cookie
        self.msg_id = 0
        deadline = time.time() + 25
        target = None
        while time.time() < deadline:
            try:
                data = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2))
                pages = [t for t in data if t.get("type") == "page"]
                if pages:
                    target = pages[0]
                    break
            except Exception:
                pass
            time.sleep(0.4)
        if not target:
            raise SystemExit("连不上 CDP")
        self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=40, suppress_origin=True)

    def send(self, method: str, **params):
        self.msg_id += 1
        self.ws.send(json.dumps({"id": self.msg_id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.msg_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr: str, await_promise: bool = False):
        res = self.send(
            "Runtime.evaluate",
            expression=expr,
            returnByValue=True,
            awaitPromise=await_promise,
            userGesture=True,
        )
        return res.get("result", {}).get("value")

    def shot(self, name: str):
        OUT.mkdir(parents=True, exist_ok=True)
        res = self.send("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        path = OUT / f"{TAG}_{name}.png"
        path.write_bytes(base64.b64decode(res["data"]))
        print("截图", path)
        return path


def seed_content() -> None:
    """建几个文件夹 + 用 WebDAV 放两个文件，让网格/列表有东西可看。"""
    status, _, cookies = http("POST", "/api/auth/login", {"username": USER, "password": PASS})
    cookie = ""
    for raw in cookies:
        if raw.startswith("cloudnetp_session="):
            cookie = raw.split(";")[0]
    for name in ("照片", "文档", "音乐"):
        http("POST", "/api/nodes", {"type": "folder", "name": name}, cookie=cookie)
    import base64
    from urllib.parse import quote

    auth = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
    for name, body in (("远行.md", "# 旅行笔记\n\n一些文字。".encode()), ("壁纸.png", bytes(range(256)) * 40)):
        req = urllib.request.Request(f"{BASE}/dav/{quote(name)}", data=body, method="PUT")
        req.add_header("Authorization", f"Basic {auth}")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                print("DAV 上传", name, resp.status)
        except urllib.error.HTTPError as exc:
            print("DAV 上传失败", name, exc.code, exc.read()[:120])


def main() -> None:
    cookie = ensure_user()
    seed_content()
    profile = Path(tempfile.mkdtemp(prefix="cdp_mobile_"))
    port = 9411
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--hide-scrollbars",
            "--force-device-scale-factor=2",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        cdp = CDP(port, cookie)
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Network.enable")
        cdp.send(
            "Emulation.setDeviceMetricsOverride",
            width=WIDTH,
            height=HEIGHT,
            deviceScaleFactor=2,
            mobile=True,
            screenWidth=WIDTH,
            screenHeight=HEIGHT,
        )
        cdp.send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        cdp.send("Emulation.setEmitTouchEventsForMouse", enabled=True, configuration="mobile")
        cdp.send(
            "Network.setCookie",
            name="cloudnetp_session",
            value=cookie.split("=", 1)[1],
            domain="127.0.0.1",
            path="/",
            httpOnly=True,
        )
        cdp.send("Page.navigate", url=f"{BASE}/#/files")
        time.sleep(3.2)
        # SPA 内改 hash 不会重新 boot，必须整页重载
        cdp.eval("location.reload()")
        time.sleep(3.2)

        report = cdp.eval(
            """(() => {
              const nav = document.querySelector('.nav');
              const navRect = nav ? nav.getBoundingClientRect() : null;
              const vr = document.querySelector('.view-root').getBoundingClientRect();
              const items = [...document.querySelectorAll('.nav-item')].map(a => {
                const r = a.getBoundingClientRect();
                const lbl = a.querySelector('.nav-item__label');
                return {label: lbl ? lbl.textContent : '', w: Math.round(r.width), h: Math.round(r.height),
                        visible: lbl ? getComputedStyle(lbl).opacity !== '0' && lbl.getBoundingClientRect().width > 0 : false};
              });
              return {
                innerWidth: innerWidth,
                innerHeight: innerHeight,
                scrollW: document.documentElement.scrollWidth,
                docScrollW: document.body.scrollWidth,
                navTop: navRect ? Math.round(navRect.top) : null,
                navH: navRect ? Math.round(navRect.height) : null,
                navAtBottom: navRect ? Math.abs(navRect.bottom - innerHeight) < 2 : false,
                viewTop: Math.round(vr.top), viewBottom: Math.round(vr.bottom),
                items,
                tileCols: getComputedStyle(document.querySelector('.file-grid') || document.body).gridTemplateColumns,
              };
            })()"""
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))

        # 网格视图（显式点切换，避免上次运行残留的视图偏好）
        cdp.eval("document.querySelector('[data-view=\\'grid\\']')?.click()")
        time.sleep(1.2)
        cdp.shot("files-grid")

        # 列表视图
        cdp.eval("document.querySelector('[data-view=\\'list\\']')?.click()")
        time.sleep(1.2)
        cdp.shot("files-list")
        cdp.eval("document.querySelector('[data-view=\\'grid\\']')?.click()")
        time.sleep(0.8)

        # 新建文件夹对话框（底部抽屉）
        cdp.eval("document.querySelector('[data-act=\\'new-folder\\']')?.click()")
        time.sleep(1.0)
        cdp.shot("dialog")
        cdp.eval("document.querySelector('.dialog [data-act=\\'cancel\\']')?.click()")
        time.sleep(0.6)

        # 设置页
        cdp.eval("location.hash = '#/settings'")
        time.sleep(2.0)
        cdp.shot("settings")

        # 管理后台（该账号非管理员，只看导航是否 5 项）
        print("导航项：", cdp.eval("[...document.querySelectorAll('.nav-item__label')].map(e=>e.textContent)"))

        overflow = cdp.eval(
            "(() => { const w = document.documentElement.scrollWidth; return w > innerWidth + 1 ? w - innerWidth : 0; })()"
        )
        print("横向溢出像素：", overflow)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    main()
