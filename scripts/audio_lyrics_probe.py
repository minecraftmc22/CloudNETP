"""音频全屏歌词播放器端到端探针。

流程：注册测试账号 → 上传带 ID3 标签与封面的 mp3（Monday Morning / CG5）
→ CDP 打开预览器 → 断言歌词渲染 / 逐词高亮 / 控制栏 → 截图。

用法：python scripts/audio_lyrics_probe.py [端口] [标签]
默认端口 8000。测试账号用完请手动清理。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import websocket

urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
TAG = sys.argv[2] if len(sys.argv) > 2 else f"p{PORT}"
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
USER = "lyricprobe"
PASS = "Lyric#2026"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"


def http(method: str, path: str, body=None, cookie: str = "", raw=None, ctype=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Origin", BASE)
    if ctype:
        req.add_header("Content-Type", ctype)
    else:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
            return resp.status, (json.loads(payload) if payload[:1] in (b"{", b"[") else payload), resp.headers
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload), exc.headers
        except Exception:
            return exc.code, payload, exc.headers


def cookie_of(headers) -> str:
    for raw in headers.get_all("Set-Cookie") or []:
        if raw.startswith("cloudnetp_session="):
            return raw.split(";")[0]
    return ""


def make_test_mp3() -> bytes:
    """造一个带 ID3（标题/歌手/专辑/封面）的假 mp3（5.8 秒静音帧）。"""
    from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1
    from mutagen.mp3 import MP3

    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
    audio = frame * int(5 * 44100 * 144 * 128 / (44100 * 413))
    tmp = Path(tempfile.mkdtemp(prefix="lyrprobe_")) / "Monday Morning.mp3"
    tmp.write_bytes(audio)
    tags = ID3()
    # 1x1 红色 PNG 当封面
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
    )
    tags.add(TIT2(encoding=3, text="Monday Morning"))
    tags.add(TPE1(encoding=3, text="CG5"))
    tags.add(TALB(encoding=3, text="Monday Morning"))
    tags.add(APIC(encoding=3, mime="image/png", type=3, desc="", data=png))
    MP3(str(tmp), ID3=ID3)
    tags.save(tmp, v2_version=3)
    return tmp.read_bytes()


def main() -> None:
    status, _, headers = http("POST", "/api/auth/register", {"username": USER, "password": PASS})
    if status not in (200, 201):
        status, _, headers = http("POST", "/api/auth/login", {"username": USER, "password": PASS})
    assert status in (200, 201), f"账号准备失败 {status}"
    cookie = cookie_of(headers)
    assert cookie, "没拿到 Cookie"

    mp3 = make_test_mp3()
    status, uploaded, _ = http(
        "POST", "/api/uploads/direct?name=Monday%20Morning.mp3",
        raw=mp3, ctype="audio/mpeg", cookie=cookie,
    )
    if status not in (200, 201):
        # 回退：走 WebDAV PUT
        import urllib.parse

        auth = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
        req = urllib.request.Request(
            f"{BASE}/dav/{urllib.parse.quote('Monday Morning.mp3')}", data=mp3, method="PUT"
        )
        req.add_header("Authorization", f"Basic {auth}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
        print("WebDAV 上传:", status)

    # 列表拿到节点 id
    status, listing, _ = http("GET", "/api/nodes", cookie=cookie)
    node = next(n for n in listing["items"] if n["name"].endswith(".mp3"))
    node_id = node["id"]
    print("节点 id:", node_id, "| previewKind:", node.get("previewKind"))

    # API 冒烟
    status, meta, _ = http("GET", f"/api/nodes/{node_id}/audio-meta", cookie=cookie)
    print("audio-meta:", status, meta if status != 200 else {k: meta[k] for k in ("title", "artist", "album", "duration", "hasCover")})
    status, cover, headers_ = http("GET", f"/api/nodes/{node_id}/cover", cookie=cookie)
    print("cover:", status, headers_.get("Content-Type"), len(cover) if isinstance(cover, bytes) else cover)
    status, lyr, _ = http("GET", "/api/lyrics?title=Monday%20Morning&artist=CG5", cookie=cookie)
    print("lyrics:", status, "lines:", len(lyr.get("lines", [])) if isinstance(lyr, dict) else lyr)

    # ---- CDP ----
    profile = Path(tempfile.mkdtemp(prefix="cdp_lyr_"))
    port = 9421
    proc = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
         "--autoplay-policy=no-user-gesture-required", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
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
                time.sleep(0.4)
        assert target, "CDP 连接失败"
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

        def evaljs(expr, await_promise=False):
            res = send("Runtime.evaluate", expression=expr, returnByValue=True,
                       awaitPromise=await_promise, userGesture=True)
            return res.get("result", {}).get("value")

        def shot(name):
            OUT.mkdir(parents=True, exist_ok=True)
            res = send("Page.captureScreenshot", format="png")
            path = OUT / f"{TAG}_{name}.png"
            path.write_bytes(base64.b64decode(res["data"]))
            print("截图", path)

        send("Page.enable")
        send("Runtime.enable")
        send("Network.enable")
        send("Emulation.setDeviceMetricsOverride", width=1280, height=800, deviceScaleFactor=2, mobile=False)
        send("Network.setCookie", name="cloudnetp_session", value=cookie.split("=", 1)[1],
             domain="127.0.0.1", path="/", httpOnly=True)
        send("Page.navigate", url=f"{BASE}/#/files")
        time.sleep(3.2)
        evaljs("location.reload()")
        time.sleep(3.2)

        # 打开音频预览
        evaljs(
            f"""(() => {{
              const row = [...document.querySelectorAll('[data-id]')].find(e => e.dataset.id == '{node_id}');
              (row || document.querySelector('.file-grid, .file-list')).dispatchEvent(
                new MouseEvent('dblclick', {{bubbles: true}}));
            }})()"""
        )
        time.sleep(2.5)
        # 双击没触发就直接调 viewer API 兜底
        opened = evaljs("Boolean(document.querySelector('.lyr'))")
        if not opened:
            evaljs(
                f"""import('/static/js/ui/viewer.js').then(m => m.openViewer({{id: {node_id},
                     type: 'file', name: 'Monday Morning.mp3', size: 1, previewKind: 'audio'}}))"""
            )
            time.sleep(2.5)

        report = evaljs(
            """(() => {
              const lyr = document.querySelector('.lyr');
              if (!lyr) return {opened: false};
              return {
                opened: true,
                song: lyr.querySelector('[data-lyr-song]')?.textContent,
                artist: lyr.querySelector('[data-lyr-artist]')?.textContent,
                lineCount: lyr.querySelectorAll('.lyr__line').length,
                hasActive: Boolean(lyr.querySelector('.lyr__line.is-active')),
                empty: lyr.querySelector('[data-lyr-empty]')?.hidden === false
                         ? lyr.querySelector('[data-lyr-empty]').textContent : null,
                hasToggle: Boolean(lyr.querySelector('[data-lyr-toggle]')),
                hasSeek: Boolean(lyr.querySelector('[data-lyr-seek]')),
                coverShown: !lyr.querySelector('[data-lyr-cover-img]')?.hidden,
                audioError: lyr.querySelector('[data-lyr-audio]')?.error?.code ?? null,
              };
            })()"""
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        shot("lyrics-initial")

        # 跳到歌词中段看逐词高亮 + 自动滚动
        evaljs(
            """(() => {
              const a = document.querySelector('[data-lyr-audio]');
              a.currentTime = 60;
              a.play().catch(() => {});
            })()"""
        )
        time.sleep(3.0)
        mid_report = evaljs(
            """(() => {
              const lyr = document.querySelector('.lyr');
              const act = lyr.querySelector('.lyr__line.is-active');
              const w = act?.querySelector('.lyr__w');
              return {
                activeText: act?.querySelector('.lyr__orig')?.textContent?.slice(0, 30),
                wordP: w?.style.getPropertyValue('--p'),
                trans: act?.querySelector('.lyr__trans')?.textContent,
                paused: document.querySelector('[data-lyr-audio]')?.paused,
              };
            })()"""
        )
        print(json.dumps(mid_report, ensure_ascii=False, indent=2))
        shot("lyrics-active")

        # 切封面视图
        evaljs("document.querySelector('[data-lyr-mode]')?.click()")
        time.sleep(0.8)
        shot("art-mode")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    main()
