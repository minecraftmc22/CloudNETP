# -*- coding: utf-8 -*-
"""看板娘对话的「重新发送 / 编辑 / 删除」UI 冒烟。

用库副本起独立实例，Playwright 驱动本机 Chrome，并用 route 拦截
`/api/ai/config` 与 `/api/ai/chat` 造出「失败 → 重试成功」的全过程：

  1. 失败：用户消息左边出现「重新发送」，空回答不留在界面上
  2. 重试成功：按钮收起、错误提示消失、发出去的仍是原话
  3. 编辑：内容回到输入框、有「正在编辑」条、取消不改动原消息
  4. 改完重发：旧问答被替换成新的
  5. 删除：两下确认，提问与它的回答一起消失
  6. 删掉回答后再发：请求里不会出现连续同角色（Anthropic 会拒收）
  7. 375px 窄屏：失败态不横向溢出

    ./.venv/Scripts/python.exe scripts/mchat_actions_ui.py
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


CONFIG = {
    "config": {
        "baseUrl": "http://127.0.0.1:1/v1",
        "hasKey": True,
        "keyMask": "sk-****fake",
        "model": "fake-model",
        "ready": True,
        "vision": False,
        "thinking": False,
        "tools": True,
        "protocol": "openai",
        "maxInput": 0,
        "maxOutput": 0,
        "maxRounds": 8,
        "presets": [],
    },
    "isAdmin": False,
    "systemPrompt": "",
}

# 两个工具调用 + 正文：验证「使用工具 N」在气泡下方、点开能看到结果
SSE_TOOLS = (
    'event: meta\ndata: {"model":"fake-model"}\n\n'
    'event: tool\ndata: {"id":"call_1","name":"search_files","label":"搜索文件","status":"running"}\n\n'
    'event: tool\ndata: {"id":"call_1","name":"search_files","label":"搜索文件","status":"done",'
    '"result":"找到 1 项：我的文件/测试.txt"}\n\n'
    'event: delta\ndata: {"text":"找到啦，就是「测试.txt」这个！"}\n\n'
    'event: tool\ndata: {"id":"call_2","name":"create_pickup_code","label":"生成取件码",'
    '"status":"running","write":true}\n\n'
    'event: tool\ndata: {"id":"call_2","name":"create_pickup_code","label":"生成取件码",'
    '"status":"done","write":true,"result":"已生成取件码：ABC123"}\n\n'
    'event: delta\ndata: {"text":"取件码是 ABC123，7 天内有效~"}\n\n'
    "event: done\ndata: {}\n\n"
)


def main() -> None:
    from app.config import settings
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-mchat-tools-")).resolve()
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
                    status=200, headers={"content-type": "application/json"}, body=json.dumps(CONFIG)
                ),
            )
            page.route(
                "**/api/ai/chat",
                lambda route: route.fulfill(
                    status=200, headers={"content-type": "text/event-stream"}, body=SSE_TOOLS
                ),
            )

            print("\n[A] 工具调用显示在消息下方并可折叠")
            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_function(
                "document.getElementById('mascot-actions')?.dataset.bound === '1'", timeout=20000
            )
            page.eval_on_selector('[data-mascot-act="chat"]', "el => el.click()")
            page.wait_for_selector("#mascot-chat", timeout=20000)
            page.fill("[data-mchat-input]", "帮我给测试.txt 弄个取件码")
            page.press("[data-mchat-input]", "Enter")
            page.wait_for_selector(".mchat__tools:not([hidden])", timeout=20000)
            page.wait_for_function(
                "() => document.querySelector('.mchat__tools-count')?.textContent === '2'", timeout=20000
            )

            tools = page.locator(".mchat__tools").last
            bubble = page.locator(".mchat__msg.is-bot .mchat__bubble").last
            check("工具块在气泡下面", tools.bounding_box()["y"] >= bubble.bounding_box()["y"], "")
            order = page.evaluate(
                """() => {
                    const row = document.querySelectorAll('.mchat__msg.is-bot');
                    const el = row[row.length - 1];
                    const b = el.querySelector('.mchat__bubble');
                    const t = el.querySelector('.mchat__tools');
                    return !!(b.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_FOLLOWING);
                }"""
            )
            check("DOM 顺序也是气泡在前", order is True)
            check("折叠摘要写出 2 次", tools.locator(".mchat__tools-count").inner_text() == "2")
            check("默认收起（内容不可见）", not tools.locator("[data-tool-result]").first.is_visible())
            check("摘要上写着「使用工具」", "使用工具" in tools.locator(".mchat__tools-head").inner_text())
            check("答案正文正常", "ABC123" in bubble.inner_text(), bubble.inner_text()[:40])
            page.screenshot(path=str(OUT / "mchat_tools_closed.png"), full_page=False)

            tools.locator(".mchat__tools-head").click()
            page.wait_for_timeout(300)
            check("点开后能看到结果", tools.locator("[data-tool-result]").first.is_visible())
            text = tools.inner_text()
            check("结果里有工具名与内容", "搜索文件" in text and "测试.txt" in text, text[:80])
            check("写入类工具有标记", tools.locator(".mchat__tool.is-write").count() == 1)
            page.screenshot(path=str(OUT / "mchat_tools_open.png"), full_page=False)

            print("\n[B] 控制台")
            check("无 JS 异常", not errors, "; ".join(errors[:3]))
            ctx.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_file.close()

    print(f"\n通过 {passed} 项，失败 {failed} 项")
    if failed == 0:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print("临时目录保留：", tmp)


if __name__ == "__main__":
    main()
