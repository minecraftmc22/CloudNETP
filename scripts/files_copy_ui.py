# -*- coding: utf-8 -*-
"""「我的文件」复制功能回归（0.6.18）。

验证点：
- API：原地复制文件/文件夹（重名自动加序号）、blob 只加引用不复制物理数据、
  配额预检（空间不够时 400）、不存在的节点 404；
- 记账：used_bytes 按逻辑字节累加、blob ref_count 递增；
- UI：右键菜单有「复制」、点完列表出现副本、多选时工具栏有「复制」按钮。

    ./.venv/Scripts/python.exe scripts/files_copy_ui.py
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
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
    print(log_path.read_text(encoding="utf-8", errors="replace")[-1200:])
    return False


def png_bytes(w: int, h: int, color) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-copy-")).resolve()
    db_path = tmp / "ui.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["STORAGE_DIR"] = str(tmp / "storage")
    os.environ["DB_FALLBACK_ENABLED"] = "0"
    os.environ["DB_PROBE_INTERVAL"] = "0"
    os.environ["DB_MIRROR_INTERVAL"] = "0"

    sys.path.insert(0, str(ROOT))
    from sqlalchemy import select

    from app.config import settings
    from app import node_service as ns
    from app import storage
    from app.db import SessionLocal, engine, ensure_schema
    from app.models import Blob, User, utcnow
    from app.security import create_token, hash_password

    ensure_schema(engine)

    def put_blob(data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = storage.blob_path(sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return sha

    with SessionLocal() as db:
        user = User(
            username="copy_a",
            display_name="小拷",
            password_hash=hash_password("Passw0rd!2024"),
            email="copy_a@example.com",
            email_verified=True,
            role=0,
            # 配额只给 5KB：配合确定性大小精确触发「空间不足」分支
            # （1500×2+900×2 = 4800 ≤ 5120；再复制 1500 到 6300 > 5120）
            quota_bytes=5120,
            token_version=1,
            created_at=utcnow(),
        )
        db.add(user)
        db.commit()
        uid, token = user.id, create_token(user.id, user.token_version)

        file_a = os.urandom(1500)
        sha_a = put_blob(file_a)
        node_file = ns.create_file(db, user, None, "报告.png", sha_a, len(file_a), "image/png")

        folder = ns.create_folder(db, user, None, "资料")
        file_b = os.urandom(900)
        sha_b = put_blob(file_b)
        ns.create_file(db, user, folder.id, "说明.png", sha_b, len(file_b), "image/png")
        db.commit()
        file_id, folder_id = node_file.id, folder.id

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
        }
    )
    log_path = tmp / "server.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    def call(path: str, method: str = "GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, method=method, data=data)
        req.add_header("Content-Type", "application/json")
        req.add_header("Cookie", f"{settings.COOKIE_NAME}={token}")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    from playwright.sync_api import sync_playwright

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来")

        # ---- API 层 ----
        code, data = call(f"/api/nodes/{file_id}/copy", "POST")
        check("复制文件返回 201", code == 201, str(code))
        dup_name = (data.get("node") or {}).get("name", "")
        check("重名副本自动加序号", dup_name.startswith("报告") and dup_name != "报告.png", dup_name)

        with SessionLocal() as db:
            blobs = db.scalars(select(Blob).where(Blob.sha256 == sha_a)).all()
            check(
                "blob 仍只有一条（引用不复制物理数据）",
                len(blobs) == 1 and blobs[0].ref_count == 2,
                f"ref_count={blobs[0].ref_count if blobs else '?'}",
            )
            user_row = db.get(User, uid)
            check(
                "used_bytes 计入副本逻辑字节",
                user_row.used_bytes == len(file_a) * 2 + len(file_b),
                f"used={user_row.used_bytes}",
            )

        code, data = call(f"/api/nodes/{folder_id}/copy", "POST")
        check("复制文件夹返回 201", code == 201, str(code))
        dup_folder_id = (data.get("node") or {}).get("id")
        code, data = call(f"/api/nodes?parentId={dup_folder_id}")
        kids = [it["name"] for it in data.get("items", [])]
        check("文件夹副本带上了子文件", any("说明" in k for k in kids), str(kids))

        # 配额：此时 used = 2×file_a + 2×file_b ≈ 1.8KB，再复制文件会顶破 3KB
        code, data = call(f"/api/nodes/{file_id}/copy", "POST")
        check(
            "空间不足时 400 且带原因",
            code == 400 and "空间不足" in data.get("detail", ""),
            f"{code} {data.get('detail', '')[:40]}",
        )

        code, _ = call("/api/nodes/999999/copy", "POST")
        check("不存在的节点 404", code == 404, str(code))

        # ---- UI 层 ----
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            ctx = browser.new_context(viewport={"width": 1280, "height": 800})
            ctx.add_cookies([{"name": settings.COOKIE_NAME, "value": token, "url": base}])
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_selector("[data-id]", timeout=20000)
            page.wait_for_timeout(800)

            # 右键菜单里要有「复制」
            page.locator("[data-id]").first.click(button="right")
            page.wait_for_selector(".popover[role=menu]", timeout=5000)
            menu_text = page.evaluate(
                "() => (document.querySelector('.popover[role=menu]')?.textContent || '')"
            )
            check("右键菜单有「复制」", "复制" in menu_text)
            page.screenshot(path=str(OUT / "files_copy_menu.png"))

            # 点「复制」→ 列表多出一份副本
            page.evaluate(
                """() => [...document.querySelectorAll('.popover[role=menu] .menu-item')]
                      .find((el) => el.textContent.trim() === '复制')?.click()"""
            )
            page.wait_for_timeout(1500)
            body = page.evaluate(
                """() => [...document.querySelectorAll('.tile__name, .file-list [class*=name]')]
                       .map((el) => el.textContent).join('\\n')"""
            )
            check("点复制后列表出现「报告 (1).png」", "报告 (1).png" in body)
            check("点复制后列表出现「资料 (1)」", "资料 (1)" in body)

            # 多选 → 工具栏出现「复制」按钮
            page.locator("[data-id]").first.click()
            page.locator("[data-id]").nth(1).click(modifiers=["Control"])
            page.wait_for_selector('[data-act="sel-copy"]', timeout=5000)
            check("多选工具栏有「复制」按钮", True)
            page.screenshot(path=str(OUT / "files_copy_selbar.png"))

            check("全程无 JS 报错", not errors, "; ".join(errors[:2]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_file.close()

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
