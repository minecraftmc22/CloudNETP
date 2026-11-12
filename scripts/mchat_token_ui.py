# -*- coding: utf-8 -*-
"""对话面板底部「实时 Token 消耗」的 UI 冒烟。

拦截 /api/ai/config 与 /api/ai/chat，不连真实模型，逐场景换配置与 SSE：

  A 正常：done 带 usage → 底部显示 ↑输入 ↓输出（不带 ≈），title 里有本次累计
  B 服务商不给用量（done 没有 usage）→ 仍显示估算值，但保留 ≈
  C 走管理员默认接口（source=default）→ 底部整块不显示
  D 设置里关掉「显示 Token 消耗」→ 不显示
  E 点「新对话」→ 数字与累计一起清零
  F 375px 窄屏：底部不横向溢出

    ./.venv/Scripts/python.exe scripts/mchat_token_ui.py
"""

from __future__ import annotations

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
PASSWORD = "Test123456"

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


BASE_CONFIG = {
    "baseUrl": "https://mine.example.com/v1",
    "hasKey": True,
    "keyMask": "sk-****fake",
    "model": "fake-model",
    "ready": True,
    "vision": False,
    "thinking": False,
    "tools": False,
    "protocol": "openai",
    "maxInput": 0,
    "maxOutput": 0,
    "maxRounds": 8,
    "showTokens": True,
    "source": "user",
    "aiEnabled": True,
    "presets": [],
}

SSE_WITH_USAGE = (
    'event: meta\ndata: {"model":"fake-model","usage":true}\n\n'
    'event: delta\ndata: {"text":"你好呀"}\n\n'
    'event: delta\ndata: {"text":"，我是CDP娘。"}\n\n'
    'event: done\ndata: {"usage":{"input":240,"output":66,"total":306},"hadText":true}\n\n'
)

SSE_NO_USAGE = (
    'event: meta\ndata: {"model":"fake-model","usage":true}\n\n'
    'event: delta\ndata: {"text":"你好呀"}\n\n'
    'event: delta\ndata: {"text":"，我是CDP娘。"}\n\n'
    "event: done\ndata: {}\n\n"
)


