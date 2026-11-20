# -*- coding: utf-8 -*-
"""「批量移动时目标列表里少了一个文件夹」的界面回归。

用户报的问题：
  选中 文件夹1 / 文件夹2 / 文件夹3 → 点「移动」→ 目标列表里只剩 2、3，1 不见了；
  只选 2 和 3 时同理，2 不见。
根因：``moveItems`` 只把「第一项」的 id 作为 ``excludeId`` 传给目录选择器，选择器又把它
从列表里**过滤掉**。列表是「选目标目录」用的，缺一行看起来就像丢了文件。

现在的行为：正在移动的文件夹照样列出来，但置灰 + 标「正在移动」，不能点进去。

    ./.venv/Scripts/python.exe scripts/move_picker_ui.py
"""

from __future__ import annotations

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
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

PASS = FAIL = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


RUNNER = r"""
import os, sys
sys.path.insert(0, os.environ["SMOKE_ROOT"])
from app import captcha
captcha.verify = lambda *a, **k: (True, "")
import app.main as m
import uvicorn
uvicorn.run(m.app, host="127.0.0.1", port=int(os.environ["SMOKE_PORT"]), log_level="warning")
"""


def wait_http(base: str, proc: subprocess.Popen) -> bool:
    for _ in range(120):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"{base}/api/meta", timeout=5) as res:
                if res.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


ROWS_JS = """
() => {
  const picker = document.querySelector(".picker");
  if (!picker) return null;
  return [...picker.querySelectorAll(".picker__row")].map((row) => ({
    name: row.dataset.name,
    disabled: row.disabled,
    blocked: row.classList.contains("is-blocked"),
    tag: row.querySelector(".picker__tag")?.textContent?.trim() || "",
    chev: Boolean(row.querySelector(".icon--chev")),
  }));
}
"""


