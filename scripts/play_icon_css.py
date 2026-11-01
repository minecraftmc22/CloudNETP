# -*- coding: utf-8 -*-
"""用 CDP CSS.getMatchedStylesForNode 拿播放/暂停图标的真实匹配规则。"""
from __future__ import annotations

import json
import subprocess
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
    req = urllib.request.Request(BASE + "/api/auth/login", data=json.dumps({"username": USER, "password": PASS}).encode(), method="POST")
    req.add_header("Content-Type", "application/json"); req.add_header("Origin", BASE)
    with urllib.request.urlopen(req, timeout=10) as resp:
        cookie = [c for c in resp.headers.get_all("Set-Cookie") if c.startswith("cloudnetp_session=")][0].split(";")[0]
    req = urllib.request.Request(BASE + "/api/nodes"); req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, timeout=10) as resp:
        listing = json.loads(resp.read())
    node = next(n for n in listing["items"] if n["name"].endswith(".mp3"))

    profile = Path(tempfile.mkdtemp(prefix="cdp_css_"))
    port = 9436
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
        send("DOM.enable"); send("CSS.enable")
        send("Network.setCookie", name="cloudnetp_session", value=cookie.split("=", 1)[1],
             domain="127.0.0.1", path="/", httpOnly=True)
        send("Page.navigate", url=f"{BASE}/#/files")
        time.sleep(4)
        evaljs(f"""import('/static/js/ui/viewer.js').then(m => m.openViewer({{id: {node['id']},
             type: 'file', name: 'Icon Probe Song.mp3', size: 1, previewKind: 'audio'}}))""")
        time.sleep(3)

        doc = send("DOM.getDocument", depth=-1)
        def find_node(attr):
            res = send("DOM.performSearch", query=f"[{attr}]", includePierce=True)
            if not res.get("resultCount"):
                return None
            results = send("DOM.getSearchResults", searchId=res["searchId"], fromIndex=0, toIndex=1)
            return results["nodeIds"][0]

        for attr in ("data-lyr-ic-play", "data-lyr-ic-pause"):
            nid = find_node(attr)
            if not nid:
                print(attr, "节点不存在"); continue
            matched = send("CSS.getMatchedStylesForNode", nodeId=nid)
            print(f"=== {attr} 匹配规则 ===")
            for rule_match in matched.get("matchedCSSRules", []):
                rule = rule_match["rule"]
                origin = rule.get("origin")
                sel = rule.get("selectorList", {}).get("text", "?")
                props = {p["name"]: p["value"] for p in rule.get("style", {}).get("cssProperties", []) if not p.get("parsedOk") is False}
                disp = props.get("display")
                if disp:
                    print(f"  [{origin}] {sel} -> display: {disp}")
            inline = matched.get("inlineStyle") or matched.get("inlinePayload")
            print("  inline:", inline)
    finally:
        proc.kill()


if __name__ == "__main__":
    main()
