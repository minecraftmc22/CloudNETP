# -*- coding: utf-8 -*-
"""诊断：为什么播放图标 display 与 hidden 属性不一致。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
PORT = 8020
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
USER, PASS = "iconprobe", "IconProbe#2026"


def main():
    # 登录拿 cookie
    req = urllib.request.Request(BASE + "/api/auth/login", data=json.dumps({"username": USER, "password": PASS}).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", BASE)
    with urllib.request.urlopen(req, timeout=10) as resp:
        cookie = [c for c in resp.headers.get_all("Set-Cookie") if c.startswith("cloudnetp_session=")][0].split(";")[0]

    _, listing, _ = None, None, None
    req = urllib.request.Request(BASE + "/api/nodes"); req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, timeout=10) as resp:
        listing = json.loads(resp.read())
    node = next(n for n in listing["items"] if n["name"].endswith(".mp3"))

    profile = Path(tempfile.mkdtemp(prefix="cdp_diag_"))
    port = 9435
    proc = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = None
        for _ in range(60):
            try:
                data = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2))
                pages = [t for t in data if t.get("type") == "page"]
                if pages: target = pages[0]; break
            except Exception: time.sleep(0.4)
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
        mid = 0
        def send(method, **params):
            nonlocal mid
            mid += 1
            ws.send(json.dumps({"id": mid, "method": method, "params": params}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == mid:
                    if "error" in msg: raise RuntimeError(f"{method}: {msg['error']}")
                    return msg.get("result", {})
        def evaljs(expr):
            return send("Runtime.evaluate", expression=expr, returnByValue=True, userGesture=True)["result"].get("value")

        send("Page.enable"); send("Runtime.enable")
        send("Network.setCookie", name="cloudnetp_session", value=cookie.split("=", 1)[1],
             domain="127.0.0.1", path="/", httpOnly=True)
        send("Page.navigate", url=f"{BASE}/#/files")
        time.sleep(4)
        evaljs("location.reload()")
        time.sleep(4)

        # DOM 结构诊断
        diag = evaljs(f"""
          (() => {{
            const ic = document.querySelectorAll('[data-lyr-ic-play]');
            const root = document.querySelector('.lyr');
            const play = document.querySelector('[data-lyr-ic-play]');
            const r = {{
              lyrRoots: document.querySelectorAll('.lyr').length,
              playIcons: ic.length,
              toggleCount: document.querySelectorAll('[data-lyr-toggle]').length,
              playInRoot: play ? root.contains(play) : null,
              layoutCssLoaded: [...document.styleSheets].some(s => (s.href||'').includes('layout.css')),
              allSheets: [...document.styleSheets].map(s => s.href || 'inline').slice(0, 20),
              hiddenRule: null,
            }};
            try {{
              for (const sheet of document.styleSheets) {{
                for (const rule of sheet.cssRules) {{
                  if (rule.selectorText && rule.selectorText.includes('[hidden]')) {{
                    r.hiddenRule = {{ sel: rule.selectorText, css: rule.style.cssText, href: sheet.href }};
                  }}
                }}
              }}
            }} catch (e) {{ r.ruleErr = String(e); }}
            return r;
          }})()
        """)
        print(json.dumps(diag, ensure_ascii=False, indent=1))

        # 打开 viewer 后再查 matched styles
        evaljs(f"""import('/static/js/ui/viewer.js').then(m => m.openViewer({{id: {node['id']},
             type: 'file', name: 'Icon Probe Song.mp3', size: 1, previewKind: 'audio'}}))""")
        time.sleep(3)
        post = evaljs("""
          (() => {
            const play = document.querySelector('[data-lyr-ic-play]');
            const pause = document.querySelector('[data-lyr-ic-pause]');
            const roots = document.querySelectorAll('.lyr');
            const cs = (el) => el ? getComputedStyle(el).display : 'missing';
            return {
              roots: roots.length,
              playHidden: play.hidden, playDisp: cs(play), playInline: play.style.display,
              pauseHidden: pause.hidden, pauseDisp: cs(pause), pauseInline: pause.style.display,
              paused: document.querySelector('[data-lyr-audio]')?.paused,
            };
          })()
        """)
        print("open 后:", json.dumps(post, ensure_ascii=False, indent=1))
    finally:
        proc.kill()


if __name__ == "__main__":
    main()
