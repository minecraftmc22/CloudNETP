# -*- coding: utf-8 -*-
"""举报/转存功能浏览器截图（分享页按钮 + 举报对话框 + 管理后台审核页签）。"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import websocket

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8020
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)

SHARER, SHARER_PW = "rptshare2", "RptShare2#2026"
ADMIN, ADMIN_PW = "rptadmin2", "RptAdmin2#2026"


def http(method, path, body=None, cookie="", raw=None, ctype=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Origin", BASE)
    req.add_header("Content-Type", ctype or "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            payload = resp.read()
            return resp.status, (json.loads(payload) if payload[:1] in (b"{", b"[") else payload), resp.headers
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload), exc.headers
        except Exception:
            return exc.code, payload, exc.headers


def cookie_of(headers):
    for entry in headers.get_all("Set-Cookie") or []:
        if entry.startswith("cloudnetp_session="):
            return entry.split(";")[0]
    return ""


def make_account(username, password):
    s, _, headers = http("POST", "/api/auth/register", {"username": username, "password": password})
    if s not in (200, 201):
        s, _, headers = http("POST", "/api/auth/login", {"username": username, "password": password})
    assert s in (200, 201)
    return cookie_of(headers)


def main():
    # ---- 数据准备 ----
    sharer_cookie = make_account(SHARER, SHARER_PW)
    admin_cookie = make_account(ADMIN, ADMIN_PW)
    from app import db_failover
    from app.models import User

    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    u = db.query(User).filter_by(username=ADMIN).first()
    u.role = 1
    db.commit()
    db.close()

    payload = b"demo report file " + b"Z" * 1024
    auth = base64.b64encode(f"{SHARER}:{SHARER_PW}".encode()).decode()
    req = urllib.request.Request(f"{BASE}/dav/{urllib.parse.quote('Demo Report File.txt')}", data=payload, method="PUT")
    req.add_header("Authorization", f"Basic {auth}")
    urllib.request.urlopen(req, timeout=30)

    _, listing, _ = http("GET", "/api/nodes", cookie=sharer_cookie)
    node = next(n for n in listing["items"] if n["name"].endswith(".txt"))
    s, data, _ = http("POST", "/api/shares", {"nodeId": node["id"]}, cookie=sharer_cookie)
    token = data["share"]["token"]
    http("POST", f"/api/public/shares/{token}/report", {"reason": "包含不当内容（演示数据）"})
    print("分享 token:", token)

    # ---- CDP ----
    profile = Path(tempfile.mkdtemp(prefix="cdp_report_"))
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--remote-debugging-port=9441", f"--user-data-dir={profile}",
         "--no-first-run", "--hide-scrollbars", "--window-size=1280,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(60):
            try:
                pages = [t for t in json.load(urllib.request.urlopen("http://127.0.0.1:9441/json/list", timeout=2)) if t.get("type") == "page"]
                if pages:
                    target = pages[0]
                    break
            except Exception:
                time.sleep(0.4)
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
        mid = 0

        def send(m, **p):
            nonlocal mid
            mid += 1
            ws.send(json.dumps({"id": mid, "method": m, "params": p}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError(f"{m}: {msg['error']}")
                    return msg.get("result", {})

        def evaljs(expr):
            return send("Runtime.evaluate", expression=expr, returnByValue=True, userGesture=True)["result"].get("value")

        def shot(name):
            res = send("Page.captureScreenshot", format="png")
            p = OUT / name
            p.write_bytes(base64.b64decode(res["data"]))
            print("截图", p)

        send("Page.enable")
        send("Runtime.enable")
        send("Emulation.setDeviceMetricsOverride", width=1280, height=900, deviceScaleFactor=1, mobile=False)

        # 1. 分享页（含转存/举报按钮）
        send("Page.navigate", url=f"{BASE}/s/{token}")
        time.sleep(4)
        shot("report_share_page.png")

        # 2. 举报对话框
        evaljs("document.querySelector('[data-report]').click()")
        time.sleep(0.6)
        shot("report_modal.png")
        evaljs("document.querySelector('#report-reason').value='截图演示：包含不当内容'")
        evaljs("document.querySelector('[data-ok]').click()")
        time.sleep(1.5)
        shot("report_done.png")

        # 3. 管理后台举报审核
        send("Network.setCookie", name="cloudnetp_session", value=admin_cookie.split("=", 1)[1],
             domain="127.0.0.1", path="/", httpOnly=True)
        send("Page.navigate", url=f"{BASE}/#/admin")
        time.sleep(4)
        evaljs("[...document.querySelectorAll('[data-tab]')].find(b=>b.dataset.tab==='reports')?.click()")
        time.sleep(1.5)
        shot("report_admin_tab.png")
    finally:
        proc.kill()

    # ---- 清理 ----
    _, shares, _ = http("GET", "/api/shares", cookie=sharer_cookie)
    for sh in shares.get("items", []):
        http("DELETE", f"/api/shares/{sh['id']}", cookie=sharer_cookie)
    _, listing, _ = http("GET", "/api/nodes", cookie=sharer_cookie)
    for n in listing["items"]:
        if n["type"] == "file":
            http("DELETE", f"/api/nodes/{n['id']}/purge", cookie=sharer_cookie)
    for username, cookie in ((SHARER, sharer_cookie), (ADMIN, admin_cookie)):
        http("POST", f"/api/auth/logout", {}, cookie=cookie)
    db = db_failover.current_sessionmaker()()
    from app.models import ShareReport

    db.query(ShareReport).delete()
    from app.models import User as _U

    db.query(_U).filter(_U.username.in_([SHARER, ADMIN])).delete(synchronize_session=False)
    db.commit()
    db.close()
    import sqlite3

    conn = sqlite3.connect("storage/db/fallback.db")
    cur = conn.cursor()
    orphans = cur.execute(
        "SELECT b.sha256 FROM blobs b WHERE NOT EXISTS "
        "(SELECT 1 FROM nodes n WHERE n.blob_sha256=b.sha256 AND n.deleted_at IS NULL)"
    ).fetchall()
    for (sha,) in orphans:
        p = Path("storage/blobs") / sha[:2] / sha[2:4] / sha
        if p.exists():
            p.unlink()
        cur.execute("DELETE FROM blobs WHERE sha256=?", (sha,))
    conn.commit()
    conn.close()
    print("清理完成")


if __name__ == "__main__":
    main()
