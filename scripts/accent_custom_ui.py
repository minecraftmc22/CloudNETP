# -*- coding: utf-8 -*-
"""设置 → 主题色：自定义色值 + AI 取色的 UI 冒烟。

用库副本起独立实例，Playwright 驱动本机 Chrome：

  A 单色手输 #rrggbb → 落库、取色器与色板跟着变；乱填会提示并回滚
  B 渐变的每一档也能手输色值 → 落库、色块跟着变
  C 没有背景图时点 AI 取色 → 提示「还没设置背景图」，且不发请求
  D 背景图有了但 AI 没配置 → 提示去「设置 → 看板娘 · 对话设置」填接口
  E 配置可用 → 显示「AI 正在挑颜色…」，拿到颜色后按渐变落库
  F 375px 无横向溢出

    ./.venv/Scripts/python.exe scripts/accent_custom_ui.py
"""

from __future__ import annotations

import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
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


def snapshot_db(dst: Path) -> None:
    src_path = ROOT / "storage" / "db" / "fallback.db"
    if not src_path.is_file():
        src_path = ROOT / "dev.db"
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        src.close()
        out.close()


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
    print(log_path.read_text(encoding="utf-8", errors="replace")[-1500:])
    return False


def bg_png() -> bytes:
    from PIL import Image

    picture = Image.new("RGB", (640, 400), (226, 88, 148))
    from PIL import ImageDraw

    ImageDraw.Draw(picture).rectangle([320, 0, 640, 400], fill=(72, 140, 232))
    buffer = io.BytesIO()
    picture.save(buffer, "PNG")
    return buffer.getvalue()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-accent-custom-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)

    # 先把库指向副本，再用 ORM 建/改用户——user_settings 有一堆 NOT NULL 列，
    # 手写 INSERT 很容易漏，交给模型定义最省事
    os.environ["DATABASE_URL"] = f"sqlite:///{db_copy.as_posix()}"
    os.environ["STORAGE_DIR"] = str(tmp / "storage")

    from app.config import settings
    from app.db import SessionLocal, engine, ensure_schema
    from app.models import User
    from app.security import create_token
    from app.user_service import ensure_settings

    ensure_schema(engine)
    with SessionLocal() as db:
        user = db.query(User).order_by(User.id).first()
        if not user:
            user = User(
                username="accentui",
                password_hash="x",
                display_name="AccentUI",
                quota_bytes=10 * 1024 ** 3,
                used_bytes=0,
                role=0,
            )
            db.add(user)
            db.commit()
        user_id, token_version = int(user.id), int(user.token_version)
        row = ensure_settings(db, user_id)
        row.show_mascot = False
        row.background_image = None       # A/B 段先测手输色值
        row.accent_color = "#c084fc"
        row.accent_style = None
        db.commit()
    cookie = create_token(user_id, token_version)

    # [E] 段要真读服务端配置（不 mock /api/ai/config），这里先把接口填进库副本：
    # 之前 mock 回扁平对象，把「配置藏在 config 里层、读错层级」的 bug 掩盖了
    from app import ai_service as ai

    with SessionLocal() as db:
        ai.save_config(
            db,
            user_id,
            {"baseUrl": "http://127.0.0.1:9/v1", "key": "sk-test", "model": "test-model"},
        )
        db.commit()

    def saved_accent():
        conn = sqlite3.connect(str(db_copy))
        try:
            row = conn.execute(
                "select accent_style, accent_color from user_settings where user_id = ?", (user_id,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {}, ""
        return json.loads(row[0] or "{}"), (row[1] or "").lower()

    def set_background(name: str | None) -> None:
        conn = sqlite3.connect(str(db_copy))
        conn.execute("update user_settings set background_image = ? where user_id = ?", (name, user_id))
        conn.commit()
        conn.close()

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db_copy.as_posix()}",
            "STORAGE_DIR": str(tmp / "storage"),
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "RELOAD": "false",
            "DB_PROBE_INTERVAL": "0",
            "DB_MIRROR_INTERVAL": "0",
            "DB_FALLBACK_ENABLED": "1",
            "SECRET_KEY": settings.SECRET_KEY,
        }
    )
    log_path = tmp / "server.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "main.py"], cwd=str(ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT
    )

    from playwright.sync_api import sync_playwright

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来，看日志：" + str(log_path))

        png = bg_png()
        ai_calls: list[dict] = []
        held: list = []
        ai_config = {"ready": False, "aiEnabled": True, "source": "none"}

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)

            ctx = browser.new_context(viewport={"width": 1440, "height": 1200}, device_scale_factor=2)
            ctx.add_cookies([{"name": "cloudnetp_session", "value": cookie, "url": base}])

            def wire(page):
                page.route("**/api/background/e2e-bg.png", lambda route: route.fulfill(
                    status=200, content_type="image/png", body=png
                ))
                # 照后端真实结构回：配置在 config 里层（外层还有 isAdmin / systemPrompt），
                # 直接回扁平对象会把「读错层级」的 bug 掩盖掉
                page.route(
                    "**/api/ai/config",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(
                            {"config": ai_config, "isAdmin": False, "systemPrompt": ""}
                        ),
                    ),
                )

                def accent_route(route):
                    # 扣住不响应：测试先看「正在挑颜色」的中间态，再手动放行
                    ai_calls.append(json.loads(route.request.post_data or "{}"))
                    held.append(route)

                page.route("**/api/ai/accent", accent_route)
                return page

            page = wire(ctx.new_page())
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

            def open_settings():
                # 只改 hash 不会重新拉 /api/me，必须真刷新一次，否则 store 里还是旧的背景图
                page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector("[data-accent-editor]", state="attached", timeout=20000)
                page.wait_for_timeout(500)

            print("\n[A] 单色手输色值")
            open_settings()
            page.locator("[data-accent-mode-btn='solid']").click()
            page.wait_for_timeout(300)
            field = page.locator("[data-accent-hex]")
            check("单色有手输框", field.is_visible())
            field.fill("3b82f6")
            field.press("Enter")
            page.wait_for_timeout(800)
            style, primary = saved_accent()
            check("少写 # 也能认", style.get("mode") == "solid" and (style.get("colors") or [""])[0] == "#3b82f6", str(style))
            check("主色跟着走", primary == "#3b82f6", primary)
            check(
                "取色器同步",
                page.locator("[data-color-pick]").input_value().lower() == "#3b82f6",
                page.locator("[data-color-pick]").input_value(),
            )
            check(
                "--accent-user 生效",
                page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent-user').trim()").lower()
                == "#3b82f6",
            )

            field.fill("zzz")
            field.press("Enter")
            page.wait_for_timeout(500)
            toast = " ".join(page.locator(".toast").all_inner_texts())
            check("乱填会提示", "#rrggbb" in toast, toast[:80])
            check("提示后回滚到原值", field.input_value().lower() == "#3b82f6", field.input_value())
            check("乱填没有写库", saved_accent()[1] == "#3b82f6", str(saved_accent()[0]))
            page.screenshot(path=str(OUT / "accent_custom_solid.png"))
            page.locator("[data-accent-editor]").screenshot(path=str(OUT / "accent_editor_solid.png"))

            print("\n[B] 渐变色标手输色值")
            page.locator("[data-accent-mode-btn='gradient']").click()
            page.wait_for_timeout(500)
            stop_field = page.locator("[data-accent-stop-hex='0']")
            check("色标也有手输框", stop_field.count() == 1)
            stop_field.fill("#22c55e")
            stop_field.press("Enter")
            page.wait_for_timeout(800)
            style, primary = saved_accent()
            check("色标手输落库", (style.get("colors") or [""])[0] == "#22c55e", str(style.get("colors")))
            check("主色跟着第一个色标", primary == "#22c55e", primary)
            check(
                "色块跟着变",
                "#22c55e" in (
                    page.locator("[data-accent-stop='0']").evaluate(
                        "el => el.closest('.accent-stop').style.getPropertyValue('--stop')"
                    )
                    or ""
                ),
            )
            page.screenshot(path=str(OUT / "accent_custom_gradient.png"))
            page.locator("[data-accent-editor]").screenshot(path=str(OUT / "accent_editor_gradient.png"))

            # 只有两档时删除按钮是隐藏的（保住下限）；加到三档后要能看见、能删
            page.locator("[data-accent-add]").click()
            page.wait_for_timeout(500)
            del_btn = page.locator("[data-accent-stop-del]").first
            check("三档时删除按钮出现", del_btn.is_visible())
            check("删除按钮在色标行内", del_btn.bounding_box() is not None)
            page.locator("[data-accent-editor]").screenshot(path=str(OUT / "accent_editor_three.png"))
            del_btn.click()
            page.wait_for_timeout(500)
            check("删回两档", page.locator("[data-accent-stop]").count() == 2, str(page.locator("[data-accent-stop]").count()))

            print("\n[B2] 切回单色：色板与色值框跟着第一档")
            # 删的是第 1 档，所以现存的「第一档」要从库里读，别写死
            style, _ = saved_accent()
            first_left = (style.get("colors") or [""])[0]
            page.locator("[data-accent-mode-btn='solid']").click()
            page.wait_for_timeout(600)
            check(
                "色值框同步成现存第一档",
                page.locator("[data-accent-hex]").input_value().lower() == first_left,
                f"{page.locator('[data-accent-hex]').input_value()} vs {first_left}",
            )
            check("落库也改成这个颜色", saved_accent()[1] == first_left, str(saved_accent()[1]))
            check(
                "取色板入口有「取色板」提示",
                "取色板" in (page.locator("[data-swatch-pick]").get_attribute("title") or ""),
                page.locator("[data-swatch-pick]").get_attribute("title") or "",
            )
            page.locator("[data-accent-mode-btn='gradient']").click()
            page.wait_for_timeout(400)

            print("\n[C] 没有背景图时点 AI 取色")
            set_background(None)
            ai_calls.clear()
            open_settings()
            page.locator("[data-act='pick-ai']").click()
            page.wait_for_timeout(600)
            toast = " ".join(page.locator(".toast").all_inner_texts())
            check("提示先设背景图", "背景图" in toast, toast[:80])
            check("没发 AI 请求", not ai_calls, str(len(ai_calls)))

            print("\n[D] 有图但 AI 没配置")
            set_background("e2e-bg.png")
            ai_config = {"ready": False, "aiEnabled": True, "source": "none"}
            ai_calls.clear()
            open_settings()
            page.locator("[data-act='pick-ai']").click()
            page.wait_for_timeout(800)
            toast = " ".join(page.locator(".toast").all_inner_texts())
            check("提示去对话设置填接口", "对话设置" in toast, toast[:80])
            check("同样没发取色请求", not ai_calls, str(len(ai_calls)))

            print("\n[E] AI 取色成功（真读服务端配置）")
            ai_config = {"ready": True, "aiEnabled": True, "source": "user"}
            ai_calls.clear()
            # 撤掉 /api/ai/config 的假响应，改由服务端按库里的真实配置作答
            page.unroute("**/api/ai/config")
            open_settings()
            real_ready = page.evaluate(
                "fetch('/api/ai/config', {credentials:'same-origin'})"
                ".then(r => r.json()).then(d => d?.config?.ready === true)"
            )
            check("服务端配置确实可用（ready=true）", real_ready is True, str(real_ready))
            button = page.locator("[data-act='pick-ai']")
            held.clear()
            button.click()
            page.wait_for_timeout(250)
            check("点下去先显示在处理", "正在挑颜色" in button.inner_text(), button.inner_text())
            check("处理中按钮禁用", button.is_disabled())
            check("请求已经发出", len(held) == 1, str(len(held)))
            page.screenshot(path=str(OUT / "accent_ai_busy.png"))
            if held:
                held[0].fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"mode": "gradient", "colors": ["#7c3aed", "#ec4899", "#f59e0b"]}),
                )
            page.wait_for_timeout(1000)
            style, primary = saved_accent()
            check("按渐变落库", style.get("mode") == "gradient", str(style))
            check(
                "颜色就是模型给的",
                (style.get("colors") or []) == ["#7c3aed", "#ec4899", "#f59e0b"],
                str(style.get("colors")),
            )
            check("主色是第一档", primary == "#7c3aed", primary)
            check("请求带图", bool(ai_calls and str(ai_calls[0].get("image", "")).startswith("data:image/jpeg")), str(bool(ai_calls)))
            check("请求带候选色", bool(ai_calls and ai_calls[0].get("candidates")), str(ai_calls[0].get("candidates") if ai_calls else None))
            check("请求带当前模式", bool(ai_calls and ai_calls[0].get("mode") == "gradient"), str(ai_calls[0].get("mode") if ai_calls else None))
            toast = " ".join(page.locator(".toast").all_inner_texts())
            check("提示挑了 3 个颜色", "3 个颜色" in toast, toast[:80])
            check("按钮恢复可点", not button.is_disabled())
            check("文案恢复", "AI 取色" in button.inner_text(), button.inner_text())
            page.screenshot(path=str(OUT / "accent_ai_result.png"))

            print("\n[F] 窄屏")
            mctx = browser.new_context(viewport={"width": 375, "height": 900}, device_scale_factor=2)
            mctx.add_cookies([{"name": "cloudnetp_session", "value": cookie, "url": base}])
            mpage = wire(mctx.new_page())
            mpage.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            mpage.wait_for_selector("[data-accent-editor]", state="attached", timeout=20000)
            mpage.locator("[data-accent-mode-btn='gradient']").click()
            mpage.wait_for_timeout(500)
            overflow = mpage.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            check("没有横向溢出", overflow <= 1, f"{overflow}px")
            check("AI 按钮可见", mpage.locator("[data-act='pick-ai']").is_visible())
            check("色标手输框可见", mpage.locator("[data-accent-stop-hex='0']").is_visible())
            mpage.screenshot(path=str(OUT / "accent_custom_mobile.png"), full_page=False)

            print("\n[G] 控制台")
            check("无 JS 报错", not errors, str(errors[:2]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    print(f"\n通过 {passed} 项，失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
