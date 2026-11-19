# -*- coding: utf-8 -*-
"""管理后台「存储」页签的端到端验证（临时库 + 真实磁盘目录）。

  1. 页签与概况数字对得上：物理文件、无人引用、快传占用
  2. 手动「扫描并清理」只删真正没人引用的：有引用的、被举报拉黑的、刚写入的都留下
  3. 「立即执行一次」按当前配置跑：无人引用 + 失效快传一起回收
  4. 自动清理设置能存能读，重新进页面回显一致
  5. 定时器到点执行、同一天不重复执行
  6. 英文界面无残留中文、手机宽度不溢出、全程无 JS 报错

跑法：
    ./.venv/Scripts/python.exe scripts/storage_admin_ui.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-storage-admin-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'smoke.db').as_posix()}"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ["SECRET_KEY"] = "smoke-secret-key-for-storage-admin"
os.environ["DB_FALLBACK_ENABLED"] = "0"
os.environ["DB_PROBE_INTERVAL"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  OK   {name} {detail}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def hide_mascot(page) -> None:
    """看板娘画布常驻右下角，会挡住卡片底部的按钮，测试时先藏起来。"""
    page.evaluate(
        """() => {
            const style = document.createElement('style');
            style.id = 'hide-mascot';
            style.textContent = '#mascot{display:none!important}';
            document.head.appendChild(style);
        }"""
    )


# 造数据用的固定内容，sha 由内容算出来，清理后直接按 sha 判断在不在
KEEP = b"keep-me-referenced"
ORPHAN = b"orphan-old-bytes"
BLOCKED = b"blocked-original"
FRESH = b"fresh-upload"
TRANSFER = b"expired-transfer-bytes"
ORPHAN2 = b"orphan-before-run"
TICK = b"orphan-for-tick"

SHA = {name: hashlib.sha256(data).hexdigest() for name, data in {
    "keep": KEEP,
    "orphan": ORPHAN,
    "blocked": BLOCKED,
    "fresh": FRESH,
    "transfer": TRANSFER,
    "orphan2": ORPHAN2,
    "tick": TICK,
}.items()}


def write_blob(sha: str, data: bytes, *, hours_old: float = 0) -> None:
    from app import storage

    path = storage.ensure_blob_dirs(sha)
    path.write_bytes(data)
    if hours_old:
        stamp = time.time() - hours_old * 3600
        os.utime(path, (stamp, stamp))


def main() -> None:
    import uvicorn

    from app import cleanup_service as cs
    from app import db_failover, settings_store, storage
    from app.config import settings
    from app.main import app as fastapi_app
    from app.models import Blob, BlockedBlob, Node, QuickTransfer, User, utcnow
    from app.security import create_token, hash_password
    from app.user_service import ensure_settings

    port = free_port()
    cfg = uvicorn.Config(
        fastapi_app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        if server.started:
            break
        time.sleep(0.2)
    if not server.started:
        raise SystemExit("服务没起来")

    Factory = db_failover.current_sessionmaker()

    # ------------------------------------------------------------------ #
    # 造数据
    # ------------------------------------------------------------------ #
    with Factory() as db:
        admin = User(
            username="sakura",
            password_hash=hash_password("Cloud@2026"),
            display_name="樱花",
            role=1,
            is_super=True,
            quota_bytes=1024**3,
            token_version=1,
            created_at=datetime.utcnow(),
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        ensure_settings(db, admin.id)
        admin_id = admin.id

        def add_blob(sha: str, data: bytes) -> None:
            db.add(Blob(sha256=sha, size=len(data), ref_count=0))
            write_blob(sha, data)
            db.commit()

        # 有引用的：不清
        write_blob(SHA["keep"], KEEP)
        db.add(Blob(sha256=SHA["keep"], size=len(KEEP), ref_count=1))
        db.add(
            Node(
                user_id=admin_id,
                parent_id=None,
                type="file",
                name="keep.txt",
                blob_sha256=SHA["keep"],
                size=len(KEEP),
                mime="text/plain",
            )
        )
        db.commit()

        # 没人引用、放置 2 小时：该清
        add_blob(SHA["orphan"], ORPHAN)
        write_blob(SHA["orphan"], ORPHAN, hours_old=2)
        # 没人引用但被举报拉黑：留下原件供复核
        add_blob(SHA["blocked"], BLOCKED)
        write_blob(SHA["blocked"], BLOCKED, hours_old=2)
        db.add(BlockedBlob(sha256=SHA["blocked"], reason="测试", blocked_by=admin_id))
        db.commit()
        # 刚落盘不到 1 小时：手动清理要跳过
        add_blob(SHA["fresh"], FRESH)

        # 失效快传：过期 10 天的取件码 + 自己的副本
        write_blob(SHA["transfer"], TRANSFER)
        db.add(Blob(sha256=SHA["transfer"], size=len(TRANSFER), ref_count=1))
        node = Node(
            user_id=admin_id,
            parent_id=None,
            type="file",
            name="pickup.bin",
            blob_sha256=SHA["transfer"],
            size=len(TRANSFER),
            mime="application/octet-stream",
            quick_owned=True,
        )
        db.add(node)
        db.commit()
        db.refresh(node)
        transfer_node_id = node.id
        db.add(
            QuickTransfer(
                kind="pickup",
                owner_id=admin_id,
                token="smoke-pickup-token-" + str(int(time.time())),
                code="ABC123",
                node_ids=json.dumps([transfer_node_id]),
                name="pickup.bin",
                size=len(TRANSFER),
                count=1,
                expires_at=utcnow() - timedelta(days=10),
                created_at=utcnow() - timedelta(days=11),
            )
        )
        db.commit()

        # 立即执行一次时要被回收的无人引用文件
        add_blob(SHA["orphan2"], ORPHAN2)
        write_blob(SHA["orphan2"], ORPHAN2, hours_old=30)

        cookie = create_token(admin_id, admin.token_version)

    errors: list[str] = []
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1360, "height": 1100})
            ctx.add_cookies(
                [
                    {
                        "name": settings.COOKIE_NAME,
                        "value": cookie,
                        "domain": "127.0.0.1",
                        "path": "/",
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))

            page.goto(f"{base}/#/admin", wait_until="domcontentloaded")
            page.wait_for_selector("[data-tab='storage']", timeout=20000)
            hide_mascot(page)
            page.click("[data-tab='storage']")
            page.wait_for_selector("[data-card='cleanup']", timeout=20000)

            print("\n[A] 页签与概况")
            check("存储页签在", page.locator("[data-tab='storage']").count() == 1)
            text = page.inner_text("[data-body], .admin-body")
            check("四个统计卡都在", page.locator(".stat-card").count() == 4)
            check("有无引用统计卡", "无人引用" in text)
            api_state = page.request.get(f"{base}/api/admin/storage").json()
            st = api_state.get("storage") or {}
            unref = st.get("unreferenced") or {}
            check("物理文件数 = 造出来的 6 份", st.get("blobs") == 6, st.get("blobs"))
            check("无人引用 3 个（含刚写入的）", unref.get("count") == 3, unref)
            check("黑名单文件被保护", unref.get("blocked") == 1, unref.get("blocked"))
            check("快传占用有数字", (st.get("quickFiles") or 0) == 1, st.get("quickFiles"))
            check("存储目录回显", (st.get("path") or "").endswith("storage"), st.get("path"))

            print("\n[B] 手动清理无人引用的文件")
            page.click("[data-act='orphan-gc']")
            page.wait_for_selector(".dialog__body", timeout=20000)
            body = page.inner_text(".dialog__body")
            check("确认框报出 2 个可清（刚写入的除外）", "2" in body, body.replace("\n", " ")[:120])
            page.click(".dialog [data-act='ok']")
            page.wait_for_timeout(1500)

            with Factory() as db:
                left = {b.sha256 for b in db.query(Blob).all()}
            check("旧的无引用记录被删", SHA["orphan"] not in left)
            check("有引用的 blob 还在", SHA["keep"] in left)
            check("黑名单原件还在", SHA["blocked"] in left)
            check("刚落盘的还在", SHA["fresh"] in left)
            check("磁盘文件同步删除", not storage.blob_path(SHA["orphan"]).is_file())
            check("黑名单磁盘文件还在", storage.blob_path(SHA["blocked"]).is_file())
            check("有引用的磁盘文件还在", storage.blob_path(SHA["keep"]).is_file())

            page.wait_for_timeout(300)
            reread = page.request.get(f"{base}/api/admin/storage").json()["storage"]
            check("接口里的无人引用回到 1", reread["unreferenced"]["count"] == 1, reread["unreferenced"])

            print("\n[C] 立即执行一次（无人引用 + 失效快传）")
            # B 已经把原来的候选清空了，这里再造一条超期 30 小时的无引用文件，
            # 用来确认「立即执行」走的是 24 小时保护期、而不是复用页面上的旧数字
            write_blob(SHA["orphan2"], ORPHAN2, hours_old=30)
            with Factory() as db:
                db.add(Blob(sha256=SHA["orphan2"], size=len(ORPHAN2), ref_count=0))
                db.commit()
            page.click("[data-act='cleanup-run']")
            page.wait_for_timeout(2500)
            state = page.request.get(f"{base}/api/admin/storage").json()
            cleanup_cfg = state.get("cleanup") or {}
            check("lastRun 写入了", bool(cleanup_cfg.get("lastRun")), cleanup_cfg.get("lastRun"))
            result = cleanup_cfg.get("lastResult") or {}
            check("结果里带无人引用", (result.get("orphans") or {}).get("applied") is True, result.get("orphans"))
            check("结果里带快传", (result.get("quick") or {}).get("files", 0) >= 1, result.get("quick"))
            with Factory() as db:
                remaining = {b.sha256 for b in db.query(Blob).all()}
                transfer_node = db.get(Node, transfer_node_id)
                codes = db.query(QuickTransfer).filter(QuickTransfer.kind == "pickup").count()
            check("30 小时前的无引用文件被回收", SHA["orphan2"] not in remaining)
            check("失效快传的副本没了", transfer_node is None and SHA["transfer"] not in remaining)
            check("失效取件码记录也没了", codes == 0, codes)
            check("快传磁盘文件删了", not storage.blob_path(SHA["transfer"]).is_file())

            print("\n[D] 自动清理设置")
            page.evaluate(
                """() => {
                    document.querySelector('[data-cleanup-enabled]').click();
                    const sel = document.querySelector('[data-cleanup-hour]');
                    sel.value = '7';
                    document.querySelector('[data-cleanup-quick]').click();
                }"""
            )
            page.click("[data-act='cleanup-save']")
            page.wait_for_timeout(1200)
            saved = page.request.get(f"{base}/api/admin/storage").json()["cleanup"]
            check("开关已打开", saved.get("enabled") is True, saved.get("enabled"))
            check("时间是 7 点", saved.get("hour") == 7, saved.get("hour"))
            check("快传清理被关掉", saved.get("quick") is False, saved.get("quick"))
            check("无人引用仍开着", saved.get("orphans") is True, saved.get("orphans"))

            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("[data-tab='storage']", timeout=20000)
            hide_mascot(page)
            page.click("[data-tab='storage']")
            page.wait_for_selector("[data-card='cleanup']", timeout=20000)
            check(
                "重进页面开关回显",
                page.evaluate("() => document.querySelector('[data-cleanup-enabled]').checked") is True,
            )
            check(
                "重进页面时间回显",
                page.evaluate("() => document.querySelector('[data-cleanup-hour]').value") == "7",
            )
            page.screenshot(path=str(_TMP / "storage_desktop.png"), full_page=True)

            print("\n[E] 定时器到点执行且当天不重复")
            write_blob(SHA["tick"], TICK, hours_old=30)
            with Factory() as db:
                db.add(Blob(sha256=SHA["tick"], size=len(TICK), ref_count=0))
                db.commit()
                now = datetime.now()
                settings_store.set_value(db, "auto_cleanup_enabled", "1")
                settings_store.set_value(db, "auto_cleanup_hour", str(now.hour))
                settings_store.set_value(db, "auto_cleanup_orphans", "1")
                settings_store.set_value(db, "auto_cleanup_quick", "1")
                settings_store.set_value(db, "auto_cleanup_last_run", "")
                db.commit()

            cs.tick()
            with Factory() as db:
                gone = db.get(Blob, SHA["tick"]) is None
                first_run = settings_store.get_value(db, "auto_cleanup_last_run")
            check("到点执行删掉了候选文件", gone)
            check("记下了运行时间", bool(first_run), first_run)

            time.sleep(1.1)
            cs.tick()
            with Factory() as db:
                second_run = settings_store.get_value(db, "auto_cleanup_last_run")
            check("同一天不会重复执行", second_run == first_run, second_run)

            with Factory() as db:
                settings_store.set_value(db, "auto_cleanup_enabled", "0")
                db.commit()

            print("\n[F] 英文界面与手机宽度")
            page.evaluate(
                """async () => {
                    await fetch('/api/me/settings', {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ language: 'en-US' }),
                    });
                }"""
            )
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("[data-tab='storage']", timeout=20000)
            hide_mascot(page)
            page.click("[data-tab='storage']")
            page.wait_for_selector("[data-card='cleanup']", timeout=20000)
            en_text = page.inner_text(".admin-body")
            leftover = re.findall(r"[\u4e00-\u9fa5]+", en_text)
            check("英文页面没有残留中文", not leftover, leftover[:8])
            check("英文标题是 Auto cleanup", "Auto cleanup" in en_text)

            page.set_viewport_size({"width": 390, "height": 900})
            page.wait_for_timeout(600)
            box = page.evaluate(
                """() => {
                    const c = document.querySelector("[data-card='cleanup']");
                    const r = c.getBoundingClientRect();
                    return { right: Math.round(r.right), vw: window.innerWidth };
                }"""
            )
            check("手机宽度卡片不溢出", box["right"] <= box["vw"] + 1, box)
            page.locator("[data-card='cleanup']").screenshot(path=str(_TMP / "storage_mobile.png"))

            check("没有 JS 报错", not errors, "; ".join(errors[:3]))
            browser.close()
    finally:
        pass

    print(f"\n{'=' * 56}")
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for name in FAILED:
        print("  FAILED:", name)
    print("截图:", _TMP / "storage_mobile.png")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
