# -*- coding: utf-8 -*-
"""英文界面下三处新增文案的回归。

  [A] 设置 → 快传 页签：开关标题、说明、存放位置全部是英文
  [B] 管理后台 → 审计日志：0.4.6 / 0.4.8 新增的 5 个动作名是英文
  [C] 设置 → 看板娘：锁定开关的气泡提示是英文（且中文界面下文案正确）

    ./.venv/Scripts/python.exe scripts/i18n_new_strings_ui.py
"""

from __future__ import annotations

import json
import os
import re
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
CJK = re.compile(r"[\u4e00-\u9fa5]")

# 英文界面里允许出现的中文：看板娘固定叫 CDP娘，语言自称
ALLOW = {"CDP娘"}

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


def leftover(items: list[str]) -> list[str]:
    """剩下真正该翻译却还是中文的文案。

    两条排除规则：
      - 看板娘固定叫 CDP娘，剔掉名字再看有没有中文
      - 超过 150 字的当作「内容」跳过（AI 系统提示词、技能说明都允许是中文，那是数据不是界面）
    """
    out = []
    for item in items:
        text = (item or "").strip()
        if not text:
            continue
        for name in ALLOW:
            text = text.replace(name, "")
        text = text.strip()
        if not text or not CJK.search(text) or len(text) > 150:
            continue
        out.append(text)
    return out


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


def login_cookie(base: str, username: str, password: str) -> str:
    """走真实登录接口拿会话 cookie（比本地签 token 稳，不依赖密钥一致）。"""
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
    raise SystemExit("登录没拿到会话 cookie")


def collect(page, selector: str) -> list[str]:
    """收元素可见文本 + title/aria-label/placeholder。"""
    return page.evaluate(
        """(sel) => {
            const out = [];
            const root = document.querySelector(sel);
            if (!root) return out;
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
            let n;
            while ((n = walker.nextNode())) {
                const t = (n.textContent || '').trim();
                if (t) out.push(t);
            }
            root.querySelectorAll('*').forEach((el) => {
                for (const a of ['title', 'aria-label', 'placeholder']) {
                    const v = el.getAttribute(a);
                    if (v && v.trim()) out.push(v.trim());
                }
            });
            return out;
        }""",
        selector,
    )


