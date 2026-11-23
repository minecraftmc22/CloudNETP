# -*- coding: utf-8 -*-
"""看板娘「删除 / 快传发送」权限项的界面回归。

库副本起独立实例，Playwright 驱动本机 Chrome：

  A 设置 → 看板娘 · 对话设置：新增删除权限项，带「谨慎开启」标签，默认关，点开即落库
  B 快传那一行改成「取件码 / 发给用户」，说明里带上发送
  C 对话里删除工具跑完：工具条目显示「删除」，没有 JS 异常
  D 390px：危险项那一行不撑出横向滚动，「谨慎开启」标签还在行内

    ./.venv/Scripts/python.exe scripts/ai_delete_perm_ui.py
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
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
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
    print(log_path.read_text(encoding="utf-8", errors="replace")[-1500:])
    return False


def http(base: str, method: str, path: str, body=None, cookie: str = ""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Origin", base)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except Exception:
            return exc.code, {"raw": raw[:200].decode("utf-8", "replace")}


def login_cookie(base: str, username: str, password: str) -> str:
    req = urllib.request.Request(
        base + "/api/auth/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", base)
    with urllib.request.urlopen(req, timeout=30) as resp:
        for item in resp.headers.get_all("Set-Cookie") or []:
            if item.startswith("cloudnetp_session="):
                return item.split(";", 1)[0].split("=", 1)[1]
    raise SystemExit("登录没拿到 cookie")


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
        "deleteFiles": True,
        "quickTransfer": True,
        "protocol": "openai",
        "maxInput": 0,
        "maxOutput": 0,
        "maxRounds": 8,
        "presets": [],
    },
    "isAdmin": False,
    "systemPrompt": "",
}


def sse(*events: tuple[str, dict]) -> str:
    return "".join(
        f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n" for name, data in events
    )


DELETE_CHAT = sse(
    ("meta", {"model": "fake-model"}),
    (
        "tool",
        {
            "id": "t1",
            "name": "delete_item",
            "label": "删除",
            "status": "done",
            "result": "已删除：旧备份.zip（共 2 个条目，已放进回收站，需要的话可以还原）。",
            "images": 0,
            "settings": None,
            "write": True,
        },
    ),
    ("delta", {"text": "删掉啦，回收站里还能找回来~"}),
    ("done", {}),
)


def main() -> int:
    from app.config import settings as app_settings
    from app.security import hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-ai-del-ui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)

    con = sqlite3.connect(str(db_copy))
    row = con.execute("select id, username from users where role = 1 order by id limit 1").fetchone()
    if not row:
        raise SystemExit("副本库里没有管理员")
    admin_id, admin_name = row
    con.execute("update users set password_hash=? where id=?", (hash_password(PASSWORD), admin_id))
    con.execute("update user_settings set ai_config=NULL where user_id=?", (admin_id,))
    con.commit()
    con.close()

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
            "SECRET_KEY": app_settings.SECRET_KEY,
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
            raise SystemExit("服务没起来：" + str(log_path))
        session = login_cookie(base, admin_name, PASSWORD)
        cookie = f"cloudnetp_session={session}"

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
            ctx.add_cookies([{"name": "cloudnetp_session", "value": session, "url": base}])
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on(
                "console",
                lambda msg: errors.append(msg.text)
                if msg.type == "error" and "Failed to load resource" not in msg.text
                else None,
            )

            print("\n[A] 对话设置里的删除权限")
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector("[data-settings-tab]", timeout=20000)
            page.click('[data-settings-tab="mascot"]')
            page.wait_for_selector('[data-ai-flag="deleteFiles"]', state="attached", timeout=20000)
            toggle = page.locator('[data-ai-flag="deleteFiles"]')
            check("开关存在", toggle.count() == 1)
            check("默认是关的", not toggle.is_checked())
            label = page.locator("label.ai-line", has=toggle)
            text = label.inner_text()
            check("标题写的是「删除文件 / 清空回收站」", "删除文件" in text and "清空回收站" in text, text[:40])
            check("带「谨慎开启」提示", "谨慎开启" in text, text[:60])
            check("说明里写了可还原与不可恢复", "回收站" in text and "找不回来" in text, "")
            check("整行有危险样式", "ai-line--danger" in (label.get_attribute("class") or ""))
            check(
                "「谨慎开启」用了危险标签",
                page.locator('label.ai-line .chip--danger').count() == 1,
                str(page.locator("label.ai-line .chip--danger").count()),
            )
            label.scroll_into_view_if_needed()
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT / "ai_delete_toggle.png"), full_page=False)

            print("\n[B] 快传那一行带上「发给用户」")
            quick = page.locator("label.ai-line", has=page.locator('[data-ai-flag="quickTransfer"]'))
            qtext = quick.inner_text()
            check("标题提到发给用户", "发给用户" in qtext, qtext[:30])
            check("说明提到对方同意", "同意" in qtext, "")

            print("\n[C] 点开删除权限就落库")
            label.locator(".switch").click()
            page.wait_for_timeout(900)
            status, data = http(base, "GET", "/api/ai/config", None, cookie)
            cfg = (data or {}).get("config") or {}
            check("接口里 deleteFiles=true", status == 200 and cfg.get("deleteFiles") is True, json.dumps(cfg)[:60])
            con = sqlite3.connect(str(db_copy))
            stored = con.execute("select ai_config from user_settings where user_id=?", (admin_id,)).fetchone()
            con.close()
            raw = json.loads(stored[0]) if stored and stored[0] else {}
            check("库里 ai_config 有 deleteFiles", raw.get("deleteFiles") is True, json.dumps(raw)[:80])
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("[data-settings-tab]", timeout=20000)
            page.click('[data-settings-tab="mascot"]')
            page.wait_for_selector('[data-ai-flag="deleteFiles"]', state="attached", timeout=20000)
            check("刷新后仍然是勾上的", page.locator('[data-ai-flag="deleteFiles"]').is_checked())

            print("\n[D] 对话里的删除工具条目")
            page.route(
                "**/api/ai/config",
                lambda route: route.fulfill(
                    status=200, headers={"content-type": "application/json"}, body=json.dumps(CONFIG)
                ),
            )
            page.route(
                "**/api/ai/chat",
                lambda route: route.fulfill(
                    status=200, headers={"content-type": "text/event-stream"}, body=DELETE_CHAT
                ),
            )
            page.wait_for_selector('[data-mascot-act="chat"]', timeout=20000)
            page.wait_for_function(
                "document.getElementById('mascot-actions')?.dataset.bound === '1'", timeout=20000
            )
            page.eval_on_selector('[data-mascot-act="chat"]', "el => el.click()")
            page.wait_for_selector("#mascot-chat", timeout=20000)
            page.fill("[data-mchat-input]", "把旧备份.zip 删掉")
            page.press("[data-mchat-input]", "Enter")
            page.wait_for_selector(".mchat__msg.is-bot .mchat__bubble:not(.is-streaming)", timeout=20000)
            tool_label = page.evaluate(
                "document.querySelector('.mchat__tool [data-tool-label]')?.textContent || ''"
            )
            check("工具条目显示「删除」", "删除" in tool_label, tool_label)
            bubble = page.locator(".mchat__msg.is-bot .mchat__bubble").last.inner_text()
            check("回答提到回收站", "回收站" in bubble, bubble[:40])
            page.screenshot(path=str(OUT / "ai_delete_tool.png"), full_page=False)

            print("\n[E] 390px：危险项那一行别撑破卡片")
            page.eval_on_selector("[data-mchat-close]", "el => el.click()")
            page.wait_for_timeout(300)
            page.set_viewport_size({"width": 390, "height": 780})
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector("[data-settings-tab]", timeout=20000)
            page.click('[data-settings-tab="mascot"]')
            page.wait_for_selector('[data-ai-flag="deleteFiles"]', state="attached", timeout=20000)
            box = page.locator("label.ai-line", has=page.locator('[data-ai-flag="deleteFiles"]')).bounding_box()
            check("这一行在视口内", bool(box) and box["x"] >= 0 and box["x"] + box["width"] <= 391, str(box))
            overflow = page.evaluate(
                "(() => { const v = document.querySelector('.view-body'); return v ? v.scrollWidth - v.clientWidth : -1; })()"
            )
            check("没有横向滚动", overflow <= 1, str(overflow))
            chip = page.locator("label.ai-line .chip--danger").bounding_box()
            check("标签没被挤出行外", bool(chip) and chip["width"] > 20, str(chip))
            page.locator("label.ai-line", has=page.locator('[data-ai-flag="deleteFiles"]')).scroll_into_view_if_needed()
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT / "ai_delete_mobile.png"), full_page=False)

            print("\n[F] JS 异常")
            check("没有报错", not errors, "; ".join(errors[:3]))

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_file.close()

    print(f"\n结果：{passed} 通过，{failed} 失败")
    import shutil

    if failed == 0:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print("临时目录保留：", tmp)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
