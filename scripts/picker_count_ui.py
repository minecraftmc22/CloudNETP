# -*- coding: utf-8 -*-
"""快传「选择要发送的文件」列表里，文件夹的子项数量显示。

回归点：`/api/nodes` 下发的字段叫 `childCount`，前端曾经读成 `item.count`，
于是所有文件夹都显示「0 项」。这个脚本真的开浏览器点开选择器，把每行右侧
那格文字读出来比对。

  [A] 打开「快速 → 发给用户 → 选择文件」，选择器出现
  [B] 有内容的文件夹显示真实条数（2 项），不是 0
  [C] 空文件夹显示 0 项（这条本来就该是 0）
  [D] 文件行显示的是体积，不是「项」
  [E] 进到文件夹里，子项的计数同样正确
  [F] 控制台无 JS 异常

    ./.venv/Scripts/python.exe scripts/picker_count_ui.py
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


def upload(base: str, cookie: str, name: str, content: bytes, parent_id=None) -> dict:
    status, data = http(
        base, "POST", "/api/uploads/init",
        {"name": name, "size": len(content), "parentId": parent_id}, cookie,
    )
    if status != 200:
        raise SystemExit(f"init 失败 {status} {data}")
    sid = data["sessionId"]
    chunk = data.get("chunkSize") or len(content)
    for index in data.get("missing") or []:
        start = index * chunk
        req = urllib.request.Request(
            f"{base}/api/uploads/{sid}/{index}", data=content[start : start + chunk], method="PUT"
        )
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("Cookie", cookie)
        urllib.request.urlopen(req, timeout=30).read()
    status, done = http(base, "POST", f"/api/uploads/{sid}/complete", None, cookie)
    if status != 200:
        raise SystemExit(f"complete 失败 {status} {done}")
    return done["node"]


def main() -> int:
    from app.config import settings
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-pickercount-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)

    con = sqlite3.connect(str(db_copy))
    row = con.execute(
        "select id, username, token_version from users order by role desc, id limit 1"
    ).fetchone()
    if row is None:
        con.execute(
            "insert into users (username, password_hash, display_name, quota_bytes, used_bytes, role,"
            " token_version, created_at) values (?,?,?,?,?,?,?,datetime('now'))",
            ("pickercount", "x", "pickercount", 10 * 1024 ** 3, 0, 1, 0),
        )
        con.commit()
        row = con.execute(
            "select id, username, token_version from users order by role desc, id limit 1"
        ).fetchone()
    uid, uname, uver = row
    con.execute("update users set password_hash=? where id=?", (hash_password(PASSWORD), uid))
    try:
        con.execute(
            "update user_settings set quick_terms_at=datetime('now') where user_id=?", (uid,)
        )
    except sqlite3.OperationalError:
        pass
    con.commit()
    con.close()
    cookie = f"cloudnetp_session={create_token(uid, uver)}"

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

        # 造数据：根目录放 有内容文件夹(2) / 空文件夹 / 单文件
        status, folder = http(
            base, "POST", "/api/nodes/folder", {"parentId": None, "name": "计数验证夹"}, cookie
        )
        if status != 201:
            raise SystemExit(f"建目录失败 {status} {folder}")
        folder_id = folder["node"]["id"]
        upload(base, cookie, "甲.txt", b"a" * 200, folder_id)
        upload(base, cookie, "乙.txt", b"b" * 300, folder_id)
        status, empty = http(
            base, "POST", "/api/nodes/folder", {"parentId": None, "name": "空夹子"}, cookie
        )
        empty_id = empty["node"]["id"]
        upload(base, cookie, "根上一个.txt", b"root" * 10)

        # 先确认接口层字段是对的（问题只在前端读错名字）
        status, listing = http(base, "GET", "/api/nodes?sort=name&order=asc", None, cookie)
        by_name = {item["name"]: item for item in listing.get("items") or []}
        check(
            "[A1] 接口下发 childCount",
            by_name.get("计数验证夹", {}).get("childCount") == 2
            # 空文件夹没有子节点，SQL group by 里不会出现这行 → null，前端 `?? 0` 兜住
            and by_name.get("空夹子", {}).get("childCount") in (None, 0),
            f"{by_name.get('计数验证夹', {}).get('childCount')}"
            f" / {by_name.get('空夹子', {}).get('childCount')}",
        )
        check(
            "[A2] 接口不再有旧的 count 字段",
            "count" not in by_name.get("计数验证夹", {}),
            str(sorted(by_name.get("计数验证夹", {}).keys())),
        )

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)

            ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
            ctx.add_cookies([{"name": "cloudnetp_session", "value": create_token(uid, uver), "url": base}])
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on(
                "console",
                lambda msg: errors.append(msg.text)
                if msg.type == "error" and "Failed to load resource" not in msg.text
                else None,
            )
            page.goto(base + "/#/quick", wait_until="domcontentloaded")
            page.wait_for_selector(".quick-tabs", timeout=20000)

            print("\n[A] 打开选择器")
            page.locator('.quick-tab[data-tab="send"]').click()
            page.wait_for_timeout(400)
            page.locator('[data-act="pick"]').first.click()
            page.wait_for_selector(".picker__row", timeout=15000)

            def row_size(name: str) -> str:
                """走 DOM 拿文本，避免 inner_text 里的换行/空格影响比对。"""
                return page.evaluate(
                    """(name) => {
                        const rows = [...document.querySelectorAll('.picker__row')];
                        const hit = rows.find((r) =>
                            (r.querySelector('.picker__name')?.textContent || '').trim() === name);
                        return hit ? (hit.querySelector('.picker__size')?.textContent || '').trim() : '(缺行)';
                    }""",
                    name,
                )

            print("\n[B] 有内容的文件夹")
            text = row_size("计数验证夹")
            check("显示 2 项", "2" in text and "项" in text, text)
            check("不是 0 项", not text.startswith("0"), text)

            print("\n[C] 空文件夹")
            text = row_size("空夹子")
            check("显示 0 项", text.startswith("0"), text)

            print("\n[D] 文件行")
            text = row_size("根上一个.txt")
            check("显示体积不是项", "项" not in text and text != "", text)
            page.screenshot(path=str(OUT / "picker_count_root.png"))

            print("\n[E] 进到文件夹里")
            page.locator(".picker__row", has_text="计数验证夹").first.locator("[data-enter]").click()
            page.wait_for_timeout(500)
            inner = page.evaluate(
                """() => [...document.querySelectorAll('.picker__row')].map((r) => ({
                    name: (r.querySelector('.picker__name')?.textContent || '').trim(),
                    size: (r.querySelector('.picker__size')?.textContent || '').trim(),
                }))"""
            )
            names = [item["name"] for item in inner]
            check("子项是两个文件", sorted(names) == ["乙.txt", "甲.txt"], str(names))
            check("子项显示体积", all("项" not in item["size"] for item in inner), str(inner))

            page.screenshot(path=str(OUT / "picker_count.png"))

            print("\n[F] 控制台")
            real = [e for e in errors if "favicon" not in e.lower()]
            check("无 JS 异常", not real, str(real[:3]))
            browser.close()

        con = sqlite3.connect(str(db_copy))
        orphan = con.execute(
            "select count(*) from nodes where name in ('计数验证夹','空夹子','根上一个.txt')"
        ).fetchone()[0]
        con.close()
        check("造的数据进了副本库（没碰真库）", orphan == 3, str(orphan))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    print(f"\n结果：{passed} 通过，{failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
