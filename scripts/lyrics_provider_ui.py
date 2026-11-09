# -*- coding: utf-8 -*-
"""歌词「来源分类」端到端：网易云（真连）+ 歌词库（本地假上游）。

验证的是「.env 里同时启用两个来源」之后：
  1. /api/lyrics/providers 返回两项，默认项是配置里的第一个可用来源
  2. /api/lyrics/search?provider=lyriclib 走歌词库、provider=netease 走网易云
  3. 未启用的来源 / 不认识的来源 → 400
  4. UI：歌词设置 → 搜索指定，出现「网易云 / 歌词库」分类；
     点「歌词库」后搜索请求带 provider=lyriclib，结果带歌词库标签；
     点选结果后偏好存成 provider=lyriclib，播放器角标变成「歌词库」
  5. 全程无 JS 报错

跑法（会真连网易云，其余全在本地）：
    ./.venv/Scripts/python.exe scripts/lyrics_provider_ui.py
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"

passed = 0
failed = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {label} {extra}")
    else:
        failed += 1
        print(f"  FAIL {label} {extra}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_up(base: str, proc: subprocess.Popen, log_path: Path) -> bool:
    for _ in range(80):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"{base}/api/version", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    print(log_path.read_text(encoding="utf-8", errors="replace")[-1200:])
    return False


def api_json(url: str, cookie: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Cookie": f"cloudnetp_session={cookie}",
            **({"Content-Type": "application/json"} if data else {}),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception:
            return exc.code, {}


def make_wav(path: Path) -> bytes:
    """0.6 秒正弦波 + ID3 标题/歌手，让自动匹配有元数据可用。"""
    import math
    import struct

    rate = 8000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * 0.6)):
            frames += struct.pack("<h", int(12000 * math.sin(i * 2 * math.pi * 440 / rate)))
        w.writeframes(bytes(frames))

    from mutagen.id3 import TIT2, TPE1
    from mutagen.wave import WAVE

    audio = WAVE(str(path))
    audio["TIT2"] = TIT2(encoding=3, text="晴天")
    audio["TPE1"] = TPE1(encoding=3, text="周杰伦")
    audio.save()
    return path.read_bytes()


# --------------------------------------------------------------------------- #
# 假歌词库上游
# --------------------------------------------------------------------------- #
class LyriclibFake(BaseHTTPRequestHandler):
    hits: list[str] = []

    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        LyriclibFake.hits.append(self.path)
        if "/search.php" in self.path:
            payload = {
                "ok": True,
                "data": {
                    "items": [
                        {
                            "id": 90001,
                            "title": "晴天（歌词库）",
                            "artist": "周杰伦",
                            "album": "叶惠美",
                            "duration_ms": 269000,
                        }
                    ]
                },
            }
        else:
            payload = {
                "ok": True,
                "data": {
                    "granularity": "line",
                    "lines": [
                        {"start": 1000, "text": "歌词库第一行"},
                        {"start": 3000, "text": "歌词库第二行"},
                    ],
                },
            }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    from app.config import settings
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="lyr-prov-ui-")).resolve()
    db_path = tmp / "smoke.db"
    storage = tmp / "storage"

    # ---- 造库：一个管理员 + 一首带 ID3 元数据的 WAV ----
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import Base, Blob, Node, User
    from app.user_service import ensure_settings

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    now = datetime.utcnow()
    admin = User(
        username="sakura",
        password_hash=hash_password("Cloud@2026"),
        display_name="樱花",
        role=1,
        quota_bytes=1024**3,
        token_version=1,
        created_at=now,
    )
    db.add(admin)
    db.commit()
    ensure_settings(db, admin.id)

    payload = make_wav(tmp / "seed.wav")
    sha = hashlib.sha256(payload).hexdigest()
    blob_path = storage / "blobs" / sha[:2] / sha[2:4] / sha
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(payload)
    db.add(Blob(sha256=sha, size=len(payload), ref_count=1, created_at=now))
    node = Node(
        user_id=admin.id,
        parent_id=None,
        type="file",
        name="晴天.wav",
        blob_sha256=sha,
        size=len(payload),
        mime="audio/wav",
        created_at=now - timedelta(minutes=5),
        updated_at=now - timedelta(minutes=5),
    )
    db.add(node)
    db.commit()
    node_id, admin_id, admin_tv = node.id, admin.id, admin.token_version
    db.close()

    cookie = create_token(admin_id, admin_tv)

    # ---- 假歌词库 + 起服务（两个来源同时启用）----
    fake = ThreadingHTTPServer(("127.0.0.1", 0), LyriclibFake)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    fake_base = f"http://127.0.0.1:{fake.server_address[1]}/api/v1"

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
            "STORAGE_DIR": str(storage),
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "RELOAD": "false",
            "DB_PROBE_INTERVAL": "0",
            "DB_FALLBACK_ENABLED": "0",
            "SECRET_KEY": settings.SECRET_KEY,
            "LYRICS_PROVIDERS": "netease,lyriclib",
            "LYRICS_API_BASE": fake_base,
            "LYRICS_API_KEY": "test-key",
            "LYRICS_API_COOKIE": "lib_session=smoke",
        }
    )
    log_path = tmp / "server.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "main.py"], cwd=str(ROOT), env=env,
        stdout=log_file, stderr=subprocess.STDOUT,
    )

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来")

        print("\n[1] 来源列表")
        status, data = api_json(f"{base}/api/lyrics/providers", cookie)
        items = data.get("items") or []
        check("providers 200", status == 200, str(status))
        check("两个来源都在", [i["id"] for i in items] == ["netease", "lyriclib"], items)
        check("默认来源是网易云", data.get("default") == "netease", str(data.get("default")))
        check("都标记为可用", all(i["ready"] for i in items), items)

        print("\n[2] 按分类搜索")
        status, data = api_json(
            f"{base}/api/lyrics/search?q={urllib.parse.quote('晴天')}&provider=lyriclib", cookie
        )
        check("歌词库搜索 200", status == 200, str(status))
        check("结果是歌词库的条目", (data.get("items") or [{}])[0].get("provider") == "lyriclib", data.get("items"))
        check("回带本次来源", data.get("provider") == "lyriclib", str(data.get("provider")))
        check("假上游收到搜索请求", any("/search.php" in h for h in LyriclibFake.hits), LyriclibFake.hits)

        status, data = api_json(
            f"{base}/api/lyrics/search?q={urllib.parse.quote('晴天')}&provider=netease", cookie
        )
        check("网易云搜索 200", status == 200, str(status))
        check("结果是网易云的条目", (data.get("items") or [{}])[0].get("provider") == "netease", data.get("items"))

        status, data = api_json(
            f"{base}/api/lyrics/search?q=x&provider=qqmusic", cookie
        )
        check("不认识的来源 → 400", status == 400, str(status))

        print("\n[3] 界面端到端")
        from playwright.sync_api import sync_playwright

        errors: list[str] = []
        search_calls: list[str] = []
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 860})
            ctx.add_cookies(
                [
                    {
                        "name": "cloudnetp_session",
                        "value": cookie,
                        "domain": "127.0.0.1",
                        "path": "/",
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on(
                "request",
                lambda r: search_calls.append(r.url) if "/api/lyrics/search" in r.url else None,
            )

            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_selector(".tile", timeout=20000)
            page.wait_for_timeout(600)
            page.dblclick('.tile[data-type="file"]')
            page.wait_for_selector(".viewer [data-lyr-list]", timeout=20000)
            page.wait_for_selector(".viewer [data-lyr-panel]", state="attached", timeout=20000)

            # 打开歌词设置 → 搜索指定：自动按默认来源（网易云）搜一次
            page.click(".viewer [data-lyr-settings]")
            page.wait_for_selector(".viewer [data-lyr-panel]:not([hidden])", timeout=8000)
            page.click('.viewer [data-src="song"]')
            page.wait_for_selector(".viewer [data-lyr-srcpick]:not([hidden])", timeout=15000)
            chips = page.locator(".viewer .lyr__src")
            check("分类出现两个", chips.count() == 2, str(chips.count()))
            labels = [chips.nth(i).inner_text() for i in range(chips.count())]
            check("分类是网易云/歌词库", labels == ["网易云", "歌词库"], labels)
            active = page.locator(".viewer .lyr__src.is-active").inner_text()
            check("默认选中网易云", active.strip() == "网易云", active)

            page.wait_for_selector(".viewer .lyr__result-tag", timeout=25000)
            tag = page.inner_text(".viewer .lyr__result-tag")
            check("网易云分类出网易云结果", tag.strip() == "网易云", tag)
            check("默认搜索带 provider", any("provider=netease" in u for u in search_calls), search_calls[-3:])

            # 切到歌词库：应换来源重搜，结果带歌词库标签
            page.click('.viewer .lyr__src:has-text("歌词库")')
            page.wait_for_function(
                "() => document.querySelector('.viewer .lyr__result-tag')?.textContent.trim() === '歌词库'",
                timeout=20000,
            )
            check("切来源后请求带 provider=lyriclib", any("provider=lyriclib" in u for u in search_calls), search_calls[-3:])
            check("假上游收到搜索请求（UI）", any("/search.php" in h for h in LyriclibFake.hits))

            check("切来源后面板仍然开着", page.evaluate(
                "() => !document.querySelector('.viewer .lyr__panel')?.hidden"))
            page.screenshot(path=str(tmp / "lyrics_provider_panel.png"))
            page.click(".viewer .lyr__result")
            page.wait_for_timeout(1200)

            status, pref = api_json(f"{base}/api/nodes/{node_id}/lyrics-pref", cookie)
            check("偏好存成歌词库", (pref.get("song") or {}).get("provider") == "lyriclib", pref)

            page.wait_for_function(
                "() => (document.querySelector('.viewer [data-meta]')?.textContent || '').includes('歌词库')",
                timeout=20000,
            )
            meta_text = page.inner_text(".viewer [data-meta]")
            check("播放器角标显示歌词库", "歌词库" in meta_text, meta_text.strip()[-40:])

            page.screenshot(path=str(tmp / "lyrics_provider_badge.png"))
            browser.close()

        check("无 JS 报错", not errors, "; ".join(errors[:2]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    # ---- 只启用一个来源时不该出现分类切换 ----
    print("\n[4] 只启用网易云：分类应隐藏")
    port2 = free_port()
    base2 = f"http://127.0.0.1:{port2}"
    env2 = dict(env)
    env2.update({"PORT": str(port2), "LYRICS_PROVIDERS": "netease", "LYRICS_PROVIDER": "netease"})
    log2 = tmp / "server2.log"
    log2_file = open(log2, "w", encoding="utf-8", errors="replace")
    proc2 = subprocess.Popen(
        [str(VENV_PY), "main.py"], cwd=str(ROOT), env=env2,
        stdout=log2_file, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_up(base2, proc2, log2):
            raise SystemExit("第二实例没起来")
        from playwright.sync_api import sync_playwright

        errors2: list[str] = []
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 860})
            ctx.add_cookies(
                [
                    {
                        "name": "cloudnetp_session",
                        "value": cookie,
                        "domain": "127.0.0.1",
                        "path": "/",
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors2.append(str(e)))
            page.goto(f"{base2}/#/files", wait_until="domcontentloaded")
            page.wait_for_selector(".tile", timeout=20000)
            page.wait_for_timeout(600)
            page.dblclick('.tile[data-type="file"]')
            page.wait_for_selector(".viewer [data-lyr-list]", timeout=20000)
            page.click(".viewer [data-lyr-settings]")
            page.wait_for_selector(".viewer [data-lyr-panel]:not([hidden])", timeout=8000)
            page.click('.viewer [data-src="song"]')
            page.wait_for_timeout(1500)  # 等来源列表拉回来
            check(
                "单来源时不显示分类",
                page.evaluate("() => !!document.querySelector('.viewer [data-lyr-srcpick]')?.hidden"),
            )
            browser.close()
        check("单来源实例无 JS 报错", not errors2, "; ".join(errors2[:2]))
    finally:
        proc2.terminate()
        try:
            proc2.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc2.kill()
        log2_file.close()
        fake.shutdown()

    print(f"\n通过 {passed}/{passed + failed}")
    print(f"截图目录：{tmp}")
    if failed:
        print("服务日志尾部：")
        print(log_path.read_text(encoding="utf-8", errors="replace")[-800:])
    raise SystemExit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
