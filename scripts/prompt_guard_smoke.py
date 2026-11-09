# -*- coding: utf-8 -*-
"""内置系统提示词的「不外泄」冒烟。

跑一个用库副本起的独立实例（不碰真实库，跑完删临时目录），检查：

  1. GET /api/ai/config 不再返回 defaultPrompt 字段
  2. GET /api/ai/prompt 同样只回自定义内容
  3. 两个接口的响应体里都搜不到内置提示词的正文片段
  4. 普通用户拿到的 systemPrompt 是空串
  5. 未登录访问 /api/ai/prompt 被挡
  6. 设置页 UI：没有「填入默认」按钮，textarea 里也不是内置正文

    ./.venv/Scripts/python.exe scripts/prompt_guard_smoke.py
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
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

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
    # 现役库是容灾副本；dev.db 是更早的开发库，schema 可能落后
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
    # 0.3.44 才加的 language 列：老库里没有，副本上先补上，免得后面 update 直接报错
    con = sqlite3.connect(str(dst))
    try:
        if "language" not in [row[1] for row in con.execute("pragma table_info(user_settings)")]:
            con.execute("alter table user_settings add column language varchar(16)")
            con.commit()
    finally:
        con.close()


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
    print(log_path.read_text(encoding="utf-8", errors="replace")[-1200:])
    return False


def get_json(url: str, cookie: str | None) -> tuple[int, str]:
    req = urllib.request.Request(url)
    if cookie:
        req.add_header("Cookie", f"cloudnetp_session={cookie}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def probes() -> list[str]:
    """从内置提示词里挑几段特征句：任何一段出现在响应里都算泄露。

    取带名字「CDP娘」的整句，而不是按固定偏移切中段——后者容易切到
    「工具边界」这类用户自定义人设里也会写的通用规则，造成假阳性。
    """
    from app.ai_service import DEFAULT_SYSTEM_PROMPTS

    out: list[str] = []
    for text in DEFAULT_SYSTEM_PROMPTS.values():
        for line in text.strip().splitlines():
            line = line.strip()
            if "CDP娘" in line and len(line) >= 24:
                out.append(line[:52])
    return out


def main() -> None:
    from app.config import settings
    from app.security import create_token

    probes_list = probes()
    check("能取到内置提示词做比对样本", len(probes_list) >= 2, f"{len(probes_list)} 段")

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-prompt-")).resolve()
    db_copy = tmp / "guard.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    row = con.execute("select id, token_version from users where role = 1 order by id limit 1").fetchone()
    if not row:
        con.close()
        raise SystemExit("副本库里没有管理员")
    admin_cookie = create_token(row[0], row[1])
    # 界面语言跟着账号走：副本里把管理员切成英文，UI 断言顺便覆盖英文文案
    con.execute("update user_settings set language = 'en-US' where user_id = ?", (row[0],))
    con.commit()
    # 普通用户：优先找现成的，没有就在副本里造一个（临时库，不影响真实数据）
    plain = con.execute("select id, token_version from users where role != 1 order by id limit 1").fetchone()
    if not plain:
        con.execute(
            "insert into users (username, password_hash, display_name, quota_bytes, used_bytes, role,"
            " token_version, created_at) values (?,?,?,?,?,?,?,datetime('now'))",
            ("guard_probe", "x", "Guard Probe", 1024 * 1024, 0, 0, 0),
        )
        con.commit()
        plain = con.execute("select id, token_version from users where username = 'guard_probe'").fetchone()
    con.close()
    plain_cookie = create_token(plain[0], plain[1])

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

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来")

        print("\n[1] 管理员接口")
        status, body = get_json(f"{base}/api/ai/config", admin_cookie)
        check("GET /api/ai/config 200", status == 200, str(status))
        data = json.loads(body) if status == 200 else {}
        check("响应里没有 defaultPrompt 字段", "defaultPrompt" not in data)
        check("响应里没有 lang 字段", "lang" not in data)
        leaks = [p for p in probes_list if p in body]
        check("config 响应体不含内置提示词正文", not leaks, f"命中 {len(leaks)} 段" if leaks else "")

        status, body = get_json(f"{base}/api/ai/prompt", admin_cookie)
        check("GET /api/ai/prompt 200", status == 200, str(status))
        if status == 200:
            pdata = json.loads(body)
            check("prompt 接口只有 prompt 字段", set(pdata) == {"prompt"}, str(sorted(pdata)))
        leaks = [p for p in probes_list if p in body]
        check("prompt 响应体不含内置提示词正文", not leaks, f"命中 {len(leaks)} 段" if leaks else "")

        print("\n[2] 普通用户 / 未登录")
        status, body = get_json(f"{base}/api/ai/config", plain_cookie)
        if status == 200:
            pdata = json.loads(body)
            check("普通用户 systemPrompt 为空", not pdata.get("systemPrompt"))
            check("普通用户 isAdmin 为 false", pdata.get("isAdmin") is False)
            leaks = [p for p in probes_list if p in body]
            check("普通用户响应不含内置正文", not leaks, f"命中 {len(leaks)} 段" if leaks else "")
        else:
            check("普通用户 GET /api/ai/config 200", False, str(status))

        status, _ = get_json(f"{base}/api/ai/prompt", plain_cookie)
        # require_admin 用 404 而非 403：不暴露管理端点是否存在
        check("普通用户访问 /api/ai/prompt 被拒", status in (401, 403, 404), str(status))
        status, _ = get_json(f"{base}/api/ai/prompt", None)
        check("未登录访问 /api/ai/prompt 被拒", status in (401, 403), str(status))

        print("\n[3] 设置页 UI")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)
            ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
            ctx.add_cookies([{"name": "cloudnetp_session", "value": admin_cookie, "url": base}])
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.add_init_script(
                "try { localStorage.setItem('cloudnetp.lang', 'en-US'); } catch (e) {}"
                "window.addEventListener('DOMContentLoaded', () => {"
                "  const s = document.createElement('style');"
                "  s.textContent = '#mascot{display:none!important}';"
                "  document.head.appendChild(s);"
                "});"
            )
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector(".ai-prompt", timeout=20000)
            page.wait_for_timeout(600)
            page.screenshot(path=str(tmp / "settings.png"), full_page=True)

            has_button = page.evaluate("() => Boolean(document.querySelector('[data-ai-prompt-default]'))")
            check("页面上没有 data-ai-prompt-default 按钮", not has_button)
            labels = page.evaluate(
                "() => Array.from(document.querySelectorAll('.ai-prompt button')).map((b) => b.textContent.trim())"
            )
            check("按钮只剩保存提示词", labels == ["Save prompt"], str(labels))
            area_value = page.evaluate("() => document.querySelector('[data-ai-prompt]')?.value || ''")
            check(
                "textarea 里不是内置正文",
                not any(p in area_value for p in probes_list),
                f"长度 {len(area_value)}",
            )
            desc = page.evaluate("() => document.querySelector('.ai-prompt .setting-row__desc')?.textContent || ''")
            check("英文界面下说明文案已翻译", desc.strip().startswith("Defines CDP娘"), desc.strip()[:48])
            check("说明文案不再提「中英各一版」", "中英各一版" not in desc)
            check("页面无 JS 报错", not errors, f"{len(errors)} 条" if errors else "")
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_file.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n通过 {passed}，失败 {failed}")
    raise SystemExit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
