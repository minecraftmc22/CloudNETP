# -*- coding: utf-8 -*-
"""表情悬停预览回归：纯预览、固定弹在表情上方、点表情直接发送。

背景（0.6.17）：旧预览浮层自带「发送 / 删除」按钮栏，且定位在屏幕放不下时
会兜底弹到表情下方、图片加载完浮层变高还会往下压住表情格子 —— 点击落在
预览上，表情发不出去。现在：浮层只放一张大图，永远在表情正上方，
点击表情本身就是发送（面板顺带关掉）。

    ./.venv/Scripts/python.exe scripts/chat_sticker_preview_ui.py
"""
from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

PASS = FAIL = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
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
        time.sleep(0.5)
    print(log_path.read_text(encoding="utf-8", errors="replace")[-1200:])
    return False


def png_bytes(w: int, h: int, color) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-stkprev-")).resolve()
    db_path = tmp / "ui.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["STORAGE_DIR"] = str(tmp / "storage")
    os.environ["DB_FALLBACK_ENABLED"] = "0"
    os.environ["DB_PROBE_INTERVAL"] = "0"
    os.environ["DB_MIRROR_INTERVAL"] = "0"

    sys.path.insert(0, str(ROOT))
    from app.config import settings
    from app.db import SessionLocal, engine, ensure_schema
    from app.models import Friendship, User, utcnow
    from app.security import create_token, hash_password

    ensure_schema(engine)
    with SessionLocal() as db:
        rows = {}
        for name, display in (("sp_a", "小樱"), ("sp_b", "阿蓝")):
            row = User(
                username=name,
                display_name=display,
                password_hash=hash_password("Passw0rd!2024"),
                email=f"{name}@example.com",
                email_verified=True,
                role=0,
                quota_bytes=4 * 1024**3,
                token_version=1,
                created_at=utcnow(),
            )
            db.add(row)
            rows[name] = row
        db.commit()
        db.add(Friendship(requester_id=rows["sp_a"].id, addressee_id=rows["sp_b"].id,
                          status="accepted", message="", created_at=utcnow(), updated_at=utcnow()))
        db.commit()
        ids = {k: v.id for k, v in rows.items()}
        tokens = {k: create_token(v.id, v.token_version) for k, v in rows.items()}

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
            "STORAGE_DIR": str(tmp / "storage"),
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "RELOAD": "false",
        }
    )
    log_path = tmp / "server.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    import json

    def call(path: str, token: str, method: str = "GET", body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(base + path, method=method, data=data)
        req.add_header("Content-Type", "image/png" if raw is not None else "application/json")
        req.add_header("Cookie", f"{settings.COOKIE_NAME}={token}")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}")

    from playwright.sync_api import sync_playwright

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来")
        code, data = call("/api/chat/conversations/direct", tokens["sp_a"], "POST",
                          {"userId": ids["sp_b"]})
        assert code == 201, data
        direct_id = data["conversation"]["id"]
        # 存两张表情（一张大图：预览浮层变高后才看得出生不生效）
        for name, size, color in (("大太阳", (300, 300), (240, 180, 60)),
                                  ("小星星", (96, 96), (90, 150, 230))):
            code, _ = call(
                f"/api/chat/stickers?name={urllib.request.quote(name)}",
                tokens["sp_a"], "POST", raw=png_bytes(*size, color),
            )
            assert code in (200, 201), (name, code)

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            ctx = browser.new_context(viewport={"width": 1280, "height": 800})
            ctx.add_cookies(
                [{"name": settings.COOKIE_NAME, "value": tokens["sp_a"], "url": base}]
            )
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"{base}/#/chat/{direct_id}", wait_until="domcontentloaded")
            page.wait_for_selector('[data-act="open-stickers"]', timeout=20000)
            page.wait_for_timeout(900)
            page.click('[data-act="open-stickers"]')
            page.wait_for_selector('.sticker-item__btn', timeout=5000)
            page.wait_for_timeout(400)

            first = page.locator(".sticker-item").first

            def hover_and_measure():
                first.hover()
                page.wait_for_selector(".sticker-preview", timeout=5000)
                # 等图片加载完（浮层会变高，load 后有重新定位）
                page.wait_for_function(
                    """() => {
                      const img = document.querySelector('.sticker-preview img');
                      return img && img.complete && img.naturalWidth > 0;
                    }""",
                    timeout=5000,
                )
                page.wait_for_timeout(250)
                return page.evaluate(
                    """() => {
                      const pv = document.querySelector('.sticker-preview').getBoundingClientRect();
                      const it = document.querySelector('.sticker-item').getBoundingClientRect();
                      return {
                        pvTop: Math.round(pv.top), pvBottom: Math.round(pv.bottom),
                        pvHeight: Math.round(pv.height),
                        itemTop: Math.round(it.top),
                        hasBar: Boolean(document.querySelector('.sticker-preview__bar, [data-sp]')),
                      };
                    }"""
                )

            m1 = hover_and_measure()
            print("  预览(第一格):", m1)
            check("预览在表情上方", m1["pvBottom"] <= m1["itemTop"] + 2,
                  f"预览底 {m1['pvBottom']} vs 表情顶 {m1['itemTop']}")
            check("浮层里没有发送/删除按钮（纯预览）", not m1["hasBar"])
            check("浮层没有超出屏幕顶", m1["pvTop"] >= 8, f"top={m1['pvTop']}")
            page.screenshot(path=str(OUT / "chat_sticker_hover.png"))

            # 第二格（小图）悬停：浮层也不落到下方
            page.locator(".sticker-item").nth(1).hover()
            page.wait_for_timeout(500)
            m2 = page.evaluate(
                """() => {
                  const pv = document.querySelector('.sticker-preview').getBoundingClientRect();
                  const items = document.querySelectorAll('.sticker-item');
                  const it = items[1].getBoundingClientRect();
                  return { pvBottom: Math.round(pv.bottom), itemTop: Math.round(it.top) };
                }"""
            )
            check("换一格悬停预览仍在表情上方", m2["pvBottom"] <= m2["itemTop"] + 2,
                  f"{m2['pvBottom']} vs {m2['itemTop']}")

            # 悬停预览挂着时，直接点击表情 → 立即发送（面板关掉，消息流出现表情）
            first.hover()
            page.wait_for_selector(".sticker-preview", timeout=5000)
            page.click('.sticker-item__btn >> nth=0')
            page.wait_for_selector(".sticker-panel", state="attached", timeout=5000)
            try:
                page.wait_for_selector(".msg__sticker", timeout=6000)
                sent = True
            except Exception:
                sent = page.evaluate(
                    """() => Boolean(document.querySelector('.msg__sticker img'))"""
                )
            check("点击表情直接发送（消息流出现表情）", sent)
            closed = page.evaluate(
                """() => document.querySelector('[data-sticker-panel]')?.hidden !== false"""
            )
            check("发送后面板自动收起", closed)

            page.screenshot(path=str(OUT / "chat_sticker_preview.png"))
            check("全程没有 JS 报错", not errors, str(errors[:1]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_file.close()

    print(f"\n通过 {PASS} / {PASS + FAIL}，截图在 {OUT}")
    return 1 if FAIL else 0


sys.exit(main())
