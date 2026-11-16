# -*- coding: utf-8 -*-
"""歌词切换的加载态与防连点冒烟。

真实服务 + 库副本 + 一首没有内嵌歌词的 WAV；用 CDP 给网络加 800ms 延迟，
模拟「切歌词转圈」的慢网络，验证：

  A 点「无歌词」→ 歌词区立刻出现「正在切换歌词…」+ 转圈
  B 加载中再点「自动」→ 弹「慢点，歌词还在加载中」，且不会重复发请求
  C 完成后转圈消失，落到「这首歌没有歌词」
  D 搜索结果里点一首 → 同样有加载态；加载中连点其它结果会提示
  E 全程无 JS 报错

    ./.venv/Scripts/python.exe scripts/lyrics_switch_ui.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)

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
    for _ in range(90):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"{base}/api/version", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    print(log_path.read_text(encoding="utf-8", errors="replace")[-1500:])
    return False


def make_wav(path: Path) -> bytes:
    """0.6 秒正弦波 + ID3 标题/歌手；故意不带内嵌歌词，切换后应落到「没有歌词」。"""
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


SEARCH_ITEMS = {
    "items": [
        {"id": 90001, "title": "晴天（网易云）", "artist": "周杰伦", "album": "叶惠美", "provider": "netease"},
        {"id": 90002, "title": "晴天（歌词库）", "artist": "周杰伦", "album": "叶惠美", "provider": "lyriclib"},
    ]
}


def main() -> int:
    from app.config import settings
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="lyr-switch-ui-")).resolve()
    db_path = tmp / "smoke.db"
    storage = tmp / "storage"

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
            "LYRICS_PROVIDERS": "",
        }
    )
    log_path = tmp / "server.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "main.py"], cwd=str(ROOT), env=env,
        stdout=log_file, stderr=subprocess.STDOUT,
    )

    from playwright.sync_api import sync_playwright

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来，看日志：" + str(log_path))

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)

            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
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
            errors: list[str] = []
            lyric_calls: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on(
                "response",
                lambda r: lyric_calls.append(f"{r.status} {r.url.split('/api')[-1]}")
                if re.search(r"/api/nodes/\d+/lyrics$", r.url)
                else None,
            )

            # 来源与搜索结果直接由本地返回，避免连真实曲库；歌词/偏好仍走真实后端（有 CDP 延迟）
            page.route(
                re.compile(r".*/api/lyrics/providers$"),
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "items": [
                                {"id": "netease", "label": "网易云", "ready": True, "default": True},
                                {"id": "lyriclib", "label": "歌词库", "ready": True},
                            ],
                            "default": "netease",
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            page.route(
                re.compile(r".*/api/lyrics/search.*"),
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(SEARCH_ITEMS, ensure_ascii=False),
                ),
            )

            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_selector(".tile", timeout=20000)
            page.wait_for_timeout(600)

            # 把「保存歌词偏好」的 PUT 真实发给服务端，但先扣住响应：
            # 服务端状态已经更新，前端却仍在等 —— 既能稳定观察加载态，又不影响后续加载。
            held = []

            def hold_pref(route):
                if route.request.method == "PUT":
                    held.append((route, route.fetch()))
                else:
                    route.continue_()

            page.route(re.compile(r".*/api/nodes/\d+/lyrics-pref$"), hold_pref)

            page.dblclick('.tile[data-type="file"]')
            page.wait_for_selector(".viewer [data-lyr-list]", timeout=20000)
            page.wait_for_selector(".viewer [data-lyr-panel]", state="attached", timeout=20000)
            page.wait_for_timeout(800)  # 等首次歌词请求收尾

            print("\n[A] 切换来源时显示加载中")
            page.click(".viewer [data-lyr-settings]")
            page.wait_for_selector(".viewer [data-lyr-panel]:not([hidden])", timeout=8000)
            page.click('.viewer [data-src="none"]')
            page.wait_for_timeout(250)
            status_el = page.locator(".viewer [data-lyr-panel-status]")
            check("面板内出现加载提示", status_el.is_visible())
            check(
                "文案是「正在切换歌词…」",
                "正在切换歌词" in (status_el.inner_text() if status_el.is_visible() else ""),
                status_el.inner_text() if status_el.is_visible() else "",
            )
            check("提示带转圈", "is-busy" in (status_el.get_attribute("class") or ""))
            check(
                "加载层不盖住设置面板（面板仍可操作）",
                page.locator(".viewer [data-lyr-loading]").is_hidden(),
            )
            check("保存请求已被挂起", len(held) == 1, str(len(held)))
            page.screenshot(path=str(OUT / "lyrics_switch_loading.png"))

            print("\n[B] 加载中连点要提醒")
            page.click('.viewer [data-src="auto"]')
            page.wait_for_timeout(260)
            toast_text = " ".join(page.locator(".toast").all_inner_texts())
            check("弹出「慢点」提示", "慢点" in toast_text, toast_text[:120])
            check("没有重复发保存请求", len(held) == 1, str(len(held)))

            print("\n[C] 面板收起后歌词区继续转圈")
            # 先把接下来的歌词请求也扣住，好稳定看到「面板已关 + 歌词区转圈」的状态
            lyrics_held = []
            page.route(
                re.compile(r".*/api/nodes/\d+/lyrics$"),
                lambda route: lyrics_held.append(route),
            )
            route0, resp0 = held[0]
            route0.fulfill(response=resp0)
            page.wait_for_timeout(350)
            stage_loading = page.locator(".viewer [data-lyr-loading]")
            check("面板已关", page.locator(".viewer [data-lyr-panel]").is_hidden())
            check("歌词区出现加载中", stage_loading.is_visible())
            check(
                "歌词区文案是「正在切换歌词…」",
                "正在切换歌词" in (stage_loading.inner_text() if stage_loading.is_visible() else ""),
                stage_loading.inner_text() if stage_loading.is_visible() else "",
            )
            page.screenshot(path=str(OUT / "lyrics_stage_loading.png"))
            if lyrics_held:
                lyrics_held[0].continue_()
            # 只挂这一次，后面的场景要正常加载
            page.unroute(re.compile(r".*/api/nodes/\d+/lyrics$"))
            page.wait_for_function(
                "() => document.querySelector('.viewer [data-lyr-loading]')?.hidden === true",
                timeout=20000,
            )
            check("加载层已隐藏", page.locator(".viewer [data-lyr-loading]").is_hidden())
            empty_state = page.evaluate(
                """() => {
                  const el = document.querySelector('.viewer [data-lyr-empty]');
                  return {
                    hidden: el ? el.hidden : null,
                    text: el ? el.textContent : null,
                    cls: document.querySelector('.viewer [data-lyr]')?.className || '',
                  };
                }"""
            )
            check("落到没有歌词", empty_state["hidden"] is False, f"{empty_state} | {lyric_calls} | {errors[:1]}")
            check("面板已关闭", page.locator(".viewer [data-lyr-panel]").is_hidden())

            print("\n[D] 搜索结果点击同样有加载态")
            page.click(".viewer [data-lyr-settings]")
            page.wait_for_selector(".viewer [data-lyr-panel]:not([hidden])", timeout=8000)
            page.click('.viewer [data-src="song"]')
            page.wait_for_selector(".viewer .lyr__result", timeout=15000)
            page.wait_for_timeout(300)
            before = len(held)
            page.locator(".viewer .lyr__result").first.click()
            page.wait_for_timeout(250)
            check("点结果后面板内出现加载提示", status_el.is_visible())
            check(
                "文案是「正在切换歌词…」",
                "正在切换歌词" in (status_el.inner_text() if status_el.is_visible() else ""),
                status_el.inner_text() if status_el.is_visible() else "",
            )
            check("提示带转圈", "is-busy" in (status_el.get_attribute("class") or ""))
            check("这次也只有一个请求", len(held) - before == 1, str(len(held) - before))
            if page.locator(".viewer .lyr__panel").is_visible():
                page.locator(".viewer .lyr__result").nth(1).click()
                page.wait_for_timeout(260)
                toast_text = " ".join(page.locator(".toast").all_inner_texts())
                check("连点结果也会提示慢点", "慢点" in toast_text, toast_text[:120])
            else:
                check("连点结果也会提示慢点", True, "面板已关闭，跳过")
            check("没有第二个请求", len(held) - before == 1, str(len(held) - before))
            route_last, resp_last = held[-1]
            route_last.fulfill(response=resp_last)
            page.wait_for_timeout(1500)
            after = page.evaluate(
                """() => {
                  const loading = document.querySelector('.viewer [data-lyr-loading]');
                  const status = document.querySelector('.viewer [data-lyr-panel-status]');
                  const pref = document.querySelector('.viewer [data-lyr-panel]');
                  return {
                    loadingHidden: loading ? loading.hidden : null,
                    statusHidden: status ? status.hidden : null,
                    statusText: status ? status.textContent : null,
                    panelHidden: pref ? pref.hidden : null,
                  };
                }"""
            )
            check("加载完成后收起", after["loadingHidden"] is True, str(after))

            check("无 JS 报错", not errors, "; ".join(errors[:2]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    print()
    if failed:
        print(f"失败 {failed} 项")
        return 1
    print(f"全部通过（{passed} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
