# -*- coding: utf-8 -*-
"""0.3.7 探针：翻译开关 / 对唱左右分边 / 同唱双行高亮。

用法：python scripts/duet_probe.py [端口]
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
USER = "duetprobe"
PASS = "DuetProbe#2026"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
DUET_SONG_ID = 6  # 歌词库里 v1/v2 双角色且有重叠行的歌

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
    from mutagen.id3 import ID3, TALB, TIT2, TPE1
    from mutagen.mp3 import MP3

    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
    audio = frame * int(90 * 44100 * 144 * 128 / (44100 * 413))
    tmp = Path(tempfile.mkdtemp(prefix="duetprobe_")) / "Duet Probe Song.mp3"
    tmp.write_bytes(audio)
    tags = ID3()
    tags.add(TIT2(encoding=3, text="Duet Probe Song"))
    tags.add(TPE1(encoding=3, text="CloudNETP"))
    tags.add(TALB(encoding=3, text="Duet Probe"))
    MP3(str(tmp), ID3=ID3)
    tags.save(tmp, v2_version=3)
    return tmp.read_bytes()


def main() -> None:
    status, _, headers = http("POST", "/api/auth/register", {"username": USER, "password": PASS})
    if status not in (200, 201):
        status, _, headers = http("POST", "/api/auth/login", {"username": USER, "password": PASS})
    assert status in (200, 201), f"账号准备失败 {status}"
    cookie = cookie_of(headers)

    auth = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
    req = urllib.request.Request(
        f"{BASE}/dav/{urllib.parse.quote('Duet Probe Song.mp3')}", data=make_test_mp3(), method="PUT"
    )
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        print("WebDAV 上传:", resp.status)
    _, listing, _ = http("GET", "/api/nodes", cookie=cookie)
    node = next(n for n in listing["items"] if n["name"].endswith(".mp3"))

    # 指定到对唱歌
    status, pref, _ = http(
        "PUT", f"/api/nodes/{node['id']}/lyrics-pref",
        {"mode": "song", "songId": DUET_SONG_ID, "songTitle": "Duet", "songArtist": "Probe"},
        cookie=cookie,
    )
    assert status == 200, f"设置 song pref 失败 {status}"
    _, lyr, _ = http("GET", f"/api/nodes/{node['id']}/lyrics", cookie=cookie)
    lines = lyr.get("lines") or []
    agents = sorted({l.get("agent") for l in lines if l.get("agent")})
    print("歌词来源:", lyr.get("source"), "| agents:", agents, "| 行数:", len(lines))
    check("source=song", lyr.get("source") == "song")
    check("带 agent 字段", len(agents) >= 2, str(agents))

    # 找一个「两人同时唱」的重叠时刻（取重叠窗口中点）
    overlap_start = None
    for i, l in enumerate(lines):
        for m in lines[i + 1:i + 3]:
            if m["start"] < l["end"] and l["start"] < m["end"] and m.get("agent") != l.get("agent"):
                overlap_start = (max(l["start"], m["start"]) + min(l["end"], m["end"])) // 2
                break
        if overlap_start:
            break
    print("重叠时刻:", overlap_start)
    check("存在同唱重叠行", overlap_start is not None)

    # CDP
    profile = Path(tempfile.mkdtemp(prefix="cdp_duet_"))
    port = 9439
    proc = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
         "--autoplay-policy=no-user-gesture-required", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(60):
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

        def evaljs(expr):
            return send("Runtime.evaluate", expression=expr, returnByValue=True, userGesture=True)["result"].get("value")

        def shot(name):
            res = send("Page.captureScreenshot", format="png")
            OUT.mkdir(parents=True, exist_ok=True)
            (OUT / name).write_bytes(base64.b64decode(res["data"]))
            print("截图", OUT / name)

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
            f"""import('/static/js/ui/viewer.js').then(m => m.openViewer({{id: {node['id']},
                 type: 'file', name: 'Duet Probe Song.mp3', size: 1, previewKind: 'audio'}}))"""
        )
        time.sleep(3.5)

        sides = evaljs(
            """(() => ({
                left: document.querySelectorAll('.lyr__line.is-left').length,
                right: document.querySelectorAll('.lyr__line.is-right').length,
                total: document.querySelectorAll('.lyr__line').length,
              }))()"""
        )
        print("左右分边:", json.dumps(sides))
        check("左侧行存在", sides.get("left", 0) > 0, str(sides))
        check("右侧行存在", sides.get("right", 0) > 0, str(sides))
        shot("duet_sides.png")

        # 跳到同唱时刻，验证两行同时高亮 + 逐词进度
        evaljs(
            f"""(() => {{
              const audio = document.querySelector('[data-lyr-audio]');
              audio.currentTime = {overlap_start} / 1000;
              return audio.currentTime;
            }})()"""
        )
        time.sleep(1.4)
        actives = evaljs(
            """(() => {
              const act = [...document.querySelectorAll('.lyr__line.is-active')];
              return {
                count: act.length,
                sides: act.map(e => e.classList.contains('is-right') ? 'R' : 'L'),
                wordsWithProgress: act.reduce((n, e) => n + [...e.querySelectorAll('.lyr__w')]
                  .filter(w => (w.style.getPropertyValue('--p') || '0') !== '0').length, 0),
              };
            })()"""
        )
        print("同唱高亮:", json.dumps(actives))
        check("重叠时两行同时高亮", actives.get("count") == 2, str(actives))
        check("两行左右各一", sorted(actives.get("sides") or []) == ["L", "R"], str(actives.get("sides")))
        check("逐词进度在走", actives.get("wordsWithProgress", 0) > 0, str(actives))
        shot("duet_overlap.png")

        # ---- 翻译开关 ----
        trans_visible_before = evaljs(
            """(() => {
              const t = document.querySelector('.lyr__line .lyr__trans');
              return t ? getComputedStyle(t).display !== 'none' : null;
            })()"""
        )
        evaljs("document.querySelector('[data-lyr-settings]').click()")
        time.sleep(0.6)
        # 点开关轨道（label 转发点击）
        evaljs("document.querySelector('[data-lyr-trans]').closest('.switch').querySelector('.switch__track').click()")
        time.sleep(0.6)
        switch_state = evaljs(
            """(() => ({
                checked: document.querySelector('[data-lyr-trans]').checked,
                noTrans: document.querySelector('.lyr').classList.contains('no-trans'),
                transDisplay: (() => { const t = document.querySelector('.lyr__line .lyr__trans');
                  return t ? getComputedStyle(t).display : 'missing'; })(),
              }))()"""
        )
        print("关闭翻译后:", json.dumps(switch_state))
        check("开关点击生效(checked=false)", switch_state.get("checked") is False, str(switch_state))
        check("no-trans 类已加", switch_state.get("noTrans") is True)
        check("翻译行已隐藏", switch_state.get("transDisplay") == "none", str(switch_state.get("transDisplay")))
        check("之前翻译可见", trans_visible_before is True, str(trans_visible_before))

        # 持久化验证（防抖 400ms；PATCH 空体不改值，返回当前设置）
        time.sleep(0.8)
        _, me, _ = http("PATCH", "/api/me/settings", {}, cookie=cookie)
        saved = (me.get("settings") or {}).get("lyricsStyle") or {}
        check("showTrans=false 已保存", saved.get("showTrans") is False, str(saved))

        # 再点开
        evaljs("document.querySelector('[data-lyr-trans]').closest('.switch').querySelector('.switch__track').click()")
        time.sleep(0.4)
        re_state = evaljs(
            """(() => ({
                checked: document.querySelector('[data-lyr-trans]').checked,
                noTrans: document.querySelector('.lyr').classList.contains('no-trans'),
              }))()"""
        )
        check("再点恢复显示", re_state.get("checked") is True and re_state.get("noTrans") is False, str(re_state))

        # 恢复默认样式（清理账号偏好）
        _, _, _ = http("PATCH", "/api/me/settings", {"lyricsStyle": {}}, cookie=cookie)
    finally:
        proc.kill()

    # ---- 清理测试数据 ----
    _, _, _ = http("DELETE", f"/api/nodes/{node['id']}/purge", cookie=cookie)
    print("已清理节点", node["id"])
    print()
    print("失败项:", failures if failures else "无")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
