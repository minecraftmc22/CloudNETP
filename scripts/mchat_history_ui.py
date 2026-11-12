# -*- coding: utf-8 -*-
"""对话面板「历史对话 / 长期记忆 / 上下文圆环 / ask 问答工具」的 UI 冒烟。

拦截 /api/ai/*，在 Python 里模拟一个有状态的后端（会话、消息、记忆都存内存），
不连真实模型、不碰真实库（用 dev.db 的副本启服务）。

  A 首次打开是问候页；发一句话 → 自动 POST 建会话并落消息
  B 刷新页面再打开 → 自动恢复刚才那段对话
  C 上下文圆环：done.usage.input=240 / maxInput=4096 → 显示；调到 300 → 警示色
  D 历史抽屉能看到会话；「新对话」清掉当前；点列表项又能切回来
  E ask 事件 → 问题气泡 + 选项胶囊 + 自定义；点选后答案作为用户消息发出
  F 记忆页签：渲染 / 手动添加 / 从对话提炼（自动条目）
  G 删光消息会删掉会话；375px 窄屏抽屉不溢出；无 JS 报错

    ./.venv/Scripts/python.exe scripts/mchat_history_ui.py
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
from datetime import datetime, timezone
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
    "tools": True,
    "protocol": "openai",
    "maxInput": 4096,
    "maxOutput": 0,
    "maxRounds": 8,
    "showTokens": True,
    "saveHistory": True,
    "memory": True,
    "source": "user",
    "aiEnabled": True,
    "presets": [],
}

SSE_TEXT = (
    'event: meta\ndata: {"model":"fake-model","usage":true}\n\n'
    'event: delta\ndata: {"text":"你好呀"}\n\n'
    'event: done\ndata: {"usage":{"input":240,"output":66,"total":306},"hadText":true}\n\n'
)

SSE_ASK = (
    'event: meta\ndata: {"model":"fake-model","usage":true}\n\n'
    'event: ask\ndata: {"question":"要整理哪个文件夹？","options":["全部整理","只整理照片"]}\n\n'
    'event: done\ndata: {"usage":{"input":120,"output":18,"total":138},"hadText":false}\n\n'
)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    from app.config import settings
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-mchat-history-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    row = con.execute("select id, token_version from users where role = 1 order by id limit 1").fetchone()
    if not row:
        raise SystemExit("库里没有管理员，先手动建一个再来跑这个脚本")
    con.execute("update users set password_hash=? where id=?", (hash_password(PASSWORD), row[0]))
    # 后台里可能存过覆盖配置，副本里删掉，免得影响接口配置（记忆/历史是前端拦截的，这里只求稳）
    con.execute("delete from system_settings where k in ('ai_default_config','ai_system_prompt')")
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

    live = {"config": dict(BASE_CONFIG), "sse": SSE_TEXT}
    # 假后端的状态
    store = {
        "next_id": 1,
        "sessions": {},  # id -> {title, messages}
        "memories": [{"text": "文件按日期命名", "source": "custom", "createdAt": now_iso()}],
        "chat_bodies": [],
        "created": 0,
        "replaced": 0,
        "deleted": [],
        "generated": 0,
    }

    def jsonify(route, obj: dict) -> None:
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(obj, ensure_ascii=False),
        )

    def sessions_route(route) -> None:
        req = route.request
        path = req.url.split("?")[0].split("/api/ai/sessions")[-1].strip("/")
        if req.method == "GET" and not path:
            return jsonify(route, {"sessions": [
                {"id": sid, "title": s["title"], "updatedAt": now_iso(), "count": len(s["messages"])}
                for sid, s in sorted(store["sessions"].items(), reverse=True)
            ]})
        if req.method == "POST" and not path:
            body = json.loads(req.post_data or "{}")
            messages = body.get("messages") or []
            title = next(
                (m["content"].strip().replace("\n", " ")[:40] for m in messages
                 if m.get("role") == "user" and (m.get("content") or "").strip()),
                "新对话",
            )
            sid = store["next_id"]
            store["next_id"] += 1
            store["sessions"][sid] = {"title": title, "messages": messages}
            store["created"] += 1
            return jsonify(route, {"id": sid, "title": title, "count": len(messages)})
        sid = int(path) if path.isdigit() else 0
        session = store["sessions"].get(sid)
        if session is None:
            return jsonify(route, {"detail": "没有这段对话"})
        if req.method == "GET":
            return jsonify(route, {"id": sid, "title": session["title"], "messages": session["messages"]})
        if req.method == "PUT":
            body = json.loads(req.post_data or "{}")
            session["messages"] = body.get("messages") or []
            store["replaced"] += 1
            return jsonify(route, {"id": sid, "title": session["title"], "count": len(session["messages"])})
        if req.method == "DELETE":
            store["sessions"].pop(sid, None)
            store["deleted"].append(sid)
            return jsonify(route, {"ok": True})
        return jsonify(route, {"ok": True})

    def memories_route(route) -> None:
        req = route.request
        if req.method == "GET":
            return jsonify(route, {"memories": store["memories"], "enabled": True})
        if req.method == "PUT":
            body = json.loads(req.post_data or "{}")
            store["memories"] = [
                {"text": m["text"], "source": m.get("source", "custom"), "createdAt": now_iso()}
                for m in (body.get("memories") or [])
            ]
            return jsonify(route, {"memories": store["memories"]})
        if req.method == "POST":  # generate
            store["generated"] += 1
            store["memories"].append(
                {"text": "用户喜欢一次性整理所有目录", "source": "auto", "createdAt": now_iso()}
            )
            return jsonify(route, {"memories": store["memories"], "added": 1})
        return jsonify(route, {"ok": True})

    def open_chat(page) -> None:
        page.wait_for_function(
            "document.getElementById('mascot-actions')?.dataset.bound === '1'", timeout=20000
        )
        page.eval_on_selector('[data-mascot-act="chat"]', "el => el.click()")
        page.wait_for_selector("#mascot-chat.is-open", timeout=20000)

    def ask(page, text: str) -> None:
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
                lambda route: jsonify(route, {"config": live["config"], "isAdmin": False, "systemPrompt": ""}),
            )
            page.route(
                "**/api/ai/chat",
                lambda route: (
                    store["chat_bodies"].append(json.loads(route.request.post_data or "{}")),
                    route.fulfill(
                        status=200,
                        headers={"content-type": "text/event-stream"},
                        body=live["sse"],
                    ),
                )[-1],
            )
            page.route("**/api/ai/sessions**", sessions_route)
            page.route("**/api/ai/memories**", memories_route)

            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            # 抑制「聊够 6 条自动提炼记忆」：那条链路在 ai_history_smoke.py 里验证，
            # 这里只测手动添加 / 手动提炼，免得后台请求改掉列表长度
            page.evaluate("localStorage.setItem('mchat.memgen', String(Date.now()))")
            open_chat(page)
            print("\n[A] 首次打开 + 自动保存")
            check("问候页出现", page.locator(".mchat__hello").count() == 1)
            ask(page, "帮我看看最近上传了什么")
            page.wait_for_timeout(1200)  # 600ms 防抖 + 请求往返
            check("自动创建了一段会话", store["created"] == 1, str(store["created"]))
            saved = next(iter(store["sessions"].values()))["messages"] if store["sessions"] else []
            check("落库是 2 条消息", len(saved) == 2 and saved[0]["content"] == "帮我看看最近上传了什么", str(saved)[:120])
            check("标题由第一句提问生成", next(iter(store["sessions"].values()))["title"] == "帮我看看最近上传了什么")

            print("\n[B] 刷新页面自动恢复")
            page.reload(wait_until="domcontentloaded")
            open_chat(page)
            page.wait_for_timeout(300)
            check("恢复出两条消息", page.locator(".mchat__msg").count() == 2, str(page.locator(".mchat__msg").count()))
            check("恢复的是原内容", "帮我看看最近上传了什么" in page.locator(".mchat__msg").first.inner_text())
            check("不再显示问候页", page.locator(".mchat__hello").count() == 0)

            print("\n[C] 上下文占用圆环")
            ring = page.locator("[data-mchat-ring]")
            check("恢复历史后就有圆环", ring.is_visible())
            ask(page, "算一下上下文")
            page.wait_for_timeout(350)
            title = ring.get_attribute("title") or ""
            check("tooltip 带真实用量明细", "240" in title and "4.1k" in title, title)
            live["config"] = dict(BASE_CONFIG, maxInput=300)
            page.evaluate("window.dispatchEvent(new CustomEvent('ai:config-changed'))")
            page.wait_for_timeout(350)
            ask(page, "再算一次")
            page.wait_for_timeout(500)
            check("超过 80% 转警示色", "is-warn" in (ring.get_attribute("class") or ""), ring.get_attribute("class") or "")
            live["config"] = dict(BASE_CONFIG)
            page.evaluate("window.dispatchEvent(new CustomEvent('ai:config-changed'))")
            page.wait_for_timeout(300)
            page.screenshot(path=str(OUT / "mchat_history_ring.png"), full_page=False)

            print("\n[D] 历史抽屉")
            page.click("[data-mchat-history]")
            page.wait_for_selector("[data-mchat-history-panel]:not([hidden])")
            page.wait_for_timeout(300)
            check("抽屉列出会话", page.locator(".mchat__history-item").count() >= 1)
            check("条目显示标题", "帮我看看最近上传了什么" in page.locator(".mchat__history-item").first.inner_text())
            page.click("[data-mchat-new]")
            page.wait_for_timeout(250)
            check("新对话回到问候页", page.locator(".mchat__hello").count() == 1)
            check("新对话清掉当前会话 id", page.evaluate("localStorage.getItem('mchat.session')") is None)
            page.click("[data-mchat-history]")
            page.wait_for_timeout(300)
            page.locator(".mchat__history-item").first.click()
            page.wait_for_timeout(400)
            check("点条目切回旧对话", page.locator(".mchat__msg").count() >= 2, str(page.locator(".mchat__msg").count()))

            print("\n[E] ask 问答工具")
            live["sse"] = SSE_ASK
            page.fill("[data-mchat-input]", "帮我整理文件")
            page.press("[data-mchat-input]", "Enter")
            page.wait_for_selector(".mchat__chips--ask", timeout=15000)
            check("问题气泡带问题文本", "要整理哪个文件夹？" in page.locator(".mchat__msg.is-bot").last.inner_text())
            chips = page.locator(".mchat__chips--ask .mchat__chip")
            check("自动生成 2 个候选 + 自定义", chips.count() == 3, str(chips.count()))
            check("最后一个是自定义", "自定义" in chips.last.inner_text())
            live["sse"] = SSE_TEXT
            chips.first.click()
            page.wait_for_timeout(700)
            last_body = store["chat_bodies"][-1] if store["chat_bodies"] else {}
            roles = [(m.get("role"), m.get("content")) for m in last_body.get("messages", [])]
            check("点选后答案作为用户消息发出", roles and roles[-1] == ("user", "全部整理"), str(roles[-3:]))
            check("作答后候选胶囊收走", page.locator(".mchat__chips--ask").count() == 0)

            print("\n[F] 长期记忆页签")
            page.click("[data-mchat-history]")
            page.click('[data-htab="memory"]')
            page.wait_for_timeout(350)
            check("渲染已有记忆", page.locator(".mchat__mem-item").count() == 1)
            check("手动条目标为自定义", "自定义" in page.locator(".mchat__mem-item").first.inner_text())
            page.fill("[data-mchat-mem-input]", "说话别太客套")
            page.click("[data-mchat-mem-add]")
            page.wait_for_timeout(400)
            check("添加后列表变 2 条", page.locator(".mchat__mem-item").count() == 2)
            check("PUT 提交到了假后端", len(store["memories"]) == 2, str(store["memories"]))
            page.click("[data-mchat-mem-gen]")
            page.wait_for_timeout(500)
            check("调用提炼接口", store["generated"] == 1)
            check("自动条目出现在列表", page.locator(".mchat__mem-item").count() == 3 and
                  "自动" in page.locator(".mchat__mem-item").last.inner_text())
            page.screenshot(path=str(OUT / "mchat_memory_panel.png"), full_page=False)
            page.click("[data-mchat-history-close]")

            print("\n[G] 删消息同步 + 窄屏 + 无报错")
            before = len(store["deleted"])
            # 每条删除按钮点两下确认；一直点到一条不剩（最多 20 轮防死循环）
            for _ in range(20):
                button = page.locator(".mchat__msg .mchat__op[data-act='del']").first
                if not button.count():
                    break
                button.click()
                page.wait_for_timeout(100)
                button.click()
                page.wait_for_timeout(220)
            check("消息全部删光", page.locator(".mchat__msg").count() == 0, str(page.locator(".mchat__msg").count()))
            check("删光后删掉服务端会话", len(store["deleted"]) > before or not store["sessions"], str(store["deleted"]))
            page.set_viewport_size({"width": 375, "height": 760})
            page.click("[data-mchat-history]")
            page.wait_for_timeout(250)
            overflow = page.evaluate(
                """() => ({
                    doc: document.documentElement.scrollWidth > window.innerWidth + 1,
                    body: document.querySelector('[data-mchat-history-panel]').scrollWidth >
                          document.querySelector('[data-mchat-history-panel]').clientWidth + 1,
                })"""
            )
            check("窄屏抽屉不横向溢出", not overflow["doc"] and not overflow["body"], str(overflow))
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
