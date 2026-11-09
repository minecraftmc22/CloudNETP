# -*- coding: utf-8 -*-
"""管理后台「歌词来源」卡片的端到端（假歌词库）。

  1. 没在 .env 写 LYRICS_PROVIDERS、但配了密钥时，卡片默认两个来源都启用
  2. 卡片能测试连接（真的打到配置的地址，带 Bearer）
  3. 关掉网易云、填密钥与 Cookie、保存 → 立即生效，播放器接口只剩歌词库
  4. 密钥/Cookie 在库里是密文，页面上只出现掩码
  5. 再打开网易云保存 → 两个来源都出现在播放器接口里
  6. 刷新页面配置还在；全程无 JS 报错

跑法：
    ./.venv/Scripts/python.exe scripts/lyrics_admin_ui.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-lyr-admin-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'smoke.db').as_posix()}"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ["SECRET_KEY"] = "smoke-secret-key-for-lyrics-admin"
os.environ["DB_FALLBACK_ENABLED"] = "0"
os.environ["DB_PROBE_INTERVAL"] = "0"
os.environ["ALLOW_REGISTER"] = "1"
# 关键：只配密钥、不写来源，模拟「用户在 .env 里填了歌词库密钥」的现状
os.environ["LYRICS_API_KEY"] = "env-key-abcd"
os.environ["LYRICS_API_BASE"] = "http://127.0.0.1:1/api/v1"  # 下面会改成假服务
os.environ.pop("LYRICS_PROVIDERS", None)
os.environ.pop("LYRICS_PROVIDER", None)
os.environ.pop("LYRICS_API_COOKIE", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  OK   {name} {detail}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


class FakeLib(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        FakeLib.calls.append({"path": self.path, "headers": dict(self.headers)})
        body = json.dumps(
            {
                "ok": True,
                "data": {
                    "items": [
                        {"id": 9, "title": "晴天", "artist": "周杰伦", "album": "叶惠美"},
                        {"id": 10, "title": "七里香", "artist": "周杰伦", "album": "七里香"},
                    ]
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeLib)
threading.Thread(target=fake.serve_forever, daemon=True).start()
FAKE_BASE = f"http://127.0.0.1:{fake.server_address[1]}/api/v1"
os.environ["LYRICS_API_BASE"] = FAKE_BASE


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> None:
    import uvicorn

    from app import db_failover, lyrics_config
    from app.config import settings
    from app.main import app as fastapi_app
    from app.models import User
    from app.security import create_token, hash_password
    from app.user_service import ensure_settings

    port = free_port()
    cfg = uvicorn.Config(
        fastapi_app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if server.started:
            break
        time.sleep(0.2)
    if not server.started:
        raise SystemExit("服务没起来")

    Factory = db_failover.current_sessionmaker()
    with Factory() as db:
        user = User(
            username="sakura",
            password_hash=hash_password("Cloud@2026"),
            display_name="樱花",
            role=1,
            quota_bytes=1024**3,
            token_version=1,
            created_at=datetime.utcnow(),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        ensure_settings(db, user.id)
        db.commit()
        cookie = create_token(user.id, user.token_version)
        admin_id = user.id

    errors: list[str] = []
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1360, "height": 1100})
            ctx.add_cookies(
                [
                    {
                        "name": settings.COOKIE_NAME,
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

            page.goto(f"{base}/#/admin", wait_until="domcontentloaded")
            page.wait_for_selector("[data-tab='settings']", timeout=20000)
            page.click("[data-tab='settings']")
            page.wait_for_selector("[data-card='lyrics'] [data-lyr-save]", timeout=20000)

            card = page.locator("[data-card='lyrics']")
            check("卡片出现", card.count() == 1, "data-card=lyrics")
            text = card.inner_text()
            check("默认两个来源都启用", text.count("网易云") >= 1 and "歌词库" in text, text[:80])
            on = page.evaluate(
                "() => [...document.querySelectorAll(\"[data-card='lyrics'] [data-lyr-prov]\")]"
                ".filter(b => b.classList.contains('is-active')).map(b => b.dataset.lyrProv)"
            )
            check("两个胶囊都是选中态", on == ["netease", "lyriclib"], on)
            check("提示说明来源是自动", "未显式配置" in text, text[:120])

            # 地址框：后台没保存过时留空，用 .env 的值做 placeholder
            ph = page.get_attribute("[data-lyr-base]", "placeholder")
            check("地址占位是 .env 的值", ph == FAKE_BASE, ph)

            # --- 测试连接：用 .env 的密钥打假歌词库 ---
            FakeLib.calls.clear()
            page.click("[data-card='lyrics'] [data-lyr-test]")
            page.wait_for_function(
                "() => (document.querySelector('[data-lyr-status]')?.textContent || '').includes('连接正常')",
                timeout=20000,
            )
            status = page.inner_text("[data-lyr-status]")
            check("测试连接成功", "连接正常" in status and "2 条结果" in status, status)
            auth = FakeLib.calls[0]["headers"].get("Authorization")
            check("测试用 .env 的密钥", auth == "Bearer env-key-abcd", auth)

            # --- 关掉网易云、填后台密钥与 Cookie、保存 ---
            page.click("[data-lyr-prov='netease']")
            page.fill("[data-lyr-base]", FAKE_BASE)
            page.fill("[data-lyr-key]", "admin-key-7777")
            page.fill("[data-lyr-cookie]", "site-session=lib-cookie")
            page.click("[data-card='lyrics'] [data-lyr-save]")
            page.wait_for_function(
                "() => (document.querySelector('[data-card=\"lyrics\"]')?.innerText || '').includes('由后台保存')",
                timeout=20000,
            )
            text2 = page.inner_text("[data-card='lyrics']")
            key_ph = page.get_attribute("[data-lyr-key]", "placeholder")
            check("保存后提示已保存的掩码", "…7777" in (key_ph or ""), key_ph)
            check("出现清除密钥勾选", page.locator("[data-lyr-clear-key]").count() == 1)
            check("保存后来源标记为后台", "由后台保存" in text2, text2[:160])
            on2 = page.evaluate(
                "() => [...document.querySelectorAll(\"[data-card='lyrics'] [data-lyr-prov]\")]"
                ".filter(b => b.classList.contains('is-active')).map(b => b.dataset.lyrProv)"
            )
            check("只剩歌词库选中", on2 == ["lyriclib"], on2)

            # 播放器接口：分类随之变化
            prov = page.request.get(f"{base}/api/lyrics/providers")
            data = prov.json()
            ids = [p["id"] for p in data.get("items", [])]
            check("播放器只剩歌词库", ids == ["lyriclib"], ids)
            check("歌词库标记为可用", data["items"][0]["ready"] is True, data["items"])

            # 再测一次：应该用后台刚保存的密钥
            FakeLib.calls.clear()
            page.click("[data-card='lyrics'] [data-lyr-test]")
            page.wait_for_function(
                "() => (document.querySelector('[data-lyr-status]')?.textContent || '').includes('连接正常')",
                timeout=20000,
            )
            auth2 = FakeLib.calls[0]["headers"].get("Authorization")
            check("测试改用后台密钥", auth2 == "Bearer admin-key-7777", auth2)
            check("Cookie 也透传", "site-session=lib-cookie" in FakeLib.calls[0]["headers"].get("Cookie", ""))

            # 库里是密文
            from app import settings_store

            with Factory() as db:
                raw_key = settings_store.get_value(db, lyrics_config.STORE_API_KEY)
                raw_cookie = settings_store.get_value(db, lyrics_config.STORE_API_COOKIE)
            check("密钥加密入库", raw_key.startswith("enc:") and "admin-key" not in raw_key, raw_key[:8])
            check("Cookie 加密入库", raw_cookie.startswith("enc:") and "lib-cookie" not in raw_cookie, raw_cookie[:8])
            check("页面不出现密钥原文", "admin-key-7777" not in page.content())
            check("页面不出现 Cookie 原文", "lib-cookie" not in page.content())

            # --- 把网易云加回来 ---
            page.click("[data-lyr-prov='netease']")
            page.click("[data-card='lyrics'] [data-lyr-save]")
            page.wait_for_timeout(1500)
            prov2 = page.request.get(f"{base}/api/lyrics/providers").json()
            ids2 = [p["id"] for p in prov2.get("items", [])]
            # 刚重新打开的来源排在最后（顺序即优先级），两个都在就算回来
            check("两个来源都回来了", set(ids2) == {"netease", "lyriclib"}, ids2)

            # --- 刷新后仍在 ---
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("[data-tab='settings']", timeout=20000)
            page.click("[data-tab='settings']")
            page.wait_for_selector("[data-card='lyrics'] [data-lyr-save]", timeout=20000)
            text3 = page.inner_text("[data-card='lyrics']")
            key_ph3 = page.get_attribute("[data-lyr-key]", "placeholder")
            check("刷新后掩码还在", "…7777" in (key_ph3 or ""), key_ph3)
            check("刷新后仍是后台配置", "由后台保存" in text3, text3[:160])
            page.locator("[data-card='lyrics']").screenshot(path=str(_TMP / "lyrics_card.png"))

            # --- 英文界面：卡片不能残留中文 ---
            page.evaluate(
                """async () => {
                    await fetch('/api/me/settings', {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ language: 'en-US' }),
                    });
                }"""
            )
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("[data-tab='settings']", timeout=20000)
            page.click("[data-tab='settings']")
            page.wait_for_selector("[data-card='lyrics'] [data-lyr-save]", timeout=20000)
            en_text = page.inner_text("[data-card='lyrics']")
            import re as _re

            leftover = _re.findall(r"[\u4e00-\u9fa5]+", en_text)
            check("英文卡片没有残留中文", not leftover, leftover[:5])
            check("英文卡片有标题", "Lyrics source" in en_text, en_text.splitlines()[0])

            # --- 手机宽度：卡片不溢出，字段单列 ---
            page.set_viewport_size({"width": 390, "height": 900})
            page.wait_for_timeout(600)
            box = page.evaluate(
                """() => {
                    const c = document.querySelector("[data-card='lyrics']");
                    const r = c.getBoundingClientRect();
                    return { right: Math.round(r.right), vw: window.innerWidth };
                }"""
            )
            check("手机宽度卡片不溢出", box["right"] <= box["vw"] + 1, box)
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(300)
            page.locator("[data-card='lyrics']").screenshot(path=str(_TMP / "lyrics_card_mobile.png"))

            check("没有 JS 报错", not errors, "; ".join(errors[:3]))
            browser.close()
    finally:
        fake.shutdown()

    print(f"\n{'=' * 56}")
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for name in FAILED:
        print("  FAILED:", name)
    print("截图:", _TMP / "lyrics_card.png")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
