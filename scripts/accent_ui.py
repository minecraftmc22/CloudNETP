# -*- coding: utf-8 -*-
"""设置页「主题色：单色 / 渐变」与卡片着色的 UI 冒烟。

用库副本起独立实例，Playwright 驱动本机 Chrome，真实走一遍：

  A 默认单色：只显示色板，点预设色 → accent_style = {solid, [色]}
  B 切渐变：自动补够两色，面板出现色标；--accent-grad / --accent-tint 生效
  C 卡片底色真的叠了主题薄雾（background-image 不是 none）
  D 添加 / 删除色标：上限 5、下限 2，删除按钮到下限时禁用
  E 预设渐变一键套用（极光 3 色）
  F 改色标颜色 → 落库
  G 切回单色 → 落库为单色单色值
  G2 上传背景图 → 主题色默认改成图片渐变（至少两色）
  H 375px 无横向溢出

    ./.venv/Scripts/python.exe scripts/accent_ui.py
"""

from __future__ import annotations

import json
import os
import shutil
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


def main() -> int:
    from app.config import settings
    from app.security import create_token

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-accent-ui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    plain = con.execute("select id, token_version from users where role != 1 order by id limit 1").fetchone()
    if not plain:
        con.execute(
            "insert into users (username, password_hash, display_name, quota_bytes, used_bytes, role,"
            " token_version, created_at) values (?,?,?,?,?,?,?,datetime('now'))",
            ("accentui", "x", "AccentUI", 10 * 1024 ** 3, 0, 0, 0),
        )
        con.commit()
        plain = con.execute("select id, token_version from users where username = 'accentui'").fetchone()
    con.commit()
    con.close()
    cookie = create_token(plain[0], plain[1])

    def saved_accent():
        conn = sqlite3.connect(str(db_copy))
        try:
            row = conn.execute(
                "select accent_style, accent_color from user_settings where user_id = ?", (plain[0],)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {}, ""
        return json.loads(row[0] or "{}"), (row[1] or "").lower()

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

            ctx = browser.new_context(viewport={"width": 1440, "height": 1200}, device_scale_factor=2)
            ctx.add_cookies([{"name": "cloudnetp_session", "value": cookie, "url": base}])
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            page.add_init_script(
                "window.addEventListener('DOMContentLoaded', () => {"
                "  const s = document.createElement('style');"
                "  s.textContent = '#mascot{display:none!important}';"
                "  document.head.appendChild(s);"
                "});"
            )

            def open_settings():
                page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
                page.wait_for_selector("[data-accent-editor]", state="attached", timeout=20000)

            def accent_var(name: str) -> str:
                return page.evaluate(
                    f"getComputedStyle(document.documentElement).getPropertyValue('{name}').trim()"
                )

            print("\n[A] 默认单色")
            open_settings()
            check(
                "「单色」处于选中态",
                "is-active" in (page.locator('[data-accent-mode-btn="solid"]').get_attribute("class") or ""),
            )
            check("单色色板可见", page.locator("[data-swatches]").is_visible())
            check("渐变面板隐藏", page.locator("[data-accent-gradient]").is_hidden())
            page.locator('[data-color="#60a5fa"]').click()
            page.wait_for_timeout(700)
            style, primary = saved_accent()
            check("点预设色落库为单色", style.get("mode") == "solid", str(style))
            check("主色同步", primary == "#60a5fa" and style.get("colors") == ["#60a5fa"], f"{primary} {style}")

            print("\n[B] 切到渐变")
            page.locator('[data-accent-mode-btn="gradient"]').click()
            page.wait_for_timeout(700)
            style, primary = saved_accent()
            check("落库为渐变", style.get("mode") == "gradient", str(style))
            check("自动补够两个颜色", len(style.get("colors") or []) >= 2, str(style.get("colors")))
            check("渐变面板可见", page.locator("[data-accent-gradient]").is_visible())
            check("色标数量与库一致", page.locator("[data-accent-stop]").count() == len(style.get("colors") or []))
            grad = accent_var("--accent-grad")
            tint = accent_var("--accent-tint")
            check(
                "--accent-grad 是多色渐变",
                "linear-gradient" in grad and grad.count("#") >= 2,
                grad[:80],
            )
            check("--accent-tint 已生效", tint not in {"", "none"} and "gradient" in tint, tint[:80])

            print("\n[C] 卡片底色叠了主题薄雾")
            # 设置页外层 .settings-card 是透明容器，真正露底色的是 .settings-card__body
            card_image = page.evaluate(
                "getComputedStyle(document.querySelector('.settings .settings-card__body')).backgroundImage"
            )
            check("卡片 background-image 含渐变", "gradient" in card_image, card_image[:90])
            check(
                "卡片仍保留半透明底色",
                page.evaluate(
                    "getComputedStyle(document.querySelector('.settings .settings-card__body')).backgroundColor"
                )
                != "rgba(0, 0, 0, 0)",
            )

            print("\n[D] 添加 / 删除色标")
            before = page.locator("[data-accent-stop]").count()
            page.locator("[data-accent-add]").click()
            page.wait_for_timeout(600)
            after = page.locator("[data-accent-stop]").count()
            check("添加一个色标", after == before + 1, f"{before} → {after}")
            check("库里颜色数同步", len(saved_accent()[0].get("colors") or []) == after)

            for _ in range(6):
                if page.locator("[data-accent-add]").is_disabled():
                    break
                page.locator("[data-accent-add]").click()
                page.wait_for_timeout(350)
            check("最多 5 个颜色", page.locator("[data-accent-stop]").count() == 5)
            check("到上限后「添加颜色」禁用", page.locator("[data-accent-add]").is_disabled())

            for _ in range(5):
                btn = page.locator("[data-accent-stop-del]").first
                if btn.is_disabled():
                    break
                btn.click()
                page.wait_for_timeout(350)
            check("最少保留 2 个颜色", page.locator("[data-accent-stop]").count() == 2)
            check("到下限后删除按钮禁用", page.locator("[data-accent-stop-del]").first.is_disabled())
            check("删除后库里也是 2 色", len(saved_accent()[0].get("colors") or []) == 2)

            print("\n[E] 预设渐变")
            presets = page.locator("[data-accent-preset]")
            check("预设渐变按钮存在", presets.count() >= 3, str(presets.count()))
            presets.last.click()
            page.wait_for_timeout(700)
            style, primary = saved_accent()
            check("预设套用后是渐变", style.get("mode") == "gradient", str(style))
            check("预设颜色生效（3 色）", len(style.get("colors") or []) == 3, str(style.get("colors")))
            check("色标跟着重绘", page.locator("[data-accent-stop]").count() == 3)

            print("\n[F] 改色标颜色")
            first = page.locator("[data-accent-stop]").first
            first.evaluate(
                """(el) => {
                  el.value = '#123456';
                  el.dispatchEvent(new Event('input', { bubbles: true }));
                  el.dispatchEvent(new Event('change', { bubbles: true }));
                }"""
            )
            page.wait_for_timeout(700)
            style, primary = saved_accent()
            check("改色后落库", (style.get("colors") or [""])[0] == "#123456", str(style.get("colors")))
            check("主色跟着第一个色标", primary == "#123456", primary)

            print("\n[G] 切回单色")
            page.locator('[data-accent-mode-btn="solid"]').click()
            page.wait_for_timeout(700)
            style, primary = saved_accent()
            check("落库回单色", style.get("mode") == "solid", str(style))
            check("只留一个颜色", len(style.get("colors") or []) == 1, str(style.get("colors")))
            check("单色面板可见", page.locator("[data-swatches]").is_visible())
            tint = accent_var("--accent-tint")
            check("单色也有卡片薄雾", tint not in {"", "none"} and "gradient" in tint, tint[:80])

            print("\n[G2] 上传背景图 → 主题色默认改成图片渐变")
            from PIL import Image, ImageDraw  # noqa: WPS433

            img_path = tmp / "bg-grad.png"
            pic = Image.new("RGB", (600, 400), (220, 80, 140))
            ImageDraw.Draw(pic).rectangle([300, 0, 600, 400], fill=(70, 140, 230))
            pic.save(img_path)
            page.locator("[data-bg-input]").set_input_files(str(img_path))
            page.wait_for_function(
                "() => document.querySelector('[data-bg-busy]')?.hidden !== false", timeout=20000
            )
            page.wait_for_timeout(900)
            style, primary = saved_accent()
            check("上传后默认是渐变", style.get("mode") == "gradient", str(style))
            colors = style.get("colors") or []
            check("从图片取到至少两个颜色", len(colors) >= 2, str(colors))
            check("主色与第一个渐变色同步", primary == colors[0], f"{primary} / {colors[:2]}")
            grad = accent_var("--accent-grad")
            check("--accent-grad 跟着变成图片色", grad.count("#") >= 2, grad[:80])
            check("设置页渐变面板可见", page.locator("[data-accent-gradient]").is_visible())

            print("\n[H] 窄屏")
            mctx = browser.new_context(viewport={"width": 375, "height": 800}, device_scale_factor=2)
            mctx.add_cookies([{"name": "cloudnetp_session", "value": cookie, "url": base}])
            mpage = mctx.new_page()
            mpage.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            mpage.wait_for_selector("[data-accent-editor]", state="attached", timeout=20000)
            mpage.locator('[data-accent-mode-btn="gradient"]').click()
            mpage.wait_for_timeout(500)
            overflow = mpage.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            check("375px 无横向溢出", overflow <= 1, str(overflow))
            check("窄屏色标可见", mpage.locator("[data-accent-stop]").first.is_visible())
            page.locator("[data-accent-editor]").screenshot(path=str(OUT / "accent_editor.png"))
            mpage.locator(".settings-card").first.screenshot(path=str(OUT / "accent_mobile.png"))
            mctx.close()

            check("设置页没有 JS 报错", not errors, str(errors[:2]))
            ctx.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failed:
        print(f"失败 {failed} 项")
        return 1
    print(f"全部通过（{passed} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