def main() -> int:
    from playwright.sync_api import sync_playwright

    work = Path(tempfile.mkdtemp(prefix="cloudnetp-move-"))
    db_path = work / "test.db"
    shutil.copy(ROOT / "dev.db", db_path)
    port = free_port()
    base = f"http://localhost:{port}"
    runner = work / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")

    env = {
        **os.environ,
        "SMOKE_ROOT": str(ROOT),
        "SMOKE_PORT": str(port),
        "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "STORAGE_DIR": str(work / "storage"),
        "SECRET_KEY": "smoke-secret-key-for-move",
        "DB_FALLBACK_ENABLED": "false",
        "MAIL_ENABLED": "false",
    }
    log_file = open(work / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(VENV_PY), str(runner)], cwd=str(ROOT), env=env,
        stdout=log_file, stderr=subprocess.STDOUT,
    )

    try:
        if not wait_http(base, proc):
            print((work / "server.log").read_text(encoding="utf-8")[-3000:])
            return 1

        stamp = str(int(time.time()))[-6:]
        username = f"mv{stamp}"
        password = "abc12345"

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            r = ctx.request.post(
                f"{base}/api/auth/register",
                data={
                    "username": username,
                    "password": password,
                    "captchaId": "",
                    "captcha": "0000",
                    "agree": True,
                },
            )
            check("注册", r.ok, str(r.status))
            r = ctx.request.post(
                f"{base}/api/auth/login",
                data={"username": username, "password": password, "captchaId": "", "captcha": "0000"},
            )
            check("登录", r.ok, str(r.status))

            def mkdir(name: str, parent: int | None = None) -> int:
                resp = ctx.request.post(
                    f"{base}/api/nodes/folder", data={"name": name, "parentId": parent}
                )
                assert resp.ok, resp.text()
                return int(resp.json()["node"]["id"])

            def children(parent: int | None = None) -> list[str]:
                url = f"{base}/api/nodes?sort=name&order=asc"
                if parent is not None:
                    url += f"&parentId={parent}"
                resp = ctx.request.get(url)
                assert resp.ok, resp.text()
                return [item["name"] for item in resp.json().get("items", [])]

            f1 = mkdir("文件夹1")
            f2 = mkdir("文件夹2")
            f3 = mkdir("文件夹3")
            target = mkdir("目标")
            check("建好 4 个文件夹", children() == ["文件夹1", "文件夹2", "文件夹3", "目标"], str(children()))

            page = ctx.new_page()
            page.add_init_script(
                "document.addEventListener('DOMContentLoaded', () => {"
                "  const s = document.createElement('style');"
                "  s.textContent = '#mascot{display:none!important}"
                "*{animation-duration:0s!important;transition-duration:0s!important}';"
                "  document.head.appendChild(s);"
                "});"
            )
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            def load_files() -> None:
                page.goto(f"{base}/#/files", wait_until="domcontentloaded")
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector('.view-body [data-id]', timeout=20000)
                page.wait_for_timeout(400)

            def tile(name: str):
                return page.locator(f'.view-body [data-id][title="{name}"], .view-body [data-id]').filter(
                    has_text=name
                ).first

            def click_tile(name: str, modifier: str | None = None) -> None:
                loc = tile(name)
                if modifier:
                    loc.click(modifiers=[modifier])
                else:
                    loc.click()
                page.wait_for_timeout(120)

            def open_picker() -> list[dict]:
                page.click('[data-act="sel-move"]')
                page.wait_for_selector(".picker .picker__row", timeout=10000)
                page.wait_for_timeout(300)
                rows = page.evaluate(ROWS_JS)
                return rows or []

            def close_picker() -> None:
                page.click('.picker [data-act="cancel"]')
                page.wait_for_timeout(200)
                page.click('[data-act="sel-clear"]')
                page.wait_for_timeout(150)

            # ---------------- 1. 选 1/2/3：列表里必须还是 4 个 ----------------
            load_files()
            click_tile("文件夹1")
            click_tile("文件夹2", "Control")
            click_tile("文件夹3", "Control")
            check("选中 3 项", page.locator(".is-selected").count() == 3,
                  str(page.locator(".is-selected").count()))

            rows = open_picker()
            names = [row["name"] for row in rows]
            check("目标列表一个都不少（4 行都在）",
                  names == ["文件夹1", "文件夹2", "文件夹3", "目标"], str(names))
            check("被移动的 3 个置灰", sum(1 for row in rows if row["blocked"]) == 3,
                  str([(row["name"], row["blocked"]) for row in rows]))
            check("被移动的项不可点", all(row["disabled"] for row in rows if row["blocked"]))
            check("被移动的项标了「正在移动」",
                  all(row["tag"] == "正在移动" for row in rows if row["blocked"]),
                  str([row["tag"] for row in rows]))
            check("没被移动的目录照常可点",
                  [(row["name"], row["disabled"], row["chev"]) for row in rows
                   if not row["blocked"]] == [("目标", False, True)],
                  str([(row["name"], row["disabled"], row["chev"]) for row in rows
                       if not row["blocked"]]))
            page.screenshot(path=str(OUT / "move-picker-all.png"))

            # 点置灰行不该进到那层目录去
            before = page.locator(".picker__crumb button").count()
            page.click('.picker .picker__row[disabled]', force=True)
            page.wait_for_timeout(300)
            check("点置灰行不会进入该目录",
                  page.locator(".picker__crumb button").count() == before)
            close_picker()

            # ---------------- 2. 全选 4 个：全置灰也不清空列表 ----------------
            load_files()
            page.keyboard.press("Control+a")
            page.wait_for_timeout(200)
            check("全选 4 项", page.locator(".is-selected").count() == 4,
                  str(page.locator(".is-selected").count()))
            rows = open_picker()
            names = [row["name"] for row in rows]
            check("全选后列表仍然是 4 行（不是一句空提示）",
                  names == ["文件夹1", "文件夹2", "文件夹3", "目标"], str(names))
            check("全选时 4 行都置灰", all(row["blocked"] for row in rows),
                  str([(row["name"], row["blocked"]) for row in rows]))
            check("全选时仍可「移到这里」", page.is_enabled('.picker [data-act="ok"]'))
            close_picker()

            # ---------------- 3. 只选 2 和 3：2 必须留在列表里，再真移进 1 ----------------
            load_files()
            click_tile("文件夹2")
            click_tile("文件夹3", "Control")
            check("选中了 2 项", page.locator(".is-selected").count() == 2,
                  str(page.locator(".is-selected").count()))
            rows = open_picker()
            names = [row["name"] for row in rows]
            check("只选 2 项时列表仍完整", names == ["文件夹1", "文件夹2", "文件夹3", "目标"], str(names))
            blocked = sorted(row["name"] for row in rows if row["blocked"])
            check("只有选中的 2 项置灰", blocked == ["文件夹2", "文件夹3"], str(blocked))

            page.locator('.picker .picker__row[data-name="文件夹1"]').click()
            page.wait_for_timeout(350)
            page.click('.picker [data-act="ok"]')
            page.wait_for_selector(".task-panel", timeout=10000)
            page.wait_for_selector(".task-panel", state="detached", timeout=30000)
            page.wait_for_timeout(500)
            check("根目录只剩 文件夹1 和 目标",
                  children() == ["文件夹1", "目标"], str(children()))
            check("文件夹1 里正好是 2 和 3",
                  children(f1) == ["文件夹2", "文件夹3"], str(children(f1)))
            check("移动后列表也刷新了", "文件夹2" not in children(), str(children()))

            # ---------------- 4. 进子目录再移：导航没被置灰逻辑弄坏 ----------------
            load_files()
            click_tile("目标")
            open_picker()
            page.locator('.picker .picker__row[data-name="文件夹1"]').click()
            page.wait_for_timeout(400)
            check("能进入子目录", page.locator(".picker__crumb button").count() == 2)
            rows = page.evaluate(ROWS_JS) or []
            check("子目录里的文件夹照常列出",
                  [row["name"] for row in rows] == ["文件夹2", "文件夹3"], str([row["name"] for row in rows]))
            page.click('.picker [data-act="ok"]')
            page.wait_for_selector(".task-panel", timeout=10000)
            page.wait_for_selector(".task-panel", state="detached", timeout=30000)
            page.wait_for_timeout(500)
            check("目标被移进了 文件夹1",
                  children(f1) == ["文件夹2", "文件夹3", "目标"], str(children(f1)))
            check("根目录只剩 文件夹1", children() == ["文件夹1"], str(children()))

            check("页面没有 JS 报错", not errors, str(errors[:3]))
            browser.close()

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        print(f"\n服务日志：{work / 'server.log'}")
        print(f"截图：{OUT / 'move-picker-all.png'}")

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
