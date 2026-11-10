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
        "tools": False,
        "protocol": "openai",
        "maxInput": 0,
        "maxOutput": 0,
        "maxRounds": 8,
        "presets": [],
    },
    "isAdmin": False,
    "systemPrompt": "",
}

SSE_OK = (
    'event: meta\ndata: {"model":"fake-model"}\n\n'
    'event: delta\ndata: {"text":"好的，收到"}\n\n'
    "event: done\ndata: {}\n\n"
)


def main() -> None:
    from app.config import settings
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-mchat-ui-")).resolve()
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

    scenario = {"fail": True, "calls": []}

    def on_chat(route):
        try:
            scenario["calls"].append(route.request.post_data_json or {})
        except Exception:
            scenario["calls"].append({})
        if scenario["fail"]:
            route.fulfill(
                status=500,
                headers={"content-type": "application/json"},
                body=json.dumps({"detail": "上游冒烟失败"}),
            )
            return
        route.fulfill(status=200, headers={"content-type": "text/event-stream"}, body=SSE_OK)

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来，看日志：" + str(log_path))

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)

            ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
            ctx.add_cookies([{"name": "cloudnetp_session", "value": cookie, "url": base}])
            page = ctx.new_page()
            errors: list[str] = []

            def note(kind: str, text: str) -> None:
                # 自己造的 500 也会被 Chrome 记成资源错误，过滤掉
                if "Failed to load resource" in text or "status of 500" in text:
                    return
                if kind == "error":
                    errors.append(text)

            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", lambda msg: note(msg.type, msg.text))
            page.route("**/api/ai/config", lambda route: route.fulfill(
                status=200, headers={"content-type": "application/json"}, body=json.dumps(CONFIG)
            ))
            page.route("**/api/ai/chat", on_chat)

            print("\n[A] 发送失败：左边亮出「重新发送」")
            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_selector('[data-mascot-act="chat"]', timeout=20000)
            # 按钮是静态 HTML，监听器要等 app 装配完才挂上：等到绑定标记出现再点
            page.wait_for_function(
                "document.getElementById('mascot-actions')?.dataset.bound === '1'", timeout=20000
            )
            page.eval_on_selector('[data-mascot-act="chat"]', "el => el.click()")
            page.wait_for_selector("#mascot-chat", timeout=20000)
            check("对话面板打开", page.locator("#mascot-chat").is_visible())
            page.fill("[data-mchat-input]", "第一句话")
            page.press("[data-mchat-input]", "Enter")
            page.wait_for_selector(".mchat__retry:not([hidden])", timeout=15000)
            check("用户消息渲染出来了", page.locator(".mchat__msg.is-me").count() == 1)
            retry = page.locator(".mchat__retry").first
            check("重发按钮可见", retry.is_visible())
            check("按钮上没有任何文字", retry.inner_text().strip() == "", repr(retry.inner_text()))
            check("只有图标，无障碍名还在", retry.get_attribute("aria-label") == "重新发送",
                  str(retry.get_attribute("aria-label")))
            size = retry.bounding_box()
            check("是 26px 的圆形按钮", bool(size and abs(size["width"] - 26) < 1.5 and abs(size["height"] - 26) < 1.5),
                  str(size))
            check("空回答的泡泡被收掉", page.locator(".mchat__msg.is-bot").count() == 0)
            notice = page.locator(".mchat__notice").first
            check("错误提示带上了原因", "上游冒烟失败" in notice.inner_text(), notice.inner_text())
            check(
                "用户消息标了失败态",
                "is-failed" in (page.locator(".mchat__msg.is-me").first.get_attribute("class") or ""),
            )
            rb = retry.bounding_box()
            bb = page.locator(".mchat__msg.is-me .mchat__bubble").first.bounding_box()
            check(
                "按钮在消息左边",
                bool(rb and bb and rb["x"] + rb["width"] <= bb["x"] + 1),
                f"{rb} | {bb}",
            )
            page.screenshot(path=str(OUT / "mchat_failed.png"), full_page=False)

            print("\n[B] 重新发送：成功一次")
            scenario["fail"] = False
            before = len(scenario["calls"])
            page.click(".mchat__retry")
            page.wait_for_selector(".mchat__msg.is-bot .mchat__bubble:not(.is-streaming)", timeout=15000)
            page.wait_for_timeout(500)
            check("这次真的发了一次请求", len(scenario["calls"]) == before + 1, str(len(scenario["calls"])))
            bubble = page.locator(".mchat__msg.is-bot .mchat__bubble").first
            check("回答出来了", "好的" in bubble.inner_text(), bubble.inner_text()[:30])
            check("重新发送按钮收起", page.locator(".mchat__retry").first.is_hidden())
            check("失败样式去掉", "is-failed" not in (page.locator(".mchat__msg.is-me").first.get_attribute("class") or ""))
            check("错误提示已清掉", page.locator(".mchat__notice").count() == 0)
            sent = scenario["calls"][-1].get("messages") or []
            check(
                "重发的是原来那句",
                sent and sent[-1].get("content") == "第一句话",
                json.dumps(sent, ensure_ascii=False)[:120],
            )

            print("\n[C] 编辑：内容回到输入框")
            me = page.locator(".mchat__msg.is-me").first
            me.hover()
            page.wait_for_timeout(200)
            check("气泡下有编辑 / 删除", me.locator("[data-act=edit]").count() == 1 and me.locator("[data-act=del]").count() == 1)
            bot_ops = page.locator(".mchat__msg.is-bot").first.locator("[data-act=del]")
            check("回答下面也有删除", bot_ops.count() == 1)
            me.locator("[data-act=edit]").click()
            page.wait_for_timeout(300)
            check("出现编辑提示条", page.locator("[data-mchat-editing]").is_visible())
            check("内容填回输入框", page.input_value("[data-mchat-input]") == "第一句话")
            check("原消息标成编辑中", "is-editing" in (me.get_attribute("class") or ""))
            page.screenshot(path=str(OUT / "mchat_editing.png"), full_page=False)
            page.click("[data-mchat-edit-cancel]")
            page.wait_for_timeout(200)
            check("取消后输入框清空", page.input_value("[data-mchat-input]") == "")
            check("取消后提示条收起", page.locator("[data-mchat-editing]").is_hidden())
            check("取消不动原消息", page.locator(".mchat__msg.is-me").count() == 1)

            print("\n[D] 改完重发：替换掉旧问答")
            me.hover()
            me.locator("[data-act=edit]").click()
            page.fill("[data-mchat-input]", "改过的第二句话")
            page.press("[data-mchat-input]", "Enter")
            page.wait_for_timeout(900)
            check("旧问答被替换", page.locator(".mchat__msg.is-me").count() == 1)
            new_text = page.locator(".mchat__msg.is-me .mchat__bubble").first.inner_text()
            check("发出的是改后的内容", "改过的第二句话" in new_text, new_text)
            check("编辑提示条收起了", page.locator("[data-mchat-editing]").is_hidden())
            check("回答只有一条", page.locator(".mchat__msg.is-bot").count() == 1)

            print("\n[E] 删除：按两下才删")
            me = page.locator(".mchat__msg.is-me").first
            me.hover()
            me.locator("[data-act=del]").click()
            page.wait_for_timeout(200)
            check("第一下变成「确认删除」", me.locator("[data-act=del]").inner_text().strip() == "确认删除",
                  me.locator("[data-act=del]").inner_text().strip())
            page.locator(".mchat__msg.is-me [data-act=del]").first.click()
            page.wait_for_timeout(400)
            check("消息和它的回答一起删掉", page.locator(".mchat__msg").count() == 0)
            check("有删除提示", page.locator(".toast").count() > 0)

            print("\n[F] 删掉回答后再发：不出现连续同角色")
            page.fill("[data-mchat-input]", "第三条")
            page.press("[data-mchat-input]", "Enter")
            page.wait_for_selector(".mchat__msg.is-bot .mchat__bubble:not(.is-streaming)", timeout=15000)
            page.wait_for_timeout(400)
            bot = page.locator(".mchat__msg.is-bot").first
            bot.hover()
            bot.locator("[data-act=del]").click()
            page.wait_for_timeout(150)
            page.locator(".mchat__msg.is-bot [data-act=del]").first.click()
            page.wait_for_timeout(400)
            check("回答删掉了、提问还在", page.locator(".mchat__msg.is-bot").count() == 0
                  and page.locator(".mchat__msg.is-me").count() == 1)
            page.fill("[data-mchat-input]", "第四条")
            page.press("[data-mchat-input]", "Enter")
            page.wait_for_timeout(900)
            roles = [m.get("role") for m in (scenario["calls"][-1].get("messages") or [])]
            merged = (scenario["calls"][-1].get("messages") or [{}])[-1].get("content") or ""
            check("首条是 user", bool(roles) and roles[0] == "user", str(roles))
            check(
                "没有连续同角色",
                all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)),
                str(roles),
            )
            check(
                "合并后两句都在",
                "第三条" in merged and "第四条" in merged,
                merged.replace("\n", " / "),
            )

            print("\n[G] 375px 窄屏：失败态不溢出")
            page.set_viewport_size({"width": 375, "height": 720})
            page.wait_for_timeout(300)
            scenario["fail"] = True
            page.fill("[data-mchat-input]", "窄屏下再发一句看看会不会把按钮挤出去")
            page.press("[data-mchat-input]", "Enter")
            page.wait_for_selector(".mchat__retry:not([hidden])", timeout=15000)
            page.wait_for_timeout(400)
            overflow = page.evaluate(
                """() => {
                    const panel = document.querySelector('#mascot-chat');
                    const feed = document.querySelector('[data-mchat-feed]');
                    return {
                        panel: panel.scrollWidth - panel.clientWidth,
                        feed: feed.scrollWidth - feed.clientWidth,
                    };
                }"""
            )
            check("面板没有横向滚动", overflow["panel"] <= 1, str(overflow))
            check("消息区没有横向滚动", overflow["feed"] <= 1, str(overflow))
            rb = page.locator(".mchat__retry").last.bounding_box()
            check("按钮还在可视范围内", bool(rb and rb["x"] >= 0), str(rb))
            page.screenshot(path=str(OUT / "mchat_failed_mobile.png"), full_page=False)

            check("全程没有 JS 报错", not errors, str(errors[:2]))
            ctx.close()
            browser.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            pass
        log_file.close()

    print(f"\n通过 {passed} 项，失败 {failed} 项")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
