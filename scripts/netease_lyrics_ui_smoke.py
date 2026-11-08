# -*- coding: utf-8 -*-
"""网易云歌词的前端端到端回归（临时库 + 临时存储起独立实例，不碰真实数据）。

  1. /api/lyrics/search 走网易云（items 带 provider=netease）
  2. 「搜索指定」偏好带 provider 保存并生效：/api/nodes/{id}/lyrics 返回
     provider / granularity，歌词行带时间轴
  3. UI：打开音频歌词播放器后信息栏出现「网易云 · 逐行」角标；设置面板里
     搜索结果带「网易云」标签；全程无 JS 报错

    ./.venv/Scripts/python.exe scripts/netease_lyrics_ui_smoke.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from datetime import datetime, timedelta
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


def get_json(url: str, cookie: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"Cookie": f"cloudnetp_session={cookie}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def make_wav(path: Path) -> bytes:
    """0.6 秒的正弦波 WAV，再写 ID3 标题/歌手，让自动匹配有元数据可用。"""
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


def main() -> None:
    from app.config import settings
    from app.security import create_token
    from app.security import hash_password

    tmp = Path(tempfile.mkdtemp(prefix="netease-ui-")).resolve()
    db_path = tmp / "smoke.db"
    storage = tmp / "storage"

    # ---- 造库：一个管理员 + 一首带 ID3 元数据的 WAV ----
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import Blob, Node, User, Base
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

    wav_path = tmp / "seed.wav"
    payload = make_wav(wav_path)
    import hashlib

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
    node_id = node.id
    admin_id = admin.id
    admin_tv = admin.token_version
    db.close()

    cookie = create_token(admin_id, admin_tv)

    # ---- 起服务 ----
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

        print("\n[1] 接口层")
        status, body = get_json(f"{base}/api/lyrics/search?q={urllib.parse.quote('晴天 周杰伦')}", cookie)
        check("歌词搜索 200", status == 200, str(status))
        items = __import__("json").loads(body).get("items", []) if status == 200 else []
        check("搜索结果来自网易云", bool(items) and items[0].get("provider") == "netease",
              items[0]["title"] if items else "无结果")

        status, body = get_json(
            f"{base}/api/nodes/{node_id}/lyrics-pref", cookie
        )
        check("读偏好 200", status == 200, str(status))

        # 直接调 PUT：mode=song + provider=netease，指到确定有歌词的原曲
        origin_id = 186016  # 晴天（周杰伦）——匿名也返回逐行歌词
        req = urllib.request.Request(
            f"{base}/api/nodes/{node_id}/lyrics-pref",
            data=json.dumps(
                {
                    "mode": "song",
                    "provider": "netease",
                    "songId": origin_id,
                    "songTitle": "晴天",
                    "songArtist": "周杰伦",
                }
            ).encode(),
            headers={
                "Cookie": f"cloudnetp_session={cookie}",
                "Content-Type": "application/json",
            },
            method="PUT",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                put_status = resp.status
                put_body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            put_status = exc.code
            put_body = {}
        check("偏好保存 200", put_status == 200, str(put_status))
        check("偏好里存了 provider", put_body.get("song", {}).get("provider") == "netease",
              str(put_body.get("song")))

        status, body = get_json(f"{base}/api/nodes/{node_id}/lyrics", cookie)
        data = json.loads(body) if status == 200 else {}
        check("歌词解析 200", status == 200, str(status))
        check("来源是网易云", data.get("provider") == "netease", str(data.get("provider")))
        check("粒度是逐行（无 Cookie）", data.get("granularity") == "line", str(data.get("granularity")))
        check("歌词行数 > 0", len(data.get("lines") or []) > 0, f"{len(data.get('lines') or [])} 行")
        first = (data.get("lines") or [{}])[0]
        check("行带时间轴", first.get("start", 0) > 0, str(first.get("start")))

        print("\n[2] 界面端到端")
        from playwright.sync_api import sync_playwright

        errors: list[str] = []
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

            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_selector(".tile", timeout=20000)
            page.wait_for_timeout(800)
            page.dblclick('.tile[data-type="file"]')  # 双击打开：晴天.wav → 歌词播放器
            page.wait_for_selector(".viewer [data-lyr-list]", timeout=20000)

            # 自动匹配已在 [1] 被覆盖成 song+netease：打开即出歌词
            page.wait_for_selector(".viewer [data-lyr-list] li", timeout=30000)
            meta_text = page.inner_text(".viewer [data-meta]")
            check("信息栏出现网易云来源", "网易云" in meta_text, meta_text.strip()[-30:])

            # 设置面板：搜索结果带网易云标签
            page.click(".viewer [data-lyr-settings]")
            page.wait_for_selector(".viewer [data-lyr-panel]:not([hidden])", timeout=8000)
            page.fill(".viewer [data-lyr-search]", "晴天 周杰伦")
            page.wait_for_selector(".viewer .lyr__result-tag", timeout=20000)
            tag = page.inner_text(".viewer .lyr__result-tag")
            check("搜索结果带网易云标签", tag.strip() == "网易云", tag)

            # 关掉面板，确认歌词行是有正文的（不只是空 li）
            page.click(".viewer [data-lyr-panel-close]")
            page.wait_for_timeout(400)
            first_line = page.inner_text(".viewer [data-lyr-list] li")
            check("歌词行有正文", bool(first_line.strip()), first_line.strip()[:20])

            page.screenshot(path=str(tmp / "lyrics.png"), full_page=False)
            browser.close()

        check("无 JS 报错", not errors, "; ".join(errors[:2]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    print(f"\n通过 {passed}/{passed + failed}")
    if failed:
        print("服务日志尾部：")
        print(log_path.read_text(encoding="utf-8", errors="replace")[-800:])
    raise SystemExit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
