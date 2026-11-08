# -*- coding: utf-8 -*-
"""生成 Wiki 文档用的步骤截图。

复用 make_readme_shots.py 的演示数据与起服逻辑，在临时库 + 临时存储里截取
操作步骤图（注册勾选、右键菜单、分享弹窗、看板娘对话、后台各页签等），
压到固定宽度写进 Wiki/.vuepress/public/images/。

    ./.venv/Scripts/python.exe scripts/make_wiki_shots.py [--keep]
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_readme_shots import DESK_W, DESK_H, VENV_PY, free_port, seed, wait_up

OUT = ROOT / "Wiki" / ".vuepress" / "public" / "images"
KEEP = "--keep" in sys.argv

# Wiki 里直接引用 README 已有的成图，这里只补步骤类截图
REUSE = ["login", "files", "viewer-image", "viewer-markdown", "settings", "share", "admin", "mobile"]


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-wiki-")).resolve()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    os.environ.update({
        "DATABASE_URL": f"sqlite:///{(tmp / 'demo.db').as_posix()}",
        "STORAGE_DIR": str(tmp / "storage"),
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "RELOAD": "false",
        "DB_PROBE_INTERVAL": "0",
        "DB_FALLBACK_ENABLED": "false",
        "ALLOW_REGISTER": "1",
        "SECRET_KEY": secrets.token_hex(32),
    })

    from app import security, settings_store
    from app.config import settings
    from app.db import SessionLocal, engine, ensure_schema

    ensure_schema(engine)
    with SessionLocal() as db:
        info = seed(db, settings, security, settings_store)
        # 注册页截图需要「允许注册」；种子数据里没写这条，显式打开
        settings_store.set_value(db, "allow_register", "1")
        db.commit()
        cookie = security.create_token(info["user"].id, info["user"].token_version)
        print(f"种子数据就绪：{settings.DATABASE_URL}")

    # 上传演示用的大文件（40 MB 随机内容，保证截到进度条）
    demo_upload = tmp / "演示素材.zip"
    demo_upload.write_bytes(secrets.token_bytes(40 * 1024 * 1024))

    log_path = tmp / "server.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "main.py"], cwd=str(ROOT), env=os.environ.copy(),
        stdout=log_file, stderr=subprocess.STDOUT,
    )

    from playwright.sync_api import sync_playwright

    shots: list[tuple[str, Path]] = []
    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来")
        raw_dir = tmp / "raw"
        raw_dir.mkdir()
        OUT.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(headless=True)

            def new_page():
                ctx = browser.new_context(
                    viewport={"width": DESK_W, "height": DESK_H},
                    device_scale_factor=2,
                    locale="zh-CN",
                    color_scheme="dark",
                )
                ctx.add_cookies([{"name": settings.COOKIE_NAME, "value": cookie, "url": base}])
                page = ctx.new_page()
                page.add_init_script("try { localStorage.setItem('cloudnetp.lang', 'zh-CN'); } catch (e) {}")
                return ctx, page

            def shot(page, name: str) -> None:
                path = raw_dir / f"{name}.png"
                page.wait_for_timeout(350)
                page.screenshot(path=str(path))
                shots.append((name, path))
                print(f"  截到 {name}.png")

            # 1. 注册页签 + 「已阅读并同意」勾选行（未登录访客）
            guest = browser.new_context(
                viewport={"width": DESK_W, "height": DESK_H}, device_scale_factor=2,
                locale="zh-CN", color_scheme="dark",
            )
            gpage = guest.new_page()
            gpage.goto(base, wait_until="domcontentloaded")
            gpage.wait_for_selector(".auth-card", timeout=20000)
            gpage.click('[data-mode="register"]')
            gpage.wait_for_selector(".auth-agree", timeout=10000)
            gpage.wait_for_timeout(600)
            shot(gpage, "register")
            guest.close()

            ctx, page = new_page()
            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_selector(".file-grid .tile", timeout=20000)
            page.wait_for_timeout(1200)

            # 2. 右键菜单
            page.click('.tile:has-text("待办.md")', button="right")
            page.wait_for_selector(".popover", timeout=10000)
            shot(page, "ctx-menu")

            # 3. 分享弹窗
            page.click(".popover .menu-item:has-text('分享链接')")
            page.wait_for_selector(".dialog", timeout=10000)
            page.wait_for_timeout(400)
            shot(page, "share-dialog")

            # 4. 分享结果
            page.click(".dialog__actions button:has-text('创建链接')")
            page.wait_for_selector(".share-result", timeout=10000)
            page.wait_for_timeout(400)
            shot(page, "share-result")
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

            # 5. 我的分享列表
            page.goto(f"{base}/#/shares", wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            shot(page, "shares")

            # 6. 回收站（先删一个文件让它有内容）
            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_selector(".file-grid .tile", timeout=20000)
            page.wait_for_timeout(800)
            page.click('.tile:has-text("读书清单.csv")', button="right")
            page.wait_for_selector(".popover", timeout=10000)
            page.click(".popover .menu-item:has-text('删除')")
            page.wait_for_timeout(600)
            confirm = page.locator(".dialog__actions .btn")
            if confirm.count():
                confirm.last.click()
            page.wait_for_timeout(800)
            page.goto(f"{base}/#/trash", wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            shot(page, "trash")

            # 7. 上传进度面板
            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_selector(".file-grid .tile", timeout=20000)
            page.wait_for_timeout(800)
            # 每片拖延一点，进度条才不会在截图前就跑完
            page.route("**/api/uploads/**", lambda route: (time.sleep(0.25), route.continue_()))
            page.set_input_files("input[data-file-input]", str(demo_upload))
            page.wait_for_selector(".upload-panel .upload-item, .upload-panel [data-upload-list] .upload-item, .upload-panel__list > *", timeout=10000)
            page.wait_for_timeout(900)
            shot(page, "upload")
            page.wait_for_timeout(6000)
            page.unroute("**/api/uploads/**")

            # 8. 看板娘 AI 对话面板
            page.eval_on_selector('[data-mascot-act="chat"]', "el => el.click()")
            page.wait_for_selector("#mascot-chat", timeout=10000)
            page.wait_for_timeout(1200)
            shot(page, "mascot-chat")
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

            # 9. 设置页 AI 配置卡（滚到卡片再截）
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector(".ai-prompt", timeout=20000)
            page.locator(".ai-prompt").scroll_into_view_if_needed()
            page.wait_for_timeout(600)
            shot(page, "ai-config")

            # 10. 管理后台：用户 / 审计日志 / 系统设置
            page.goto(f"{base}/#/admin", wait_until="domcontentloaded")
            page.wait_for_selector(".admin-tabs", timeout=20000)
            page.click('[data-tab="users"]')
            page.wait_for_timeout(1400)
            shot(page, "admin-users")
            page.click('[data-tab="logs"]')
            page.wait_for_timeout(1400)
            shot(page, "admin-logs")
            page.click('[data-tab="settings"]')
            page.wait_for_timeout(1400)
            # 系统设置页很长，滚到「数据库」卡片单独截；看板娘会挡住右下按钮，先藏起来
            page.wait_for_selector('[data-db-act="refresh"]', timeout=20000)
            page.add_style_tag(content="#mascot{display:none!important}")
            db_card = page.evaluate_handle(
                "() => document.querySelector('[data-db-act=\"refresh\"]').closest('.settings-card')"
            ).as_element()
            db_card.scroll_into_view_if_needed()
            page.wait_for_timeout(700)
            db_path = raw_dir / "admin-db.png"
            db_card.screenshot(path=str(db_path))
            shots.append(("admin-db", db_path))
            print("  截到 admin-db.png")
            ctx.close()
            browser.close()

        # README 已有的成图直接复制进 Wiki
        for name in REUSE:
            src = ROOT / "docs" / "screenshots" / f"{name}.jpg"
            if src.exists():
                shutil.copyfile(src, OUT / f"{name}.jpg")
                print(f"  复用 {name}.jpg")

        # 压到固定宽度
        from PIL import Image

        for name, path in shots:
            target_w = 1600
            img = Image.open(path).convert("RGB")
            if img.width > target_w:
                img = img.resize((target_w, round(img.height * target_w / img.width)), Image.LANCZOS)
            out = OUT / f"{name}.jpg"
            img.save(out, format="JPEG", quality=90, optimize=True, progressive=True)
            print(f"  {out.relative_to(ROOT)}  {img.width}×{img.height}  {out.stat().st_size // 1024} KB")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        log_file.close()
        if KEEP:
            print(f"临时目录保留：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
