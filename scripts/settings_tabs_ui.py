# -*- coding: utf-8 -*-
"""设置页标签页改版回归。

  1. 四个页签齐全，默认停在外观页，其它页隐藏
  2. 点页签只切 class/hidden 不重建 DOM（同一节点还在）
  3. 外观页藏着时进去再切回来，主题模式滑块要重新排（不是 0 宽）
  4. 记住上次的页签：刷新后还在那一页
  5. 手机竖屏页签条能横向滚、切换正常

    ./.venv/Scripts/python.exe scripts/settings_tabs_ui.py
"""

from __future__ import annotations

import os
import socket
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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-settabs-")).resolve()
    db_path = tmp / "ui.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["STORAGE_DIR"] = str(tmp / "storage")
    os.environ["DB_FALLBACK_ENABLED"] = "0"
    os.environ["DB_PROBE_INTERVAL"] = "0"
    os.environ["DB_MIRROR_INTERVAL"] = "0"

    sys.path.insert(0, str(ROOT))
    from app.config import settings
    from app.db import SessionLocal, engine, ensure_schema
    from app.models import User, utcnow
    from app.security import create_token, hash_password

    ensure_schema(engine)
    pw = "Passw0rd!2024"
    with SessionLocal() as db:
        who = User(
            username="settabs",
            password_hash=hash_password(pw),
            email="settabs@example.com",
            email_verified=True,
            role=0,
            quota_bytes=10 * 1024**3,
            token_version=1,
            created_at=utcnow(),
        )
        db.add(who)
        db.commit()
        uid, ver = who.id, who.token_version

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
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

    from playwright.sync_api import sync_playwright

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来")
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)

            def make_page(w=1440, h=1000, mobile=False):
                ctx = (
                    browser.new_context(
                        viewport={"width": w, "height": h}, device_scale_factor=2,
                        is_mobile=True, has_touch=True,
                    )
                    if mobile
                    else browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2)
                )
                ctx.add_cookies([{"name": "cloudnetp_session", "value": create_token(uid, ver), "url": base}])
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                return ctx, page, errors

            print("\n[A] 页签结构与默认页")
            ctx, page, errors = make_page()
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector("[data-settings-tabs] .segmented__btn", timeout=20000)
            page.wait_for_timeout(700)

            structure = page.evaluate(
                """() => {
                  const btns = [...document.querySelectorAll('[data-settings-tab]')];
                  const panes = [...document.querySelectorAll('[data-settings-pane]')];
                  const thumb = document.querySelector('[data-tab-thumb]');
                  return {
                    tabs: btns.map((b) => b.dataset.settingsTab),
                    active: btns.filter((b) => b.classList.contains('is-active')).map((b) => b.dataset.settingsTab),
                    panes: panes.map((p) => [p.dataset.settingsPane, !p.hidden]),
                    thumbW: Math.round(thumb.getBoundingClientRect().width),
                  };
                }"""
            )
            check(
                "四个页签",
                structure["tabs"] == ["appearance", "mascot", "files", "account"],
                str(structure["tabs"]),
            )
            check("默认停在外观页", structure["active"] == ["appearance"], str(structure["active"]))
            check(
                "只有外观页可见",
                structure["panes"] == [["appearance", True], ["mascot", False], ["files", False], ["account", False]],
                str(structure["panes"]),
            )
            check("页签滑块排开了", structure["thumbW"] > 20, str(structure["thumbW"]))
            page.screenshot(path=str(OUT / "settings_tabs_appearance.png"))

            print("\n[B] 切页签：不重建 DOM，目标卡可见")
            ai_card_before = page.evaluate("document.querySelector('[data-ai-card]') !== null")
            page.click('[data-settings-tab="mascot"]')
            page.wait_for_timeout(400)
            mascot = page.evaluate(
                """() => {
                  const pane = document.querySelector('[data-settings-pane="mascot"]');
                  const ai = document.querySelector('[data-ai-card]');
                  const appear = document.querySelector('[data-settings-pane="appearance"]');
                  return {
                    paneVisible: !pane.hidden,
                    aiAttached: !!ai,
                    appearHidden: appear.hidden,
                    saved: localStorage.getItem('settings-tab'),
                  };
                }"""
            )
            check("看板娘页可见", mascot["paneVisible"], str(mascot))
            check("AI 卡在这页里", ai_card_before and mascot["aiAttached"], str(mascot))
            check("外观页收起", mascot["appearHidden"], str(mascot))
            check("页签记进了 localStorage", mascot["saved"] == "mascot", str(mascot["saved"]))
            page.screenshot(path=str(OUT / "settings_tabs_mascot.png"))

            print("\n[C] 切回外观页：主题模式滑块要重新排")
            page.click('[data-settings-tab="appearance"]')
            page.wait_for_timeout(400)
            thumb = page.evaluate(
                """() => {
                  const seg = document.querySelector('[data-theme-seg]');
                  const th = seg.querySelector('[data-thumb]');
                  return { w: Math.round(th.getBoundingClientRect().width),
                           x: Math.round(th.getBoundingClientRect().left - seg.getBoundingClientRect().left) };
                }"""
            )
            check("主题模式滑块不是 0 宽", thumb["w"] > 20, str(thumb))

            print("\n[D] 文件与分享 / 账号页")
            page.click('[data-settings-tab="files"]')
            page.wait_for_timeout(350)
            files = page.evaluate(
                """() => ({
                  trash: !!document.querySelector('[data-toggle="trashEnabled"]'),
                  dav: !!document.querySelector('[data-dav-url]'),
                  share: !!document.querySelector('[data-toggle="shareShowName"]'),
                  visible: !document.querySelector('[data-settings-pane="files"]').hidden,
                })"""
            )
            check("文件页三张卡都在", all(files.values()), str(files))
            page.screenshot(path=str(OUT / "settings_tabs_files.png"))

            page.click('[data-settings-tab="account"]')
            page.wait_for_timeout(350)
            account = page.evaluate(
                """() => ({
                  profile: !!document.querySelector('[data-act="profile-save"]'),
                  danger: !!document.querySelector('[data-card="danger"]'),
                  visible: !document.querySelector('[data-settings-pane="account"]').hidden,
                })"""
            )
            check("账号页资料卡与危险区都在", all(account.values()), str(account))
            page.screenshot(path=str(OUT / "settings_tabs_account.png"))

            print("\n[E] 刷新后还在账号页")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("[data-settings-tabs] .segmented__btn", timeout=20000)
            page.wait_for_timeout(500)
            after = page.evaluate(
                """() => ({
                  active: document.querySelector('[data-settings-tab].is-active')?.dataset.settingsTab,
                  accountVisible: !document.querySelector('[data-settings-pane="account"]').hidden,
                })"""
            )
            check("刷新后仍在账号页", after["active"] == "account" and after["accountVisible"], str(after))
            ctx.close()

            print("\n[F] 手机竖屏")
            mctx, mpage, merrors = make_page(390, 844, mobile=True)
            mpage.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            mpage.wait_for_selector("[data-settings-tabs] .segmented__btn", timeout=20000)
            mpage.wait_for_timeout(700)
            mpage.click('[data-settings-tab="files"]')
            mpage.wait_for_timeout(350)
            mobile_state = mpage.evaluate(
                """() => {
                  const bar = document.querySelector('.settings-tabs');
                  const seg = bar.querySelector('.segmented');
                  const pane = document.querySelector('[data-settings-pane="files"]');
                  const last = [...seg.querySelectorAll('[data-settings-tab]')].pop();
                  return {
                    paneVisible: !pane.hidden,
                    barW: Math.round(bar.getBoundingClientRect().width),
                    segClipped: seg.scrollWidth > seg.clientWidth + 1,
                    lastRight: Math.round(last.getBoundingClientRect().right),
                    barRight: Math.round(bar.getBoundingClientRect().right),
                    noHOverflow: document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1,
                  };
                }"""
            )
            check("手机上能切到文件页", mobile_state["paneVisible"], str(mobile_state))
            check("四个页签不用横向滚也能看完", not mobile_state["segClipped"], str(mobile_state))
            check(
                "最后一个页签没被裁掉",
                mobile_state["lastRight"] <= mobile_state["barRight"] + 1,
                str(mobile_state),
            )
            check("页面没有横向溢出", mobile_state["noHOverflow"], str(mobile_state))
            mpage.screenshot(path=str(OUT / "settings_tabs_mobile.png"))
            mctx.close()

            check("没有 JS 报错", not errors and not merrors, str((errors + merrors)[:1]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_file.close()

    print()
    print(f"通过 {PASS} / {PASS + FAIL}，截图在 {OUT}")
    return 1 if FAIL else 0


sys.exit(main())
