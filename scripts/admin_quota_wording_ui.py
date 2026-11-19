# -*- coding: utf-8 -*-
"""后台概览「存储」卡的配额文案。

背景：配额是**每个账号一份**，而概览里的「全站已用」是所有账号加起来的。
早先那行写成「用户逻辑占用 454 MB / 10.0 GB」，很容易被读成「全站共用 10 GB，
满了就谁都存不进去」。现在文案改成「全站已用 X · 单账号上限 Y」，
水位百分比改成看「已用最多的那个账号」，所以这里按两个账号不同的占用去验算。

  [A] 存储卡：全站已用 = 两个账号之和，单账号上限 = 较大的那个配额
  [B] 水位百分比指向占用最高的账号（不是全站已用除以单人上限）
  [C] 进度条宽度跟文案里的百分比一致
  [D] 「逻辑占用」统计卡：数值是全站合计，副标题点名「全站合计」
  [E] 旧文案「用户逻辑占用 … / …」不再出现
  [F] 390px 不横向溢出
  [G] 控制台无 JS 异常

    ./.venv/Scripts/python.exe scripts/admin_quota_wording_ui.py
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
GB = 1024 ** 3
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


def http(base: str, method: str, path: str, cookie: str = ""):
    req = urllib.request.Request(base + path, method=method)
    req.add_header("Origin", base)
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


def main() -> int:
    from app.config import settings
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-quotaui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)

    # 造两个账号：A 已用 1 GB / 配额 10 GB，B 已用 5 GB / 配额 20 GB。
    # 期望：全站已用 6 GB，单账号上限 20 GB（取较大值），水位看 B = 25%。
    con = sqlite3.connect(str(db_copy))
    con.execute("update users set used_bytes = 0, banned = 0")
    rows = con.execute(
        "select id, username, token_version from users order by role desc, id"
    ).fetchall()
    # 副本库里用户不够就补两个占位账号（banned 是 NOT NULL 且没有 SQL 默认值，必须显式给）
    while len(rows) < 2:
        name = f"quotaui{len(rows)}"
        con.execute(
            "insert into users (username, password_hash, display_name, role, quota_bytes,"
            " used_bytes, banned, token_version, created_at)"
            " values (?,?,?,?,?,?,?,?,datetime('now'))",
            (name, "x", name, 0, 10 * GB, 0, 0, 0),
        )
        con.commit()
        rows = con.execute(
            "select id, username, token_version from users order by role desc, id"
        ).fetchall()
    admin_id, _admin_name, admin_ver = rows[0]
    heavy_id, heavy_name = rows[1][0], rows[1][1]
    con.execute(
        "update users set role = 1, quota_bytes = ?, used_bytes = ? where id = ?",
        (10 * GB, 1 * GB, admin_id),
    )
    con.execute(
        "update users set role = 0, quota_bytes = ?, used_bytes = ? where id = ?",
        (20 * GB, 5 * GB, heavy_id),
    )
    hashed = hash_password(PASSWORD)
    con.execute("update users set password_hash=? where id in (?,?)", (hashed, admin_id, heavy_id))
    con.commit()
    con.close()

    token = create_token(admin_id, admin_ver)
    cookie = f"cloudnetp_session={token}"

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

        # 先确认接口侧的原始数字（对不上就是造数据的问题，不是文案的问题）
        status, ov = http(base, "GET", "/api/admin/overview", cookie)
        expect_used = 6 * GB
        expect_quota = 20 * GB
        check(
            "[0] 接口：全站已用 = 6 GB、单账号上限 = 20 GB",
            status == 200
            and ov["users"]["usedBytes"] == expect_used
            and ov["users"]["quotaBytes"] == expect_quota,
            f"{ov.get('users', {}).get('usedBytes')} / {ov.get('users', {}).get('quotaBytes')}",
        )
        top = (ov.get("topUsers") or [{}])[0]
        check("[0] 占用排行第一是 5 GB 那个账号", top.get("username") == heavy_name, str(top))

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)

            def open_admin(width: int, height: int):
                ctx = browser.new_context(
                    viewport={"width": width, "height": height}, device_scale_factor=2
                )
                ctx.add_cookies([{"name": "cloudnetp_session", "value": token, "url": base}])
                page = ctx.new_page()
                errs: list[str] = []
                page.on("pageerror", lambda exc: errs.append(str(exc)))
                page.on(
                    "console",
                    lambda msg: errs.append(msg.text)
                    if msg.type == "error" and "Failed to load resource" not in msg.text
                    else None,
                )
                page.goto(base + "/#/admin", wait_until="domcontentloaded")
                page.wait_for_selector(".stat-grid", timeout=20000)
                page.wait_for_selector(".admin-facts", timeout=20000)
                return ctx, page, errs

            ctx, page, errors = open_admin(1440, 1000)

            storage_row = page.locator(".admin-card__pad .row").first.inner_text().replace("\n", " ")
            print(f"\n  存储卡那行：{storage_row}")

            print("\n[A] 存储卡文案")
            check("写了「全站已用」", "全站已用" in storage_row, storage_row[:80])
            check("全站已用 = 6.0 GB", "6.0 GB" in storage_row, storage_row[:80])
            check("单账号上限 = 20.0 GB", "20.0 GB" in storage_row, storage_row[:80])
            check("不再用「/」把人引向「共用一份」", " / " not in storage_row, storage_row[:80])

            print("\n[B] 水位百分比")
            check("显示「最满账号 25%」", "最满账号 25%" in storage_row, storage_row[:80])
            tip = page.evaluate(
                "() => { const s = document.querySelectorAll('.admin-card__pad .row .muted')[1];"
                " return s ? (s.getAttribute('title') || '') : ''; }"
            )
            check("悬停能看到是哪个账号", heavy_name in tip, tip)

            print("\n[C] 进度条")
            width = page.evaluate(
                "() => document.querySelector('.admin-card__pad .progress__bar').style.width"
            )
            check("宽度 = 25%", width.strip() == "25%", width)

            print("\n[D] 逻辑占用统计卡")
            card_sub = page.evaluate(
                """() => {
                    const cards = [...document.querySelectorAll('.stat-card')];
                    const hit = cards.find((c) =>
                        (c.querySelector('.stat-card__label')?.textContent || '').includes('逻辑占用'));
                    return hit ? {
                        value: (hit.querySelector('.stat-card__value')?.textContent || '').trim(),
                        sub: (hit.querySelector('.stat-card__sub')?.textContent || '').trim(),
                    } : null;
                }"""
            )
            check("数值是全站合计 6.0 GB", card_sub and card_sub["value"] == "6.0 GB", str(card_sub))
            check(
                "副标题点名「全站合计 · 单账号配额上限 20.0 GB」",
                card_sub
                and "全站合计" in card_sub["sub"]
                and "20.0 GB" in card_sub["sub"],
                str(card_sub),
            )

            print("\n[E] 旧文案")
            body = page.locator(".admin-body").inner_text()
            check("没有「用户逻辑占用」", "用户逻辑占用" not in body)
            check("没有「配额合计」当刻度", "配额合计" not in body)

            page.screenshot(path=str(OUT / "admin_quota_wording.png"))

            print("\n[F] 390px")
            ctx_m, page_m, errors_m = open_admin(390, 844)
            overflow = page_m.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            check("不横向溢出", overflow <= 1, str(overflow))
            row_m = page_m.locator(".admin-card__pad .row").first.inner_text().replace("\n", " ")
            check("窄屏也写着「全站已用」", "全站已用" in row_m, row_m[:80])
            page_m.screenshot(path=str(OUT / "admin_quota_wording_mobile.png"))

            print("\n[G] 控制台")
            real = [e for e in errors + errors_m if "favicon" not in e.lower()]
            check("无 JS 异常", not real, str(real[:3]))
            browser.close()

        con = sqlite3.connect(str(db_copy))
        total = con.execute("select sum(used_bytes) from users").fetchone()[0]
        con.close()
        check("数据只落在副本库（合计 6 GB）", total == 6 * GB, str(total))
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