def main() -> None:
    from app.config import settings
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-mchat-token-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    row = con.execute("select id, token_version from users where role = 1 order by id limit 1").fetchone()
    if not row:
        raise SystemExit("库里没有管理员，先手动建一个再来跑这个脚本")
    con.execute("update users set password_hash=? where id=?", (hash_password(PASSWORD), row[0]))
    con.commit()
    con.close()
    cookie = create_token(row[0], row[1])

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
            "DB_FALLBACK_ENABLED": "0",
            "SECRET_KEY": settings.SECRET_KEY,
            "AI_ENABLED": "true",
        }
    )
    log_path = tmp / "server.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "main.py"], cwd=str(ROOT), env=env,
        stdout=log_file, stderr=subprocess.STDOUT,
    )

    from playwright.sync_api import sync_playwright

    # 每个场景自己换配置 / SSE：handler 里读这两个可变对象
    live = {"config": dict(BASE_CONFIG), "sse": SSE_WITH_USAGE}

    def usage_text(page) -> str:
        return page.locator("[data-mchat-usage]").inner_text().strip()

    def usage_visible(page) -> bool:
        return page.locator("[data-mchat-usage]").is_visible()

    def reopen(page) -> None:
        """关掉再点开：openMascotChat 会强制重读一次配置（真实用户换配置后也是这条路）"""
        page.click("[data-mchat-close]")
        page.wait_for_timeout(250)
        page.eval_on_selector('[data-mascot-act="chat"]', "el => el.click()")
        page.wait_for_selector("#mascot-chat.is-open", timeout=10000)

    def ask(page, text: str = "在吗") -> None:
        page.fill("[data-mchat-input]", text)
        page.press("[data-mchat-input]", "Enter")
        page.wait_for_selector(".mchat__bubble--bot:not(.is-streaming)", timeout=20000)

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来，看日志：" + str(log_path))

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)

            ctx = browser.new_context(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
            ctx.add_cookies([{"name": "cloudnetp_session", "value": cookie, "url": base}])
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on(
                "console",
                lambda msg: errors.append(msg.text)
                if msg.type == "error" and "Failed to load resource" not in msg.text
                else None,
            )
            page.route(
                "**/api/ai/config",
                lambda route: route.fulfill(
                    status=200,
                    headers={"content-type": "application/json"},
                    body=json.dumps({"config": live["config"]}),
                ),
            )
            page.route(
                "**/api/ai/chat",
                lambda route: route.fulfill(
                    status=200,
                    headers={"content-type": "text/event-stream"},
                    body=live["sse"],
                ),
            )

            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_function(
                "document.getElementById('mascot-actions')?.dataset.bound === '1'", timeout=20000
            )
            page.eval_on_selector('[data-mascot-act="chat"]', "el => el.click()")
            page.wait_for_selector("#mascot-chat", timeout=20000)
            check("刚打开时不显示消耗", not usage_visible(page))

            print("\n[A] 服务端给了准确用量")
            ask(page)
            page.wait_for_selector("[data-mchat-usage]:not([hidden])", timeout=15000)
            text = usage_text(page)
            check("显示输入 / 输出", "240" in text and "66" in text and "输入" in text, text)
            check("拿到准数后不再带 ≈", not text.startswith("≈"), text)
            title = page.locator("[data-mchat-usage]").get_attribute("title") or ""
            check("title 里带本次累计", "240" in title and "66" in title, title)
            page.screenshot(path=str(OUT / "mchat_usage.png"), full_page=False)

            print("\n[B] 服务商不给用量：保留估算并标 ≈")
            live["sse"] = SSE_NO_USAGE
            ask(page)
            page.wait_for_timeout(400)
            text = usage_text(page)
            check("仍然显示估算值", text.startswith("≈") and "输入" in text, text)
            live["sse"] = SSE_WITH_USAGE

            print("\n[C] 走管理员默认接口")
            live["config"] = dict(BASE_CONFIG, source="default", baseUrl="", hasKey=True, model="")
            reopen(page)
            ask(page)
            page.wait_for_timeout(400)
            check("底部整块不显示", not usage_visible(page))

            print("\n[D] 设置里关掉显示")
            live["config"] = dict(BASE_CONFIG, showTokens=False)
            # 设置页保存时会派发这个事件，对话面板收到就立刻重读配置
            page.evaluate("window.dispatchEvent(new CustomEvent('ai:config-changed'))")
            page.wait_for_timeout(400)
            ask(page)
            page.wait_for_timeout(400)
            check("关掉后不显示", not usage_visible(page))

            print("\n[E] 新对话清零")
            live["config"] = dict(BASE_CONFIG)
            page.evaluate("window.dispatchEvent(new CustomEvent('ai:config-changed'))")
            page.wait_for_timeout(400)
            ask(page)
            page.wait_for_selector("[data-mchat-usage]:not([hidden])", timeout=15000)
            before = usage_text(page)
            page.click("[data-mchat-new]")
            page.wait_for_timeout(300)
            check("新对话后数字收起", not usage_visible(page), before)

            print("\n[F] 375px 窄屏")
            page.set_viewport_size({"width": 375, "height": 760})
            page.wait_for_timeout(200)
            ask(page)
            page.wait_for_selector("[data-mchat-usage]:not([hidden])", timeout=15000)
            overflow = page.evaluate(
                """() => {
                    const el = document.querySelector('[data-mchat-usage]');
                    const foot = el.closest('.mchat__foot');
                    return {
                        docOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
                        outside: el.getBoundingClientRect().right > foot.getBoundingClientRect().right + 1,
                    };
                }"""
            )
            check("没有横向溢出", not overflow["docOverflow"], str(overflow))
            check("token 没被挤出底栏", not overflow["outside"], str(overflow))
            page.screenshot(path=str(OUT / "mchat_usage_mobile.png"), full_page=False)

            check("页面没有 JS 报错", not errors, str(errors[:3]))
            ctx.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_file.close()

    print()
    if failed:
        print(f"失败 {failed} 项，通过 {passed} 项")
        raise SystemExit(1)
    print(f"全部通过（{passed} 项）")


if __name__ == "__main__":
    main()
