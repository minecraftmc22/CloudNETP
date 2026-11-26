# -*- coding: utf-8 -*-
"""群聊 @ 成员选择弹层回归。

背景 bug：.chat-mention__avatar 只写了 display:inline-flex，没定宽高、
也没约束里面的 img —— 头像图按原图尺寸渲染，弹层整行被撑成一条大图
（只有「有头像图」的成员会暴露，首字头像看不出来）。

覆盖：
- 头像容器固定 26×26 圆形，img 被裁进容器（object-fit:cover）
- 列表行高正常（不会被任何成员撑爆），弹层不超 max-height
- 无头像成员显示首字，同样在 26×26 容器里居中

    ./.venv/Scripts/python.exe scripts/chat_mention_ui.py
"""
from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import tempfile
import time
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
    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-mention-")).resolve()
    db_path = tmp / "ui.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["STORAGE_DIR"] = str(tmp / "storage")
    os.environ["DB_FALLBACK_ENABLED"] = "0"
    os.environ["DB_PROBE_INTERVAL"] = "0"
    os.environ["DB_MIRROR_INTERVAL"] = "0"

    sys.path.insert(0, str(ROOT))
    from app.config import settings
    from app import profile_service as ps
    from app.db import SessionLocal, engine, ensure_schema
    from app.models import Friendship, User, utcnow
    from app.security import create_token, hash_password

    ensure_schema(engine)
    with SessionLocal() as db:
        rows = {}
        for name, display, role in (
            ("mn_owner", "群主小樱", 0),
            ("mn_photo", "牛仔阿豆", 0),
            ("mn_plain", "可可", 0),
        ):
            row = User(
                username=name,
                display_name=display,
                password_hash=hash_password("Passw0rd!2024"),
                email=f"{name}@example.com",
                email_verified=True,
                role=role,
                quota_bytes=4 * 1024**3,
                token_version=1,
                created_at=utcnow(),
            )
            db.add(row)
            rows[name] = row
        db.commit()
        # 给「牛仔阿豆」一张头像（save_avatar 会裁成 256×256）
        rows["mn_photo"].avatar = ps.save_avatar(
            rows["mn_photo"].id, png_bytes(256, 256, (150, 90, 40))
        )
        # 建群要求互为好友：群主与其余两人都拉一个 accepted 好友关系
        db.add_all(
            [
                Friendship(requester_id=rows["mn_owner"].id, addressee_id=rows["mn_photo"].id,
                           status="accepted", message="", created_at=utcnow(), updated_at=utcnow()),
                Friendship(requester_id=rows["mn_owner"].id, addressee_id=rows["mn_plain"].id,
                           status="accepted", message="", created_at=utcnow(), updated_at=utcnow()),
            ]
        )
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
    import urllib.error

    def call(path: str, token: str, method: str = "GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, method=method, data=data)
        req.add_header("Content-Type", "application/json")
        req.add_header("Cookie", f"{settings.COOKIE_NAME}={token}")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as err:
            return err.code, {}

    from playwright.sync_api import sync_playwright

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来")
        code, data = call(
            "/api/chat/conversations/group",
            tokens["mn_owner"],
            "POST",
            {"title": "mention 回归群", "memberIds": [ids["mn_photo"], ids["mn_plain"]]},
        )
        assert code == 201, data
        group_id = data["conversation"]["id"]

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            ctx = browser.new_context(viewport={"width": 1280, "height": 800})
            ctx.add_cookies(
                [{"name": settings.COOKIE_NAME, "value": tokens["mn_owner"], "url": base}]
            )
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"{base}/#/chat/{group_id}", wait_until="domcontentloaded")
            page.wait_for_selector(".chat-mention", state="attached", timeout=20000)
            page.wait_for_selector('[data-act="mention"]', state="visible", timeout=20000)
            page.wait_for_timeout(1200)

            # 点 @ 按钮打开弹层
            page.click('[data-act="mention"]')
            page.wait_for_selector('.chat-mention:not([hidden])', timeout=5000)
            page.wait_for_timeout(400)

            stats = page.evaluate(
                """() => {
                  const box = document.querySelector('.chat-mention');
                  const out = { box: box.getBoundingClientRect().height, items: [] };
                  for (const item of box.querySelectorAll('.chat-mention__item')) {
                    const name = item.querySelector('.chat-mention__name')?.textContent.trim();
                    const ava = item.querySelector('.chat-mention__avatar');
                    if (!ava) { out.items.push({name, noAvatar: true, itemH: Math.round(item.getBoundingClientRect().height)}); continue; }
                    const img = ava.querySelector('img');
                    const ir = ava.getBoundingClientRect();
                    out.items.push({
                      name,
                      hasImg: Boolean(img),
                      imgLoaded: img ? img.complete && img.naturalWidth > 0 : false,
                      avaW: Math.round(ir.width), avaH: Math.round(ir.height),
                      imgW: img ? Math.round(img.getBoundingClientRect().width) : 0,
                      itemH: Math.round(item.getBoundingClientRect().height),
                    });
                  }
                  return out;
                }"""
            )
            print("  弹层高度:", stats["box"])
            for item in stats["items"]:
                print("  ", item)

            check("弹层不超过 max-height", stats["box"] <= 220 + 8, f"{stats['box']}px")
            for item in stats["items"]:
                tag = item["name"]
                if item.get("noAvatar"):
                    check(f"[{tag}] @所有人 行高正常（<60px）", item["itemH"] < 60, f"{item['itemH']}px")
                    continue
                check(f"[{tag}] 头像容器 26×26", item["avaW"] == 26 and item["avaH"] == 26,
                      f"{item['avaW']}×{item['avaH']}")
                check(f"[{tag}] 行高正常（<60px）", item["itemH"] < 60, f"{item['itemH']}px")
                if item["hasImg"]:
                    check(f"[{tag}] 头像图加载成功", item["imgLoaded"])
                    check(f"[{tag}] 图片被裁进 26px 容器", item["imgW"] <= 27, f"img 宽 {item['imgW']}")

            page.screenshot(path=str(OUT / "chat_mention_picker.png"),
                            clip={"x": 440, "y": 320, "width": 840, "height": 460})
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