def main() -> int:
    from app.config import settings
    from app.security import hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-i18nui-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)

    con = sqlite3.connect(str(db_copy))
    row = con.execute(
        "select id, username, token_version from users order by role desc, id limit 1"
    ).fetchone()
    if row is None:
        raise SystemExit("副本库里没有用户")
    admin_id, admin_name, _admin_ver = row
    con.execute(
        "update users set password_hash = ?, role = 1 where id = ?",
        (hash_password(PASSWORD), admin_id),
    )
    # 造 5 条新动作的审计记录，排在最前面（列表按时间倒序）
    actions = [
        ("quick_send_accept", '{"name": "同意看看.txt"}'),
        ("quick_send_reject", '{"name": "不要了.txt"}'),
        ("quick_inbox_folder", '{"path": "我的文件 / 快传的文件"}'),
        ("quick_pickup_delete", '{"code": "483920"}'),
        ("quick_drop_delete", '{"token": "abc123"}'),
    ]
    for action, detail in actions:
        con.execute(
            "insert into audit_logs (user_id, action, detail, ip, created_at)"
            " values (?,?,?,?,datetime('now'))",
            (admin_id, action, detail, "127.0.0.1"),
        )
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

        session = login_cookie(base, admin_name, PASSWORD)
        if not session:
            raise SystemExit("登录失败，检查密码重置是否生效")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--force-device-scale-factor=1"],
            )
            errors: list[str] = []
            ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
            ctx.add_cookies([{"name": "cloudnetp_session", "value": session, "url": base}])
            page = ctx.new_page()
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.add_init_script("try { localStorage.setItem('cloudnetp.lang', 'en-US'); } catch (e) {}")

            # ---------------- A 设置 → 快传 ----------------
            print("\n[A] Settings → Quick transfer")
            page.goto(base + "/#/settings", wait_until="domcontentloaded")
            try:
                page.wait_for_selector("[data-settings-tab]", timeout=25000)
            except Exception:
                print("URL:", page.url)
                print("HTML:", page.evaluate("document.body.innerText")[:400])
                print("errors:", errors)
                page.screenshot(path=str(OUT / "_i18n_probe.png"))
                raise
            page.locator('[data-settings-tab="quick"]').click()
            page.wait_for_selector('[data-settings-pane="quick"]:not([hidden])', timeout=15000)
            page.wait_for_timeout(600)

            quick_texts = collect(page, '[data-settings-pane="quick"]')
            bad = leftover(quick_texts)
            check("快传页签没有残留中文", not bad, str(bad[:6]))
            joined = " ".join(quick_texts)
            check("开关标题是英文", "Let others send me files" in joined, joined[:80])
            check("自动同意是英文", "Auto-accept straight into My Files" in joined)
            check("存放位置标题是英文", "Save received files to" in joined, joined[-90:])
            path_text = page.locator("[data-quick-folder-path]").inner_text().strip()
            check("默认存放路径是英文", path_text == "My Files / Quick transfer", path_text)
            page.screenshot(path=str(OUT / "i18n_quick_settings.png"), full_page=True)

            # ---------------- B 后台 → 审计日志 ----------------
            print("\n[B] Admin → Audit log")
            page.goto(base + "/#/admin", wait_until="domcontentloaded")
            page.wait_for_selector(".admin-tabs [data-tab]", timeout=25000)
            page.locator('[data-tab="logs"]').click()
            page.wait_for_selector(".log-row", timeout=25000)
            page.wait_for_timeout(700)

            # 只看动作名（log-title 的第一段）和筛选下拉，用户名/IP/详情是用户数据
            log_titles = page.evaluate(
                """() => [...document.querySelectorAll('.log-row .log-title')].map((el) => {
                    const first = [...el.childNodes]
                        .filter((n) => n.nodeType === 3)
                        .map((n) => n.textContent.trim())
                        .join(" ")
                        .trim();
                    return first;
                })"""
            )
            five = ["Accepted quick transfer", "Declined quick transfer",
                    "Changed quick-transfer inbox folder", "Deleted pickup code record",
                    "Deleted drop code record"]
            shown = [t for t in log_titles if t]
            check("5 条新动作都在第一页", all(any(f == s for s in shown) for f in five), str(shown[:8]))
            bad_logs = leftover(shown)
            check("动作名没有残留中文", not bad_logs, str(bad_logs[:6]))

            page.wait_for_selector("[data-action] option", state="attached", timeout=20000)
            options = page.evaluate(
                "() => [...document.querySelectorAll('[data-action] option')].map((o) => o.textContent.trim())"
            )
            bad_opts = leftover(options)
            check("筛选下拉没有残留中文", not bad_opts, str(bad_opts[:8]))
            check("下拉条目够多（动作表已载入）", len(options) > 20, str(len(options)))
            page.screenshot(path=str(OUT / "i18n_admin_logs.png"), full_page=True)

            # ---------------- C 看板娘锁定气泡 ----------------
            print("\n[C] Mascot lock toast")
            page.goto(base + "/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector("[data-settings-tab]", timeout=25000)
            page.locator('[data-settings-tab="mascot"]').click()
            page.wait_for_selector('[data-settings-pane="mascot"]:not([hidden])', timeout=15000)
            page.wait_for_timeout(500)

            mascot_texts = collect(page, '[data-settings-pane="mascot"]')
            bad_mascot = leftover(mascot_texts)
            check("看板娘页签没有残留中文", not bad_mascot, str(bad_mascot[:6]))

            page.evaluate("() => document.querySelectorAll('.toast').forEach((el) => el.remove())")
            page.locator('label.switch:has([data-mascot-lock])').click()
            page.wait_for_selector(".toast", timeout=15000)
            toast_en = " ".join(page.locator(".toast").all_inner_texts()).strip()
            check("锁定气泡是英文", bool(toast_en) and not CJK.search(toast_en), toast_en)
            check("气泡提到设置入口", "Settings" in toast_en, toast_en)
            page.screenshot(path=str(OUT / "i18n_mascot_lock_toast.png"))

            page.evaluate("() => document.querySelectorAll('.toast').forEach((el) => el.remove())")
            page.locator('label.switch:has([data-mascot-lock])').click()
            page.wait_for_selector(".toast", timeout=15000)
            toast_unlock = " ".join(page.locator(".toast").all_inner_texts()).strip()
            check("解锁气泡是英文", bool(toast_unlock) and not CJK.search(toast_unlock), toast_unlock)

            # ---------------- D 中文文案本身对不对（读源码 + 字典，不依赖界面语言） ----------------
            print("\n[D] Chinese wording")
            src = (ROOT / "static" / "js" / "ui" / "mascot-actions.js").read_text(encoding="utf-8")
            check("气泡指向「设置 → 看板娘」", "在「设置 → 看板娘」里可以解锁" in src and "装饰" not in src)
            check("锁定气泡过了 t()", 't("看板娘已锁定' in src)
            check("解锁气泡过了 t()", 't("已解锁，看板娘' in src)
            dict_src = (ROOT / "static" / "js" / "i18n" / "en-US.js").read_text(encoding="utf-8")
            check(
                "字典里两条气泡都有",
                "看板娘已锁定，鼠标可以穿透了；在「设置 → 看板娘」里可以解锁" in dict_src
                and "已解锁，看板娘又能拖动和戳一戳了" in dict_src,
            )

            check("控制台无 JS 异常", not errors, str(errors[:3]))
            ctx.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    print(f"\n结果：{passed} 项通过，{failed} 项失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
