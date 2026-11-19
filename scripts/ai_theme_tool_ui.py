# -*- coding: utf-8 -*-
"""看板娘「改主题色」工具的界面回归。

库副本起独立实例，Playwright 驱动本机 Chrome：

  A 设置 → 看板娘 · 对话设置：新增「修改界面主题色」开关，默认关，点开即落库
  B 对话里工具改了单色：界面立刻换色、设置页取色控件同步、toast 提示、工具条目有记录
  C 渐变：两个以上颜色时 --accent-bar 生效，设置页渐变面板跟着切过去
  D 390px：新开关那一行不把卡片撑出横向滚动
  E 无 JS 异常

    ./.venv/Scripts/python.exe scripts/ai_theme_tool_ui.py
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
        "themeColor": True,
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
    return "".join(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n" for name, data in events)


def tool_done(settings: dict, text: str) -> str:
    return sse(
        ("meta", {"model": "fake-model"}),
        (
            "tool",
            {
                "id": "t1",
                "name": "set_theme_color",
                "label": "改主题色",
                "status": "done",
                "result": text,
                "images": 0,
                "settings": settings,
                "write": True,
            },
        ),
        ("delta", {"text": "换好啦，看看喜不喜欢~"}),
        ("done", {}),
    )


def main() -> int:
    from app.config import settings as app_settings
    from app.security import hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-ai-theme-ui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)

    con = sqlite3.connect(str(db_copy))
    row = con.execute("select id, username from users where role = 1 order by id limit 1").fetchone()
    if not row:
        raise SystemExit("副本库里没有管理员")
    admin_id, admin_name = row
    con.execute("update users set password_hash=? where id=?", (hash_password(PASSWORD), admin_id))
    con.execute(
        "update user_settings set accent_color=?, accent_style=? where user_id=?",
        ("#c084fc", json.dumps({"mode": "solid", "colors": ["#c084fc"]}), admin_id),
    )
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

    scenario = {
        "body": tool_done(
            {"accentColor": "#60a5fa", "accentStyle": {"mode": "solid", "colors": ["#60a5fa"]}},
            "已把主题色改成 #60a5fa（界面已经生效，不满意可以让我再换）。",
        )
    }

    def on_chat(route):
        route.fulfill(
            status=200, headers={"content-type": "text/event-stream"}, body=scenario["body"]
        )

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

            print("\n[A] 对话设置里的新开关")
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector("[data-settings-tab]", timeout=20000)
            page.click('[data-settings-tab="mascot"]')
            page.wait_for_selector('[data-ai-flag="themeColor"]', state="attached", timeout=20000)
            toggle = page.locator('[data-ai-flag="themeColor"]')
            check("开关存在", toggle.count() == 1)
            check("默认是关的", not toggle.is_checked())
            label = page.locator("label.ai-line", has=toggle)
            check("标题写的是「修改界面主题色」", "修改界面主题色" in label.inner_text(), label.inner_text()[:40])
            check("说明里提到背景图取色", "背景图" in label.inner_text() and "主题色" in label.inner_text())
            label.scroll_into_view_if_needed()
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT / "ai_theme_toggle.png"), full_page=False)

            label.locator(".switch").click()
            page.wait_for_timeout(900)
            status, data = http(base, "GET", "/api/ai/config", None, cookie)
            cfg = (data or {}).get("config") or {}
            check("点开后就落库了", status == 200 and cfg.get("themeColor") is True, json.dumps(cfg)[:80])
            con = sqlite3.connect(str(db_copy))
            stored = con.execute(
                "select ai_config from user_settings where user_id=?", (admin_id,)
            ).fetchone()
            con.close()
            raw = json.loads(stored[0]) if stored and stored[0] else {}
            check("库里 ai_config 有 themeColor", raw.get("themeColor") is True, json.dumps(raw)[:80])

            print("\n[B] 对话里换成单色")
            page.route("**/api/ai/config", lambda route: route.fulfill(
                status=200, headers={"content-type": "application/json"}, body=json.dumps(CONFIG)
            ))
            page.route("**/api/ai/chat", on_chat)

            page.click('[data-settings-tab="appearance"]')
            page.wait_for_selector("[data-accent-hex]", timeout=20000)
            check("改之前主色是樱紫", page.input_value("[data-accent-hex]") == "#c084fc",
                  page.input_value("[data-accent-hex]"))
            accent_before = page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--accent-user').trim()"
            )

            page.wait_for_selector('[data-mascot-act="chat"]', timeout=20000)
            page.wait_for_function(
                "document.getElementById('mascot-actions')?.dataset.bound === '1'", timeout=20000
            )
            page.eval_on_selector('[data-mascot-act="chat"]', "el => el.click()")
            page.wait_for_selector("#mascot-chat", timeout=20000)
            page.fill("[data-mchat-input]", "主题色换成天青蓝")
            page.press("[data-mchat-input]", "Enter")
            page.wait_for_selector(".mchat__msg.is-bot .mchat__bubble:not(.is-streaming)", timeout=20000)

            accent_after = page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--accent-user').trim()"
            )
            check("界面主色立刻变了", accent_after == "#60a5fa" and accent_before != accent_after,
                  f"{accent_before} → {accent_after}")
            # 工具列表是折叠的 details，inner_text 拿不到里面，直接读节点文字
            tool_label = page.evaluate(
                "document.querySelector('.mchat__tool [data-tool-label]')?.textContent || ''"
            )
            check("工具条目记了一笔", "改主题色" in tool_label, tool_label)
            check("toast 提示了颜色", "#60a5fa" in page.locator(".toast").first.inner_text(),
                  page.locator(".toast").first.inner_text()[:60])
            check("没写进渐变变量",
                  not page.evaluate("document.documentElement.style.getPropertyValue('--accent-bar')"))

            print("\n[C] 设置页的取色控件跟上了")
            check("色值框同步", page.input_value("[data-accent-hex]") == "#60a5fa",
                  page.input_value("[data-accent-hex]"))
            check("渐变面板还藏着", page.locator("[data-accent-gradient]").is_hidden())
            cached = json.loads(page.evaluate("localStorage.getItem('cn:appearance')") or "{}")
            check("本地缓存也换了色", cached.get("accentColor") == "#60a5fa", str(cached.get("accentColor")))

            print("\n[D] 再换成渐变")
            scenario["body"] = tool_done(
                {"accentColor": "#60a5fa", "accentStyle": {"mode": "gradient", "colors": ["#60a5fa", "#34d399"]}},
                "已把主题色改成渐变：#60a5fa → #34d399（界面已经生效，不满意可以让我再换）。",
            )
            page.fill("[data-mchat-input]", "改成天青到薄荷的渐变")
            page.press("[data-mchat-input]", "Enter")
            page.wait_for_function(
                "document.querySelectorAll('.mchat__msg.is-bot .mchat__bubble').length >= 2",
                timeout=20000,
            )
            page.wait_for_timeout(600)
            bar = page.evaluate(
                "document.documentElement.style.getPropertyValue('--accent-bar')"
            )
            check("渐变进度条变量生效", "linear-gradient" in bar and "#34d399" in bar, bar[:70])
            check("渐变面板切出来了", page.locator("[data-accent-gradient]").is_visible())
            page.wait_for_selector("[data-accent-stop-hex]", state="attached", timeout=10000)
            stops = page.locator("[data-accent-stop-hex]")
            check("面板里两个色标", stops.count() == 2, str(stops.count()))
            check("色标跟对话里的颜色一致",
                  page.input_value('[data-accent-stop-hex="0"]') == "#60a5fa"
                  and page.input_value('[data-accent-stop-hex="1"]') == "#34d399",
                  page.input_value('[data-accent-stop-hex="0"]') + "," + page.input_value('[data-accent-stop-hex="1"]'))
            check("模式按钮切到渐变",
                  "is-active" in (page.locator('[data-accent-mode-btn="gradient"]').get_attribute("class") or ""))
            page.screenshot(path=str(OUT / "ai_theme_gradient.png"), full_page=False)

            print("\n[E] 390px：新开关那一行别撑破卡片")
            # 对话面板先收起来，不然手机宽下它整屏盖住设置页
            page.eval_on_selector("[data-mchat-close]", "el => el.click()")
            page.wait_for_timeout(300)
            page.set_viewport_size({"width": 390, "height": 780})
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector("[data-settings-tab]", timeout=20000)
            page.click('[data-settings-tab="mascot"]')
            page.wait_for_selector('[data-ai-flag="themeColor"]', state="attached", timeout=20000)
            box = page.locator("label.ai-line", has=page.locator('[data-ai-flag="themeColor"]')).bounding_box()
            check("这一行在视口内", bool(box) and box["x"] >= 0 and box["x"] + box["width"] <= 391, str(box))
            overflow = page.evaluate(
                "(() => { const v = document.querySelector('.view-body'); return v ? v.scrollWidth - v.clientWidth : -1; })()"
            )
            check("没有横向滚动", overflow <= 1, str(overflow))
            page.locator("label.ai-line", has=page.locator('[data-ai-flag="themeColor"]')).scroll_into_view_if_needed()
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT / "ai_theme_mobile.png"), full_page=False)

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
