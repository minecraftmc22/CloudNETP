# -*- coding: utf-8 -*-
"""播放/暂停图标切换 + MediaSession(SMTC) 探针。

用法：python scripts/play_icon_probe.py [端口] [--noupload]
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

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else 8020
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
USER = "iconprobe"
PASS = "IconProbe#2026"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"

failures: list[str] = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  | {detail}" if detail else ""))
    if not cond:
        failures.append(name)


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
    for raw in headers.get_all("Set-Cookie") or []:
        if raw.startswith("cloudnetp_session="):
            return raw.split(";")[0]
    return ""


def make_test_mp3() -> bytes:
    from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1
    from mutagen.mp3 import MP3

    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
    audio = frame * int(8 * 44100 * 144 * 128 / (44100 * 413))
    tmp = Path(tempfile.mkdtemp(prefix="iconprobe_")) / "Icon Probe Song.mp3"
    tmp.write_bytes(audio)
    tags = ID3()
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
    )
    tags.add(TIT2(encoding=3, text="Icon Probe Song"))
    tags.add(TPE1(encoding=3, text="CloudNETP"))
    tags.add(TALB(encoding=3, text="Icon Probe"))
    tags.add(APIC(encoding=3, mime="image/png", type=3, desc="", data=png))
    MP3(str(tmp), ID3=ID3)
    tags.save(tmp, v2_version=3)
    return tmp.read_bytes()


def upload_and_get_node(cookie) -> int:
    mp3_bytes = make_test_mp3()
    auth = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
    req = urllib.request.Request(
        f"{BASE}/dav/{urllib.parse.quote('Icon Probe Song.mp3')}", data=mp3_bytes, method="PUT"
    )
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        print("WebDAV 上传:", resp.status)
    _, listing, _ = http("GET", "/api/nodes", cookie=cookie)
    return next(n for n in listing["items"] if n["name"].endswith(".mp3"))["id"]


def main() -> None:
    status, _, headers = http("POST", "/api/auth/register", {"username": USER, "password": PASS})
    if status not in (200, 201):
        status, _, headers = http("POST", "/api/auth/login", {"username": USER, "password": PASS})
    assert status in (200, 201), f"账号准备失败 {status}"
    cookie = cookie_of(headers)
    node_id = upload_and_get_node(cookie)
    print("节点 id:", node_id)

    profile = Path(tempfile.mkdtemp(prefix="cdp_iconprobe_"))
    port = 9434
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

        evaljs(
            f"""import('/static/js/ui/viewer.js').then(m => m.openViewer({{id: {node_id},
                 type: 'file', name: 'Icon Probe Song.mp3', size: 1, previewKind: 'audio'}}))"""
        )
        time.sleep(3.0)

        icon_state = evaljs(
            """(() => {
              const play = document.querySelector('[data-lyr-ic-play]');
              const pause = document.querySelector('[data-lyr-ic-pause]');
              const cs = (el) => el ? getComputedStyle(el).display : 'missing';
              return {
                playFound: !!play, pauseFound: !!pause,
                playHiddenAttr: play?.hidden, pauseHiddenAttr: pause?.hidden,
                playDisplay: cs(play), pauseDisplay: cs(pause),
                paused: document.querySelector('[data-lyr-audio]')?.paused,
              };
            })()"""
        )
        print("初始:", json.dumps(icon_state, ensure_ascii=False))
        check("初始显示播放图标", icon_state.get("playDisplay") != "none" and icon_state.get("pauseDisplay") == "none",
              str(icon_state))

        # 点击播放
        evaljs("document.querySelector('[data-lyr-toggle]').click()")
        time.sleep(1.2)
        after_play = evaljs(
            """(() => {
              const play = document.querySelector('[data-lyr-ic-play]');
              const pause = document.querySelector('[data-lyr-ic-pause]');
              const cs = (el) => getComputedStyle(el).display;
              return {playDisplay: cs(play), pauseDisplay: cs(pause),
                      paused: document.querySelector('[data-lyr-audio]').paused};
            })()"""
        )
        print("播放中:", json.dumps(after_play, ensure_ascii=False))
        check("播放中显示暂停图标", after_play.get("pauseDisplay") != "none" and after_play.get("playDisplay") == "none",
              str(after_play))

        # 点击暂停
        evaljs("document.querySelector('[data-lyr-toggle]').click()")
        time.sleep(0.8)
        after_pause = evaljs(
            """(() => {
              const play = document.querySelector('[data-lyr-ic-play]');
              const pause = document.querySelector('[data-lyr-ic-pause]');
              const cs = (el) => getComputedStyle(el).display;
              return {playDisplay: cs(play), pauseDisplay: cs(pause),
                      paused: document.querySelector('[data-lyr-audio]').paused};
            })()"""
        )
        print("暂停后:", json.dumps(after_pause, ensure_ascii=False))
        check("暂停后显示播放图标", after_pause.get("playDisplay") != "none" and after_pause.get("pauseDisplay") == "none",
              str(after_pause))

        # MediaSession（SMTC）断言
        ms = evaljs(
            """(() => {
              if (!('mediaSession' in navigator)) return {supported: false};
              const m = navigator.mediaSession;
              return {supported: true,
                      title: m.metadata?.title, artist: m.metadata?.artist, album: m.metadata?.album,
                      artwork: (m.metadata?.artwork || []).map(a => a.src),
                      playbackState: m.playbackState,
                      hasPlay: typeof m.setActionHandler === 'function'};
            })()"""
        )
        print("MediaSession(暂停时):", json.dumps(ms, ensure_ascii=False))
        check("metadata.title 设置", ms.get("title") == "Icon Probe Song", str(ms.get("title")))
        check("metadata.artist 设置", ms.get("artist") == "CloudNETP", str(ms.get("artist")))
        check("metadata.album 设置", ms.get("album") == "Icon Probe", str(ms.get("album")))
        check("封面已挂到 artwork", "/cover" in str(ms.get("artwork")), str(ms.get("artwork")))
        check("playbackState=paused", ms.get("playbackState") == "paused", str(ms.get("playbackState")))

        # 播放后 playbackState 应变 playing
        evaljs("document.querySelector('[data-lyr-toggle]').click()")
        time.sleep(1.0)
        ms2 = evaljs("navigator.mediaSession.playbackState")
        check("播放中 playbackState=playing", ms2 == "playing", str(ms2))
    finally:
        proc.kill()
    print()
    print("失败项:", failures if failures else "无")


if __name__ == "__main__":
    main()
