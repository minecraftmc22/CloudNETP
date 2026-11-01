# -*- coding: utf-8 -*-
"""0.3.8 探针：行级内嵌歌词（无词级标签）的卡拉OK整行填充。

直接使用用户 m4a 里的真实内嵌 LRC 文本。
用法：python scripts/linelevel_probe.py [端口]
"""
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

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8020
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
USER = "llprobe"
PASS = "LlProbe#2026"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"

failures: list[str] = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  | {detail}" if detail else ""))
    if not cond:
        failures.append(name)


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


def embedded_text() -> str:
    """从用户 m4a 的 blob 里取真实内嵌 LRC。"""
    sys.path.insert(0, ".")
    from app.lyrics_service import embedded_lyrics

    sha = "ee9533713bd85b998388dd70e6448d03ad8822204284de46edf19b25529679c9"
    p = Path("storage/blobs") / sha[:2] / sha[2:4] / sha
    return embedded_lyrics(p)


def main() -> None:
    status, _, headers = http("POST", "/api/auth/register", {"username": USER, "password": PASS})
    if status not in (200, 201):
        status, _, headers = http("POST", "/api/auth/login", {"username": USER, "password": PASS})
    cookie = [c for c in headers.get_all("Set-Cookie") if c.startswith("cloudnetp_session=")][0].split(";")[0]

    # 90s mp3 + USLT 内嵌用户的真实行级 LRC
    from mutagen.id3 import ID3, TIT2, USLT
    from mutagen.mp3 import MP3

    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
    audio = frame * int(90 * 44100 * 144 * 128 / (44100 * 413))
    tmp = Path(tempfile.mkdtemp(prefix="ll_")) / "Monday Morning.mp3"
    tmp.write_bytes(audio)
    tags = ID3()
    tags.add(TIT2(encoding=3, text="Monday Morning"))
    tags.add(USLT(encoding=3, lang="eng", desc="", text=embedded_text()))
    MP3(str(tmp), ID3=ID3)
    tags.save(tmp, v2_version=3)

    auth = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
    req = urllib.request.Request(f"{BASE}/dav/{urllib.parse.quote('Monday Morning.mp3')}", data=tmp.read_bytes(), method="PUT")
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        print("上传:", resp.status)
    _, listing, _ = http("GET", "/api/nodes", cookie=cookie)
    node = next(n for n in listing["items"] if n["name"].endswith(".mp3"))

    _, lyr, _ = http("GET", f"/api/nodes/{node['id']}/lyrics", cookie=cookie)
    lines = lyr.get("lines") or []
    with_words = sum(1 for l in lines if l.get("words"))
    print("source:", lyr.get("source"), "| 行数:", len(lines), "| 词级行:", with_words)
    check("source=embedded", lyr.get("source") == "embedded")
    check("行级（无词级）", with_words == 0, str(with_words))
    check("同时间戳翻译已合并", any(l.get("trans") for l in lines))

    # 21 秒应命中 "That don't mean I gotta"（19818-21106 附近，以内嵌时间为准）
    target = next((l for l in lines if l["start"] <= 21000 < l["end"]), None)
    print("21s 命中行:", target and target["text"])
    check("21s 有活跃行", target is not None)

    profile = Path(tempfile.mkdtemp(prefix="cdp_ll_"))
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--remote-debugging-port=9441", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
         "--autoplay-policy=no-user-gesture-required", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target_ws = None
        for _ in range(60):
            try:
                data = json.load(urllib.request.urlopen("http://127.0.0.1:9441/json/list", timeout=2))
                pages = [t for t in data if t.get("type") == "page"]
                if pages: target_ws = pages[0]; break
            except Exception:
                time.sleep(0.4)
        ws = websocket.create_connection(target_ws["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
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
        def shot(name):
            res = send("Page.captureScreenshot", format="png")
            OUT.mkdir(parents=True, exist_ok=True)
            (OUT / name).write_bytes(base64.b64decode(res["data"]))
            print("截图", OUT / name)

        send("Page.enable"); send("Runtime.enable")
        send("Emulation.setDeviceMetricsOverride", width=1280, height=800, deviceScaleFactor=2, mobile=False)
        send("Network.setCookie", name="cloudnetp_session", value=cookie.split("=", 1)[1],
             domain="127.0.0.1", path="/", httpOnly=True)
        send("Page.navigate", url=f"{BASE}/#/files")
        time.sleep(3.5)
        evaljs(f"""import('/static/js/ui/viewer.js').then(m => m.openViewer({{id: {node['id']},
             type: 'file', name: 'Monday Morning.mp3', size: 1, previewKind: 'audio'}}))""")
        time.sleep(4.0)
        evaljs("""(() => { const a = document.querySelector('[data-lyr-audio]');
          a.play().catch(()=>{}); a.currentTime = 21; })()""")
        time.sleep(1.5)
        rep = evaljs("""
          (() => {
            const line = document.querySelector('.lyr__line.is-active');
            const span = line?.querySelector('.lyr__orig .lyr__w');
            return {
              activeText: line?.querySelector('.lyr__orig')?.textContent,
              p: span?.style.getPropertyValue('--p') || '(unset)',
              spanCount: line ? line.querySelectorAll('.lyr__orig .lyr__w').length : 0,
            };
          })()
        """)
        print(json.dumps(rep, ensure_ascii=False))
        check("活跃行正确", rep.get("activeText") == (target and target["text"]), str(rep.get("activeText")))
        check("整行 --p 在推进", rep.get("p") not in ("(unset)", "0.000", None, "(unset)"), str(rep.get("p")))
        pv = float(rep.get("p") or 0)
        check("--p 数值合理", 0 < pv <= 1, str(pv))
        shot("linelevel_fill.png")
    finally:
        proc.kill()

    http("DELETE", f"/api/nodes/{node['id']}/purge", cookie=cookie)
    print("已清理节点", node["id"])
    print()
    print("失败项:", failures if failures else "无")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
