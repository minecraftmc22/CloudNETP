# -*- coding: utf-8 -*-
"""设置页「查看文件内容」包含图片的 UI 冒烟。

用 dev.db 的副本起一个独立实例（不动真实库），Playwright 驱动本机 Chrome：
  1. 「查看文件内容」的说明里明确写了支持图片，中英文字典同步
  2. 勾上「查看文件内容」+「图片输入」后，配置真的落库（readContent / vision 都为 true）
  3. 截图留档

    ./.venv/Scripts/python.exe scripts/ai_image_ui.py
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

TEXT_ZH = (
    "读取 txt / md / 代码等纯文本（最多 4000 字），以及 png / jpg / webp / gif 图片"
    "（需同时开启「图片输入」）；不勾选时只能看文件名、大小和修改时间"
)
TEXT_EN = (
    "Reads text files (txt / md / code, up to 4000 characters) and images "
    "(png / jpg / webp / gif, requires Image input)"
)

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
    src_path = ROOT / "dev.db"
    if not src_path.is_file():
        src_path = ROOT / "storage" / "db" / "fallback.db"
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
    print("\n[0] 文案与字典")
    source = (ROOT / "static" / "js" / "ui" / "ai-config.js").read_text(encoding="utf-8")
    check("设置页文案写了图片", TEXT_ZH in source)
    dictionary = (ROOT / "static" / "js" / "i18n" / "en-US.js").read_text(encoding="utf-8")
    check("英文字典有这条 key", f'"{TEXT_ZH}"' in dictionary)
    check("英文译文提到 images", TEXT_EN in dictionary)
    check(
        "图片输入的说明也提了看图",
        "「查看文件内容」也能直接看网盘里的图片" in source,
    )

    from app.config import settings
    from app.security import create_token

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-imgui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    row = con.execute("select id, token_version from users where role = 1 order by id limit 1").fetchone()
    con.close()
    if not row:
        raise SystemExit("库里没有管理员，先手动建一个再来跑这个脚本")
    cookie = f"cloudnetp_session={create_token(row[0], row[1])}"

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
        [str(VENV_PY), "main.py"],
        cwd=str(ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
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
            ctx = browser.new_context(viewport={"width": 1440, "height": 1100}, device_scale_factor=2)
            ctx.add_cookies([{"name": "cloudnetp_session", "value": cookie.split("=", 1)[1], "url": base}])
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

            print("\n[A] 设置 → 看板娘 · 对话设置")
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector('[data-ai-flag="readContent"]', state="attached", timeout=20000)
            card = page.locator("[data-ai-card]")
            text = card.inner_text()
            check("卡片渲染出来了", "数据操作权限" in text)
            check("说明里写了 png / jpg / webp / gif", "png / jpg / webp / gif" in text, text[:120])
            check("说明里提到「图片输入」", "图片输入" in text)
            card.screenshot(path=str(OUT / "ai_image_card.png"))

            print("\n[B] 勾选「查看文件内容」+「图片输入」")
            for flag in ("readContent", "vision"):
                # 开关的原生 input 被样式隐藏（0 尺寸），直接改状态并派发 change
                page.evaluate(
                    """(flag) => {
                        const box = document.querySelector(`[data-ai-flag="${flag}"]`);
                        if (box && !box.checked) {
                            box.checked = true;
                            box.dispatchEvent(new Event("change", { bubbles: true }));
                        }
                    }""",
                    flag,
                )
            page.wait_for_timeout(1600)
            payload = json.loads(ctx.request.get(f"{base}/api/ai/config").text())
            cfg = payload.get("config") or {}
            check("readContent 已落库", cfg.get("readContent") is True, str(cfg.get("readContent")))
            check("vision 已落库", cfg.get("vision") is True, str(cfg.get("vision")))
            check("页面没有 JS 报错", not errors, str(errors[:2]))

            page.screenshot(path=str(OUT / "ai_image_settings.png"), full_page=False)
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
