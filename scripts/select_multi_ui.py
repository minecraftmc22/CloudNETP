# -*- coding: utf-8 -*-
"""多选回归：勾选框要能连续勾多个，别点第二个就把第一个顶掉。

以前 `onItemClick` 只认 ctrl/meta 才算「加选」，点勾选框和点行体走同一条路径
→ 勾第二项时 `selected.clear()`，第一个勾就没了（手机上没有 ctrl，更没法多选）。

  [A] 列表视图：连续点两个勾选框 → 两行都选中，「已选 2 项」
  [B] 再点一次第一行的勾选框 → 只取消它，第二行仍选中
  [C] 点行体（不是勾选框）仍是单选，不会累加
  [D] ctrl + 点行体仍是加选（老快捷键没坏）
  [E] 网格视图：单击单选，ctrl + 点在原来的基础上再加一个
  [F] 390px 手机：勾选框常显，连点两个仍是 2 项
  [G] shift 连选跨越整段（勾选框不参与 shift）

    ./.venv/Scripts/python.exe scripts/select_multi_ui.py
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


def http(base: str, method: str, path: str, body=None, cookie: str = ""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Origin", base)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except Exception:
            return exc.code, {"raw": raw[:200].decode("utf-8", "replace")}


def main() -> int:
    from app.config import settings
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-select-ui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)

    con = sqlite3.connect(str(db_copy))
    admin = con.execute(
        "select id, username, token_version from users where role = 1 order by id limit 1"
    ).fetchone()
    if not admin:
        raise SystemExit("库里没有管理员，先手动建一个再来跑这个脚本")
    # 视图模式会被 user_settings 覆盖，固定成列表视图起手，脚本本身不依赖上次选择
    con.execute("update users set password_hash=? where id=?", (hash_password(PASSWORD), admin[0]))
    con.commit()
    con.close()

    cookie = f"cloudnetp_session={create_token(admin[0], admin[2])}"

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

        stamp = str(int(time.time()))
        names = [f"选择样本{index}-{stamp}" for index in range(1, 5)]
        ids = []
        for name in names:
            status, data = http(base, "POST", "/api/nodes/folder", {"parentId": None, "name": name}, cookie)
            if status != 201:
                raise SystemExit(f"建目录失败 {status} {data}")
            ids.append(data["node"]["id"])

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)

            def make_page(width: int = 1440, height: int = 1000):
                ctx = browser.new_context(
                    viewport={"width": width, "height": height}, device_scale_factor=2
                )
                ctx.add_cookies([{"name": "cloudnetp_session", "value": cookie.split("=", 1)[1], "url": base}])
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.on(
                    "console",
                    lambda msg: errors.append(msg.text)
                    if msg.type == "error" and "Failed to load resource" not in msg.text
                    else None,
                )
                return ctx, page, errors

            ctx, page, errors = make_page()
            page.goto(f"{base}/#/files", wait_until="networkidle")
            page.wait_for_selector(".file-grid, .file-list", timeout=15000)

            # 统一切到列表视图（勾选框只在列表里）
            page.click('[data-view="list"]')
            page.wait_for_selector(".file-list .frow:not(.frow--head)", timeout=10000)

            def rows():
                return page.eval_on_selector_all(
                    ".file-list .frow:not(.frow--head)",
                    "els => els.map(el => ({id: Number(el.dataset.id),"
                    " name: el.querySelector('.frow__name').textContent.trim(),"
                    " sel: el.classList.contains('is-selected'),"
                    " box: el.querySelector('input[type=checkbox]').checked}))",
                )

            def count():
                return page.eval_on_selector("[data-sel-count]", "el => Number(el.textContent)")

            def row_of(node_id: int):
                return page.query_selector(f'.file-list .frow[data-id="{node_id}"]')

            def box_of(node_id: int):
                return page.query_selector(f'.file-list .frow[data-id="{node_id}"] .frow__check input')

            # ---- [A] 连续勾两个 ----
            box_of(ids[0]).click()
            page.wait_for_timeout(120)
            step1 = [r for r in rows() if r["id"] == ids[0]][0]
            check("[A1] 点第一个勾选框：该项选中", step1["sel"] and step1["box"])
            box_of(ids[1]).click()
            page.wait_for_timeout(120)
            state = {r["id"]: r for r in rows()}
            check(
                "[A2] 再勾第二个：两个都还在选中（不再互相顶掉）",
                state[ids[0]]["sel"] and state[ids[1]]["sel"],
                f"{state[ids[0]]['sel']}/{state[ids[1]]['sel']}",
            )
            check("[A3] 选中条显示 2 项", count() == 2, f"count={count()}")
            check(
                "[A4] 勾选框自身也是勾上的",
                state[ids[0]]["box"] and state[ids[1]]["box"],
            )
            page.screenshot(path=str(OUT / "select_multi_two.png"))

            # ---- [B] 再点一次取消 ----
            box_of(ids[0]).click()
            page.wait_for_timeout(120)
            state = {r["id"]: r for r in rows()}
            check(
                "[B1] 再点第一个勾选框：只取消它",
                (not state[ids[0]]["sel"]) and state[ids[1]]["sel"],
            )
            check("[B2] 选中条剩 1 项", count() == 1, f"count={count()}")

            # ---- [C] 点行体仍是单选 ----
            row_of(ids[2]).click()
            page.wait_for_timeout(120)
            state = {r["id"]: r for r in rows()}
            check(
                "[C1] 点行体：只选中这一行（其余清掉）",
                state[ids[2]]["sel"] and not state[ids[1]]["sel"] and count() == 1,
                f"count={count()}",
            )

            # ---- [D] ctrl + 点行体 ----
            row_of(ids[3]).click(modifiers=["Control"])
            page.wait_for_timeout(120)
            state = {r["id"]: r for r in rows()}
            check(
                "[D1] ctrl + 点行体：加选，共 2 项",
                state[ids[2]]["sel"] and state[ids[3]]["sel"] and count() == 2,
                f"count={count()}",
            )

            # ---- [G] shift 连选 ----
            page.click('[data-act="sel-clear"]')
            page.wait_for_timeout(80)
            row_of(ids[0]).click()
            row_of(ids[2]).click(modifiers=["Shift"])
            page.wait_for_timeout(120)
            state = {r["id"]: r for r in rows()}
            ordered = [r["id"] for r in rows()]
            lo, hi = sorted((ordered.index(ids[0]), ordered.index(ids[2])))
            expected = set(ordered[lo : hi + 1])
            selected = {r["id"] for r in rows() if r["sel"]}
            check(
                "[G1] shift 连选：整段都选中",
                selected == expected and count() == len(expected),
                f"{sorted(selected)} vs {sorted(expected)}",
            )

            # ---- [E] 网格视图 ----
            page.click('[data-act="sel-clear"]')
            page.click('[data-view="grid"]')
            page.wait_for_selector(".file-grid .tile", timeout=10000)
            page.click(f'.tile[data-id="{ids[0]}"]')
            page.wait_for_timeout(100)
            page.click(f'.tile[data-id="{ids[1]}"]', modifiers=["Control"])
            page.wait_for_timeout(120)
            tiles = page.eval_on_selector_all(
                ".file-grid .tile",
                "els => els.filter(el => el.classList.contains('is-selected')).map(el => Number(el.dataset.id))",
            )
            check(
                "[E1] 网格：单击 + ctrl 点 = 2 项",
                sorted(tiles) == sorted(ids[:2]) and count() == 2,
                f"{tiles} count={count()}",
            )
            page.screenshot(path=str(OUT / "select_multi_grid.png"))

            check("[X] 无 JS 异常", not errors, "; ".join(errors[:3]))
            ctx.close()

            # ---- [F] 手机 ----
            ctx2, page2, errors2 = make_page(390, 844)
            page2.goto(f"{base}/#/files", wait_until="networkidle")
            page2.wait_for_selector(".file-grid, .file-list", timeout=15000)
            page2.click('[data-view="list"]')
            page2.wait_for_selector(".file-list .frow:not(.frow--head)", timeout=10000)
            boxes = page2.query_selector_all(".file-list .frow:not(.frow--head) .frow__check input")
            boxes[0].click()
            page2.wait_for_timeout(120)
            boxes[1].click()
            page2.wait_for_timeout(150)
            mobile_count = page2.eval_on_selector("[data-sel-count]", "el => Number(el.textContent)")
            check("[F1] 390px 连点两个勾选框 = 2 项", mobile_count == 2, f"count={mobile_count}")
            page2.screenshot(path=str(OUT / "select_multi_mobile.png"))
            check("[F2] 手机无 JS 异常", not errors2, "; ".join(errors2[:3]))
            ctx2.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        log_file.close()

    print(f"\n{passed} passed, {failed} failed")
    print(f"screenshots: {OUT}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
