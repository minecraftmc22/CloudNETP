# -*- coding: utf-8 -*-
"""设置页 / 管理后台的 UI 冒烟（AI 默认接口 + 重置看板娘位置）。

用库副本起独立实例，Playwright 驱动本机 Chrome：
  1. 普通用户在「设置 → 看板娘 · 对话设置」看到「使用管理员提供的默认接口」，
     接口表单默认收起，点「使用自己的接口」才展开；权限开关始终可见
  2. 「设置 → 装饰」里的「重置看板娘位置」能把 --mascot-dx/dy 归零并清掉 localStorage
  3. 管理后台能看到默认接口的地址、密钥掩码，改模型能保存

    ./.venv/Scripts/python.exe scripts/ai_default_ui.py
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

DEFAULT_BASE = "https://default.example.com/v1"
DEFAULT_KEY = "sk-default-1234567890abcdef"
DEFAULT_MODEL = "default-vision-model"

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


def main() -> None:
    from app.config import settings
    from app.security import create_token

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-ai-default-ui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    admin = con.execute("select id, token_version from users where role = 1 order by id limit 1").fetchone()
    plain = con.execute("select id, token_version from users where role != 1 order by id limit 1").fetchone()
    if not plain:
        con.execute(
            "insert into users (username, password_hash, display_name, quota_bytes, used_bytes, role,"
            " token_version, created_at) values (?,?,?,?,?,?,?,datetime('now'))",
            ("defaultui", "x", "DefaultUI", 10 * 1024 ** 3, 0, 0, 0),
        )
        con.commit()
        plain = con.execute("select id, token_version from users where username = 'defaultui'").fetchone()
    con.close()
    if not admin:
        raise SystemExit("库里没有管理员，先手动建一个再来跑这个脚本")
    admin_cookie = create_token(admin[0], admin[1])
    plain_cookie = create_token(plain[0], plain[1])

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
            "AI_ENABLED": "true",
            "AI_DEFAULT_ENABLED": "true",
            "AI_DEFAULT_BASE_URL": DEFAULT_BASE,
            "AI_DEFAULT_API_KEY": DEFAULT_KEY,
            "AI_DEFAULT_MODEL": DEFAULT_MODEL,
            "AI_DEFAULT_PROTOCOL": "openai",
            "AI_DEFAULT_VISION": "true",
            "AI_DEFAULT_MAX_INPUT": "32000",
            "AI_DEFAULT_MAX_OUTPUT": "2048",
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

            def make_page(cookie: str):
                ctx = browser.new_context(viewport={"width": 1440, "height": 1100}, device_scale_factor=2)
                ctx.add_cookies(
                    [{"name": "cloudnetp_session", "value": cookie, "url": base}]
                )
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
                return ctx, page, errors

            print("\n[A] 普通用户：默认接口状态 + 表单收起")
            ctx, page, errors = make_page(plain_cookie)
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector("[data-ai-default]", state="attached", timeout=20000)
            card = page.locator("[data-ai-card]")
            text = card.inner_text()
            check("显示默认接口提示", "管理员提供的默认接口" in text, text[:120])
            check("显示默认模型", DEFAULT_MODEL in text, text[:160])
            check("没有暴露默认地址", DEFAULT_BASE not in text)
            check("没有暴露默认密钥", DEFAULT_KEY not in text)
            check("接口表单默认收起", page.locator("[data-ai-form]").is_hidden())
            check("权限开关仍然可操作", page.locator('[data-ai-flag="readContent"]').count() == 1)
            card.screenshot(path=str(OUT / "ai_default_settings.png"))

            page.click("[data-ai-use-own]")
            page.wait_for_timeout(300)
            check("点「使用自己的接口」后表单展开", page.locator("[data-ai-form]").is_visible())
            check("展开后地址输入框可用", page.locator("[data-ai-base]").is_visible())

            print("\n[A2] 手机端不横向溢出")
            mctx = browser.new_context(viewport={"width": 375, "height": 800}, device_scale_factor=2)
            mctx.add_cookies([{"name": "cloudnetp_session", "value": plain_cookie, "url": base}])
            mpage = mctx.new_page()
            mpage.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            mpage.wait_for_selector("[data-ai-default]", state="attached", timeout=20000)
            mpage.wait_for_timeout(600)
            overflow = mpage.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            check("设置页 375px 无横向溢出", overflow <= 1, str(overflow))
            mpage.screenshot(path=str(OUT / "ai_default_mobile.png"), full_page=False)
            mctx.close()

            print("\n[B] 重置看板娘位置")
            page.evaluate(
                """() => {
                    localStorage.setItem("mascot-pos", JSON.stringify({ dx: 120, dy: -80 }));
                    localStorage.setItem("mascot-size", "140");
                }"""
            )
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("[data-mascot-reset]", state="attached", timeout=20000)
            page.wait_for_timeout(800)
            dx = page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--mascot-dx').trim()"
            )
            check("重置前偏移已生效", dx == "120px", dx)
            page.locator("[data-mascot-reset]").scroll_into_view_if_needed()
            page.click("[data-mascot-reset]")
            page.wait_for_timeout(500)
            dx = page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--mascot-dx').trim()"
            )
            dy = page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--mascot-dy').trim()"
            )
            saved = page.evaluate("localStorage.getItem('mascot-pos')")
            size = page.evaluate("localStorage.getItem('mascot-size')")
            check("偏移归零 dx", dx in {"0px", ""}, dx)
            check("偏移归零 dy", dy in {"0px", ""}, dy)
            check("localStorage 里的位置已归零", json.loads(saved or "{}") == {"dx": 0, "dy": 0}, str(saved))
            check("大小不受影响", size == "140", str(size))
            check("设置页没有 JS 报错", not errors, str(errors[:2]))
            ctx.close()

            print("\n[C] 管理后台：能看到并修改默认接口")
            ctx, page, errors = make_page(admin_cookie)
            page.goto(f"{base}/#/admin", wait_until="domcontentloaded")
            page.wait_for_selector('[data-tab="settings"]', timeout=20000)
            page.click('[data-tab="settings"]')
            page.wait_for_selector('[data-card="ai"]', timeout=20000)
            card = page.locator('[data-card="ai"]')
            text = card.inner_text()
            check("卡片渲染出来了", "看板娘 AI 默认接口" in text, text[:80])
            base_placeholder = page.locator("[data-ai-admin-base]").get_attribute("placeholder") or ""
            base_value = page.locator("[data-ai-admin-base]").input_value()
            check(
                "地址对管理员可见（输入框或占位）",
                DEFAULT_BASE in base_placeholder or DEFAULT_BASE in base_value,
                f"{base_value} | {base_placeholder}",
            )
            placeholder = page.locator("[data-ai-admin-key]").get_attribute("placeholder") or ""
            check("密钥只显示掩码", "…" in placeholder and DEFAULT_KEY not in placeholder, placeholder)
            check("总开关显示已开启", "已开启" in text, text[:120])
            card.screenshot(path=str(OUT / "ai_default_admin.png"))

            model_input = page.locator("[data-ai-admin-model]")
            model_input.fill("ui-picked-model")
            page.click("[data-ai-admin-save]")
            page.wait_for_timeout(1200)
            check("保存后有成功提示", page.locator(".toast").count() > 0)
            check("后台没有 JS 报错", not errors, str(errors[:2]))
            page.screenshot(path=str(OUT / "ai_default_admin_saved.png"), full_page=False)
            ctx.close()

            print("\n[D] AI 总开关关闭时的前端表现")
            ctx2, page2, errors2 = make_page(plain_cookie)
            page2.route(
                "**/api/ai/config",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "config": {"aiEnabled": False, "ready": False, "source": "none"},
                            "isAdmin": False,
                            "systemPrompt": "",
                        }
                    ),
                ),
            )
            page2.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page2.wait_for_selector("[data-ai-card]", timeout=20000)
            page2.wait_for_timeout(700)
            text = page2.locator("[data-ai-card]").inner_text()
            check("设置页提示 AI 已关闭", "管理员已关闭 AI 功能" in text, text[:100])
            check("接口表单不再渲染", page2.locator("[data-ai-base]").count() == 0)
            check("html 标记为 ai=off", page2.evaluate("document.documentElement.dataset.ai") == "off")
            display = page2.evaluate(
                "getComputedStyle(document.querySelector('[data-mascot-act=chat]')).display"
            )
            check("看板娘操作条上的对话按钮隐藏", display == "none", display)
            check("关闭态没有 JS 报错", not errors2, str(errors2[:2]))
            ctx2.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failed:
        print(f"失败 {failed} 项，通过 {passed} 项")
        raise SystemExit(1)
    print(f"全部通过（{passed} 项）")


if __name__ == "__main__":
    main()
