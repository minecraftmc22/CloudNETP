# -*- coding: utf-8 -*-
"""复现「歌词高亮没有」：songId=1（Monday Morning，纯 v1 词级）+ 跳 0:21。"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import websocket

urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

PORT = 8020
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
USER = "hlprobe"
PASS = "HlProbe#2026"


def http(method, path, body=None, cookie=""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Origin", BASE)
    req.add_header("Content-Type", "application/json")
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


def main():
    status, _, headers = http("POST", "/api/auth/register", {"username": USER, "password": PASS})
    if status not in (200, 201):
        status, _, headers = http("POST", "/api/auth/login", {"username": USER, "password": PASS})
    cookie = [c for c in headers.get_all("Set-Cookie") if c.startswith("cloudnetp_session=")][0].split(";")[0]

    # 造一个 90s mp3（无内嵌歌词）
    from mutagen.id3 import ID3, TIT2
    from mutagen.mp3 import MP3
    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
    audio = frame * int(90 * 44100 * 144 * 128 / (44100 * 413))
    tmp = Path(tempfile.mkdtemp(prefix="hl_")) / "Monday Morning.mp3"
    tmp.write_bytes(audio)
    tags = ID3()
    tags.add(TIT2(encoding=3, text="Monday Morning"))
    MP3(str(tmp), ID3=ID3)
    tags.save(tmp, v2_version=3)

    auth = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
    req = urllib.request.Request(f"{BASE}/dav/{urllib.parse.quote('Monday Morning.mp3')}", data=tmp.read_bytes(), method="PUT")
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        print("上传:", resp.status)
    _, listing, _ = http("GET", "/api/nodes", cookie=cookie)
    node = next(n for n in listing["items"] if n["name"].endswith(".mp3"))
    # 指定 songId=1（复现同曲），auto 也会命中同一首
    http("PUT", f"/api/nodes/{node['id']}/lyrics-pref", {"mode": "song", "songId": 1}, cookie=cookie)

    profile = Path(tempfile.mkdtemp(prefix="cdp_hl_"))
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--remote-debugging-port=9440", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
         "--autoplay-policy=no-user-gesture-required", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = None
        for _ in range(60):
            try:
                data = json.load(urllib.request.urlopen("http://127.0.0.1:9440/json/list", timeout=2))
                pages = [t for t in data if t.get("type") == "page"]
                if pages: target = pages[0]; break
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
                    if "error" in msg: raise RuntimeError(f"{m}: {msg['error']}")
                    return msg.get("result", {})
        def evaljs(e):
            return send("Runtime.evaluate", expression=e, returnByValue=True, userGesture=True)["result"].get("value")

        send("Page.enable"); send("Runtime.enable")
        send("Network.setCookie", name="cloudnetp_session", value=cookie.split("=", 1)[1],
             domain="127.0.0.1", path="/", httpOnly=True)
        send("Page.navigate", url=f"{BASE}/#/files")
        time.sleep(3.5)
        evaljs(f"""import('/static/js/ui/viewer.js').then(m => m.openViewer({{id: {node['id']},
             type: 'file', name: 'Monday Morning.mp3', size: 1, previewKind: 'audio'}}))""")
        time.sleep(4.0)

        # 播放并跳到 21 秒
        evaljs("""(() => { const a = document.querySelector('[data-lyr-audio]');
          a.play().catch(()=>{}); a.currentTime = 21; return a.currentTime; })()""")
        time.sleep(1.5)
        report = evaljs("""
          (() => {
            const line = document.querySelector('.lyr__line.is-active');
            const words = line ? line.querySelectorAll('.lyr__orig .lyr__w') : [];
            const a = document.querySelector('[data-lyr-audio]');
            return {
              ms: a.currentTime * 1000,
              activeText: line?.querySelector('.lyr__orig')?.textContent,
              wordSpanCount: words.length,
              ps: [...words].slice(0, 6).map(w => w.style.getPropertyValue('--p') || '(unset)'),
              activeCount: document.querySelectorAll('.lyr__line.is-active').length,
            };
          })()
        """)
        print(json.dumps(report, ensure_ascii=False, indent=1))
    finally:
        proc.kill()
    http("DELETE", f"/api/nodes/{node['id']}/purge", cookie=cookie)


if __name__ == "__main__":
    main()
