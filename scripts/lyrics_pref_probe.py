# -*- coding: utf-8 -*-
"""歌词来源偏好 + 样式设置端到端探针（0.3.5）。

覆盖：内嵌歌词优先 / 搜索指定歌曲 / 自定义文本 / 无歌词 / auto 回退链 /
偏好读写回环 / 用户样式保存与白名单 / CDP 面板交互。

用法：python scripts/lyrics_pref_probe.py [端口]
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import websocket

urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8020
TAG = f"pref{PORT}"
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
USER = "lyrpref"
PASS = "LyricPref#2026"
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
    """带标题/歌手/封面/USLT 内嵌歌词的 mp3（内嵌与在线歌名刻意错开）。"""
    from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, USLT
    from mutagen.mp3 import MP3

    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
    audio = frame * int(5 * 44100 * 144 * 128 / (44100 * 413))
    tmp = Path(tempfile.mkdtemp(prefix="lyrpref_")) / "Embedded Priority Test.mp3"
    tmp.write_bytes(audio)
    tags = ID3()
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
    )
    tags.add(TIT2(encoding=3, text="Embedded Priority Test"))
    tags.add(TPE1(encoding=3, text="CloudNETP"))
    tags.add(TALB(encoding=3, text="Pref Probe"))
    tags.add(USLT(encoding=3, lang="eng", desc="", text="[00:00.50]内嵌第一行\n[00:02.00]内嵌第二行"))
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

    mp3_bytes = make_test_mp3()
    # WebDAV PUT 上传（同旧探针）
    import urllib.parse

    auth = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
    req = urllib.request.Request(
        f"{BASE}/dav/{urllib.parse.quote('Embedded Priority Test.mp3')}", data=mp3_bytes, method="PUT"
    )
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        print("WebDAV 上传:", resp.status)
    status, listing, _ = http("GET", "/api/nodes", cookie=cookie)
    node = next(n for n in listing["items"] if n["name"].endswith(".mp3"))
    node_id = node["id"]
    print("节点 id:", node_id)

    base = f"/api/nodes/{node_id}"

    # ---- 1. 默认 auto：内嵌歌词优先 ----
    status, lyr, _ = http("GET", f"{base}/lyrics", cookie=cookie)
    check("auto 解析 200", status == 200, str(lyr))
    check("auto → source=embedded", lyr.get("source") == "embedded", str(lyr.get("source")))
    check("内嵌行数=2", len(lyr.get("lines", [])) == 2, str(lyr.get("lines")))
    check("内嵌时间戳", lyr["lines"] and lyr["lines"][0]["start"] == 500, str(lyr.get("lines", [{}])[0]))

    # pref 默认值
    status, pref, _ = http("GET", f"{base}/lyrics-pref", cookie=cookie)
    check("pref 默认 auto", status == 200 and pref.get("mode") == "auto", str(pref))

    # ---- 2. 指定歌曲（走在线歌词库） ----
    status, search, _ = http("GET", "/api/lyrics/search?q=Monday%20Morning%20CG5", cookie=cookie)
    online_ok = status == 200 and search.get("items")
    if online_ok:
        item = search["items"][0]
        status, pref, _ = http(
            "PUT", f"{base}/lyrics-pref",
            {"mode": "song", "songId": item["id"], "songTitle": item.get("title", ""), "songArtist": item.get("artist", "")},
            cookie=cookie,
        )
        check("PUT song pref", status == 200 and pref.get("mode") == "song", str(pref))
        status, lyr, _ = http("GET", f"{base}/lyrics", cookie=cookie)
        check("song → source=song", status == 200 and lyr.get("source") == "song", str(lyr)[:200])
        check("song matched id", (lyr.get("matched") or {}).get("id") == item["id"], str(lyr.get("matched")))
        check("song 有歌词行", len(lyr.get("lines", [])) > 0)
        # 非法 songId → 4xx
        status, err, _ = http("PUT", f"{base}/lyrics-pref", {"mode": "song", "songId": 999999999}, cookie=cookie)
        check("非法 songId 被拒", status in (404, 424, 503), f"{status} {err}")
    else:
        print("WARN 在线歌词库不可达，跳过 song 模式断言", status, search)

    # ---- 3. 自定义 LRC ----
    status, pref, _ = http(
        "PUT", f"{base}/lyrics-pref",
        {"mode": "custom", "customLyrics": "[00:01.00]自定义第一行\n[00:03.00]自定义第二行"},
        cookie=cookie,
    )
    check("PUT custom pref", status == 200 and pref.get("mode") == "custom", str(pref))
    status, lyr, _ = http("GET", f"{base}/lyrics", cookie=cookie)
    check("custom → source=custom", status == 200 and lyr.get("source") == "custom", str(lyr)[:150])
    check("自定义行解析", len(lyr.get("lines", [])) == 2 and lyr["lines"][0]["start"] == 1000, str(lyr.get("lines")))

    # 纯文本自定义 → unsynced
    status, pref, _ = http("PUT", f"{base}/lyrics-pref", {"mode": "custom", "customLyrics": "纯文本甲\n纯文本乙"}, cookie=cookie)
    status, lyr, _ = http("GET", f"{base}/lyrics", cookie=cookie)
    check("纯文本 unsynced", lyr.get("unsynced") is True and len(lyr.get("lines", [])) == 2, str(lyr)[:150])

    # 空内容被拒
    status, err, _ = http("PUT", f"{base}/lyrics-pref", {"mode": "custom", "customLyrics": "  "}, cookie=cookie)
    check("空自定义被拒", status == 400, f"{status} {err}")

    # ---- 4. 无歌词 ----
    status, pref, _ = http("PUT", f"{base}/lyrics-pref", {"mode": "none"}, cookie=cookie)
    status, lyr, _ = http("GET", f"{base}/lyrics", cookie=cookie)
    check("none → 空", status == 200 and lyr.get("source") == "none" and not lyr.get("lines"), str(lyr))

    # ---- 5. 回 auto：又回到内嵌优先 ----
    status, pref, _ = http("PUT", f"{base}/lyrics-pref", {"mode": "auto"}, cookie=cookie)
    status, lyr, _ = http("GET", f"{base}/lyrics", cookie=cookie)
    check("auto 回退到内嵌", lyr.get("source") == "embedded", str(lyr.get("source")))

    # pref 回环
    status, pref, _ = http("GET", f"{base}/lyrics-pref", cookie=cookie)
    check("pref 回环 auto/空 song", pref.get("mode") == "auto" and pref.get("song") is None, str(pref))

    # ---- 6. 用户样式：保存 + 白名单 ----
    status, me, _ = http(
        "PATCH", "/api/me/settings",
        {"lyricsStyle": {"fontSize": 30, "hotColor": "#ffd166", "showTrans": False, "hacked": "x"}},
        cookie=cookie,
    )
    style = (me.get("settings") or {}).get("lyricsStyle") if status == 200 else None
    check("样式保存", status == 200 and style == {"fontSize": 30, "hotColor": "#ffd166", "showTrans": False}, str(me)[:200])
    status, _, _ = http("PATCH", "/api/me/settings", {"lyricsStyle": {}}, cookie=cookie)
    check("样式清空", status == 200)

    # ---- 7. CDP：面板交互 ----
    profile = Path(tempfile.mkdtemp(prefix="cdp_lyrpref_"))
    port = 9433
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

        evaljs(
            f"""import('/static/js/ui/viewer.js').then(m => m.openViewer({{id: {node_id},
                 type: 'file', name: 'Embedded Priority Test.mp3', size: 1, previewKind: 'audio'}}))"""
        )
        time.sleep(3.0)
        report = evaljs(
            """(() => {
              const lyr = document.querySelector('.lyr');
              if (!lyr) return {opened: false};
              return {
                opened: true,
                lineCount: lyr.querySelectorAll('.lyr__line').length,
                firstLine: lyr.querySelector('.lyr__line .lyr__orig')?.textContent,
                hasSettings: Boolean(lyr.querySelector('[data-lyr-settings]')),
                empty: !lyr.querySelector('[data-lyr-empty]')?.hidden
                         ? lyr.querySelector('[data-lyr-empty]').textContent : null,
              };
            })()"""
        )
        print(json.dumps(report, ensure_ascii=False))
        check("预览打开", report.get("opened"))
        check("内嵌歌词渲染", report.get("lineCount") == 2, str(report.get("lineCount")))
        check("设置按钮存在", report.get("hasSettings"))

        # 打开设置面板
        evaljs("document.querySelector('[data-lyr-settings]').click()")
        time.sleep(0.8)
        panel = evaljs(
            """(() => {
              const p = document.querySelector('[data-lyr-panel]');
              return {
                open: p && !p.hidden,
                activeSrc: p?.querySelector('.segmented__btn.is-active')?.dataset.src,
                hint: p?.querySelector('[data-lyr-srchint]')?.textContent,
                panes: [...p?.querySelectorAll('[data-srcpane]') || []].map(e => ({k: e.dataset.srcpane, hidden: e.hidden})),
                swatches: p?.querySelectorAll('.lyr__swatch').length,
              };
            })()"""
        )
        print(json.dumps(panel, ensure_ascii=False))
        check("面板打开", panel.get("open"))
        check("默认 auto 高亮", panel.get("activeSrc") == "auto", str(panel.get("activeSrc")))
        check("优先级提示语", "内嵌" in (panel.get("hint") or ""), str(panel.get("hint")))
        check("颜色板 6 个", panel.get("swatches") == 6, str(panel.get("swatches")))
        shot("panel-auto")

        # 切到「使用文本」pane，填入歌词保存
        evaljs(
            """(() => {
              const seg = document.querySelector('[data-lyr-srcseg]');
              seg.querySelector('[data-src="custom"]').click();
              const ta = document.querySelector('[data-lyr-custom]');
              ta.value = '[00:01.00]面板写入第一行\\n[00:04.00]面板写入第二行';
              ta.dispatchEvent(new Event('input', {bubbles: true}));
              document.querySelector('[data-lyr-save-custom]').click();
            })()"""
        )
        time.sleep(1.5)
        after = evaljs(
            """(() => {
              const lyr = document.querySelector('.lyr');
              return {
                panelClosed: document.querySelector('[data-lyr-panel]')?.hidden,
                lineCount: lyr.querySelectorAll('.lyr__line').length,
                firstLine: lyr.querySelector('.lyr__line .lyr__orig')?.textContent,
              };
            })()"""
        )
        print(json.dumps(after, ensure_ascii=False))
        check("custom 保存后面板关闭", after.get("panelClosed"), str(after))
        check("custom 歌词生效", after.get("firstLine") == "面板写入第一行", str(after.get("firstLine")))

        # 样式：字号 + 高亮色 + 翻译开关
        evaljs(
            """(() => {
              document.querySelector('[data-lyr-settings]').click();
            })()"""
        )
        time.sleep(0.6)
        evaljs(
            """(() => {
              const size = document.querySelector('[data-lyr-size]');
              size.value = 30;
              size.dispatchEvent(new Event('input', {bubbles: true}));
              const color = document.querySelector('[data-lyr-color]');
              color.value = '#ffd166';
              color.dispatchEvent(new Event('input', {bubbles: true}));
              const trans = document.querySelector('[data-lyr-trans]');
              trans.checked = false;
              trans.dispatchEvent(new Event('change', {bubbles: true}));
            })()"""
        )
        time.sleep(1.0)
        style = evaljs(
            """(() => {
              const lyr = document.querySelector('.lyr');
              const cs = getComputedStyle(lyr.querySelector('.lyr__orig'));
              return {
                fontSize: cs.fontSize,
                hot: lyr.style.getPropertyValue('--lyr-hot').trim(),
                noTrans: lyr.classList.contains('no-trans'),
              };
            })()"""
        )
        print(json.dumps(style, ensure_ascii=False))
        check("字号 30 生效", style.get("fontSize") == "30px", str(style.get("fontSize")))
        check("高亮色生效", style.get("hot") == "#ffd166", str(style.get("hot")))
        check("翻译关闭生效", style.get("noTrans") is True)
        shot("panel-style")

        # 刷新后样式从账号设置恢复
        evaljs("location.reload()")
        time.sleep(4.0)
        evaljs(
            f"""import('/static/js/ui/viewer.js').then(m => m.openViewer({{id: {node_id},
                 type: 'file', name: 'Embedded Priority Test.mp3', size: 1, previewKind: 'audio'}}))"""
        )
        time.sleep(3.0)
        restored = evaljs(
            """(() => {
              const lyr = document.querySelector('.lyr');
              return {
                fontSize: getComputedStyle(lyr.querySelector('.lyr__orig')).fontSize,
                hot: lyr.style.getPropertyValue('--lyr-hot').trim(),
                noTrans: lyr.classList.contains('no-trans'),
                firstLine: lyr.querySelector('.lyr__line .lyr__orig')?.textContent,
              };
            })()"""
        )
        print(json.dumps(restored, ensure_ascii=False))
        check("刷新后字号保持 30", restored.get("fontSize") == "30px", str(restored.get("fontSize")))
        check("刷新后高亮保持", restored.get("hot") == "#ffd166", str(restored.get("hot")))
        check("刷新后自定义歌词保持", restored.get("firstLine") == "面板写入第一行", str(restored.get("firstLine")))
        shot("after-reload")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()

    print()
    print("FAILED:", failures if failures else "无")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
