# -*- coding: utf-8 -*-
"""网易云扫码登录的后台 UI 端到端（假网易云，一个进程里跑完）。

  1. 后台「系统设置」出现「网易云歌词」卡片，初始为未连接
  2. 点「扫码登录」→ 二维码图片能加载（服务端画的 SVG）
  3. 轮询到 803 → 卡片变「已连接」，显示昵称 / UID
  4. 页面上任何地方都不出现 Cookie 原文（MUSIC_U / token）
  5. 点「退出登录」→ 确认 → 回到未连接
  6. 全程无 JS 报错

跑法：
    ./.venv/Scripts/python.exe scripts/netease_login_ui.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-ng-ui-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'smoke.db').as_posix()}"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ["SECRET_KEY"] = "smoke-secret-key-for-netease-ui"
os.environ["DB_FALLBACK_ENABLED"] = "0"
os.environ["DB_PROBE_INTERVAL"] = "0"
os.environ["ALLOW_REGISTER"] = "1"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import netease_auth as ng  # noqa: E402

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


SECRET_TOKEN = "MUSIC_U=ui-token-abcdef; Path=/; Domain=.music.163.com; HttpOnly"


class FakeNetease(BaseHTTPRequestHandler):
    polls = 0

    def log_message(self, *_args):
        pass

    def _send(self, payload: dict, cookies: list[str] | None = None):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for raw in cookies or []:
            self.send_header("Set-Cookie", raw)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/login/qrcode/unikey":
            return self._send({"code": 200, "unikey": "ui-uni"})
        if path == "/api/login/qrcode/client/login":
            FakeNetease.polls += 1
            if FakeNetease.polls <= 1:
                return self._send({"code": 801})
            return self._send({"code": 803, "nickname": "半仙"}, [SECRET_TOKEN, "__csrf=ui-csrf; Path=/"])
        if path == "/api/nuser/account/get":
            return self._send(
                {
                    "code": 200,
                    "profile": {
                        "userId": 42,
                        "nickname": "半仙",
                        "avatarUrl": "http://p1.music.126.net/ui.jpg",
                        "vipType": 11,
                    },
                }
            )
        if path == "/api/logout":
            return self._send({"code": 200})
        return self._send({"code": 404})


fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeNetease)
threading.Thread(target=fake.serve_forever, daemon=True).start()
ng.API_BASE = f"http://127.0.0.1:{fake.server_address[1]}"


def main() -> None:
    import uvicorn
    from sqlalchemy import select

    from app import db_failover
    from app.config import settings
    from app.main import app as fastapi_app
    from app.models import User
    from app.security import create_token, hash_password
    from app.user_service import ensure_settings

    port = 8871
    cfg = uvicorn.Config(
        fastapi_app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(cfg)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.2)
    if not server.started:
        raise SystemExit("服务没起来")

    # 管理员账号（库已由 lifespan 建好）
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

    errors: list[str] = []
    responses: list[str] = []
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1360, "height": 1000})
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
            # 抓接口响应体，确认没有任何一个回包泄露 Cookie
            page.on(
                "response",
                lambda r: responses.append(r.text())
                if "/api/admin/netease" in r.url and r.ok
                else None,
            )

            page.goto(f"{base}/#/admin", wait_until="domcontentloaded")
            page.wait_for_selector("[data-tab='settings']", timeout=20000)
            page.click("[data-tab='settings']")
            page.wait_for_selector("[data-card='netease'] [data-ng-act='login']", timeout=20000)

            card = page.locator("[data-card='netease']")
            check("卡片出现", card.count() == 1)
            check("初始未连接", "未连接" in card.inner_text(), card.inner_text().split("\n")[0])

            page.click("[data-card='netease'] [data-ng-act='login']")
            page.wait_for_selector("[data-card='netease'] .ng-qr:not([hidden])", timeout=15000)
            img = page.locator("[data-ng-qr-img]")
            img.wait_for(state="visible", timeout=10000)
            page.wait_for_function(
                "() => { const i = document.querySelector('[data-ng-qr-img]'); return i && i.complete && i.naturalWidth > 60; }",
                timeout=15000,
            )
            natural = page.evaluate("() => document.querySelector('[data-ng-qr-img]').naturalWidth")
            check("二维码图片加载成功", natural > 60, f"{natural}px")
            page.screenshot(path=str(_TMP / "netease_qr.png"))
            src = page.get_attribute("[data-ng-qr-img]", "src")
            check("图片走本站接口", src.startswith("/api/admin/netease/qr/") and src.endswith(".svg"), src)
            check("二维码里没有 Cookie", "MUSIC_U" not in (src or ""), src)

            # 假上游第 2 次轮询就返回 803
            page.wait_for_function(
                "() => document.querySelector(\"[data-card='netease']\")?.innerText.includes('已连接')",
                timeout=25000,
            )
            card_text = page.inner_text("[data-card='netease']")
            check("变成已连接", "已连接" in card_text, card_text.splitlines()[1])
            flat = " / ".join(x.strip() for x in card_text.splitlines() if x.strip())
            check("显示昵称", "半仙" in card_text, flat[:60])
            check("显示 UID", "UID 42" in card_text, flat[:80])
            check("出现退出登录", page.locator("[data-ng-act='logout']").count() == 1)
            gap = page.evaluate(
                """() => {
                    const c = document.querySelector("[data-card='netease']");
                    const h = c.querySelector('.admin-hint');
                    return Math.round(c.getBoundingClientRect().bottom - h.getBoundingClientRect().bottom);
                }"""
            )
            # 提示文字的 padding 在自身盒子里，量盒子高度没意义：直接看计算值
            pad = page.evaluate(
                """() => {
                    const c = document.querySelector("[data-card='netease']");
                    const h = c.querySelector('.admin-hint');
                    return {
                        pad: parseFloat(getComputedStyle(h).paddingBottom) || 0,
                        inside: c.getBoundingClientRect().bottom - h.getBoundingClientRect().bottom,
                    };
                }"""
            )
            check(
                "提示与卡片底部有留白",
                pad["pad"] >= 10 and pad["inside"] >= -1,
                f"padding {pad['pad']}px / gap {round(pad['inside'])}px",
            )
            check("二维码区域收起", page.locator("[data-ng-qr]").is_hidden())
            page.locator("[data-card='netease']").screenshot(path=str(_TMP / "netease_connected.png"))

            # 审计日志里要留下记录
            logs = page.request.get(f"{base}/api/admin/logs?action=admin_netease_login")
            log_data = logs.json() if logs.ok else {}
            check("记了登录审计", (log_data.get("total") or 0) >= 1, log_data.get("total"))

            body = page.inner_text("body")
            for bad in ("MUSIC_U", "ui-token-abcdef", "__csrf"):
                check(f"页面不出现 {bad}", bad not in body, "")

            joined = "\n".join(responses)
            check("接口回包不含 Cookie 原文", "ui-token-abcdef" not in joined, f"{len(responses)} 个响应")
            check("接口回包不含 Cookie 名", "MUSIC_U" not in joined, f"{len(responses)} 个响应")

            # 退出登录
            page.click("[data-ng-act='logout']")
            page.wait_for_selector(".dialog", timeout=8000)
            page.click(".dialog [data-act='ok']")
            page.wait_for_function(
                "() => document.querySelector(\"[data-card='netease']\")?.innerText.includes('未连接')",
                timeout=15000,
            )
            check("退出后回到未连接", "未连接" in page.inner_text("[data-card='netease']"))

            # 英文界面：卡片文案要全部翻过去
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
            page.wait_for_selector("[data-card='netease']", timeout=20000)
            en_text = page.inner_text("[data-card='netease']")
            check("英文卡片已翻译", "Not connected" in en_text, en_text.splitlines()[:2])
            import re as _re

            leftover = _re.findall(r"[\u4e00-\u9fa5]+", en_text)
            check("英文卡片没有残留中文", not leftover, leftover[:3])
            page.locator("[data-card='netease']").screenshot(path=str(_TMP / "netease_en.png"))
            logs2 = page.request.get(f"{base}/api/admin/logs?action=admin_netease_logout")
            log_data2 = logs2.json() if logs2.ok else {}
            check("记了退出审计", (log_data2.get("total") or 0) >= 1, log_data2.get("total"))
            page.screenshot(path=str(_TMP / "netease_logged_out.png"))
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        fake.shutdown()

    check("无 JS 报错", not errors, "; ".join(errors[:2]))
    print(f"\n截图目录：{_TMP}")
    print(f"通过 {PASSED}/{PASSED + len(FAILED)}")
    if FAILED:
        print("失败项：" + "、".join(FAILED))
    raise SystemExit(0 if not FAILED else 1)


if __name__ == "__main__":
    main()
