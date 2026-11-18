# -*- coding: utf-8 -*-
"""看板娘缩放 / 位置回归。

  1. 窄窗口（≤1100px）上滚轮真的能改大小（以前被断点里写死的 px 吃掉）
  2. 跨过 1100px 时尺寸连续（以前 190 → 232 突然一跳）
  3. 反复放大缩小，位置不累积漂移（贴角时 dx 必须一直是 0）
  4. 拖到屏幕中间后缩放：中心占屏比例不变（原地变大变小）
  5. 浏览器页面缩放（改视口 CSS 宽度）：位置比例不变

    ./.venv/Scripts/python.exe scripts/mascot_zoom_ui.py
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
    print(log_path.read_text(encoding="utf-8", errors="replace")[-1200:])
    return False


MEASURE = """
() => {
  const m = document.querySelector('.mascot');
  if (!m) return null;
  const r = m.getBoundingClientRect();
  const W = window.innerWidth, H = window.innerHeight;
  return {
    w: Math.round(r.width * 10) / 10,
    left: Math.round(r.left), top: Math.round(r.top),
    bottom: Math.round(r.bottom), right: Math.round(r.right),
    cxr: +(((r.left + r.width / 2) / W).toFixed(4)),
    cyr: +(((r.top + r.height / 2) / H).toFixed(4)),
    W, H,
    scale: Number(getComputedStyle(document.documentElement).getPropertyValue('--mascot-scale')) || 1,
    dx: getComputedStyle(document.documentElement).getPropertyValue('--mascot-dx').trim(),
    dy: getComputedStyle(document.documentElement).getPropertyValue('--mascot-dy').trim(),
  };
}
"""

WHEEL = """
(dir) => {
  const host = document.querySelector('.mascot');
  for (let i = 0; i < 3; i++)
    host.dispatchEvent(new WheelEvent('wheel', {deltaY: dir * 100, bubbles: true, cancelable: true}));
}
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-mascotzoom-")).resolve()
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
    with SessionLocal() as db:
        who = User(
            username="mascotzoom",
            password_hash=hash_password("Passw0rd!2024"),
            email="mz@example.com",
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
        [str(VENV_PY), "main.py"],
        cwd=str(ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    from playwright.sync_api import sync_playwright

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来")
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            ctx = browser.new_context(viewport={"width": 1093, "height": 640})
            ctx.add_cookies(
                [{"name": "cloudnetp_session", "value": create_token(uid, ver), "url": base}]
            )
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_selector(".mascot", timeout=20000)
            page.wait_for_selector(".mascot.is-live", timeout=45000)
            page.wait_for_timeout(1200)

            def measure(tag: str = "") -> dict:
                page.wait_for_timeout(700)
                data = page.evaluate(MEASURE)
                if tag:
                    print(f"  {tag:14s}", data)
                return data

            print("\n[A] 窄窗口（1093 ≤ 1100）滚轮要能改大小")
            base_rect = measure("起始")
            page.evaluate(WHEEL, -1)
            bigger = measure("放大档")
            check("滚轮确实改了缩放值", bigger["scale"] > base_rect["scale"], f"{base_rect['scale']} → {bigger['scale']}")
            check(
                "尺寸跟着缩放走（没被断点写死）",
                bigger["w"] > base_rect["w"] + 3,
                f"{base_rect['w']} → {bigger['w']}",
            )
            page.evaluate(WHEEL, 1)
            back = measure("缩回档")
            check("缩回来尺寸也回去", abs(back["w"] - base_rect["w"]) <= 2, f"{back['w']} vs {base_rect['w']}")

            print("\n[B] 反复放大缩小 6 轮：贴角位置不许漂")
            worst = 0.0
            for _ in range(6):
                page.evaluate(WHEEL, -1)
                page.wait_for_timeout(600)
                page.evaluate(WHEEL, 1)
                settled = measure()
                worst = max(worst, abs(settled["cxr"] - back["cxr"]))
            final = measure()
            check("中心比例没漂", worst <= 0.004, f"最大偏差 {worst:.4f}")
            check("贴角时 dx 一直是 0", final["dx"] == "0px" and final["dy"] == "0px", f"dx={final['dx']} dy={final['dy']}")

            print("\n[C] 跨过 1100px 断点：尺寸必须连续")
            for_zoom_out = None
            page.set_viewport_size({"width": 1214, "height": 711})
            wide = measure("1214 宽")
            page.set_viewport_size({"width": 1093, "height": 640})
            narrow = measure("1093 宽")
            ratio = wide["w"] / narrow["w"]
            check("跨断点尺寸不跳（比值≈1）", 0.92 <= ratio <= 1.08, f"{narrow['w']} vs {wide['w']} → {ratio:.3f}")
            for_zoom_out = wide

            print("\n[D] 拖到屏幕中间再缩放：围着原地长")
            page.evaluate(
                "() => { localStorage.setItem('mascot-pos', JSON.stringify({dx: -700, dy: -120})); location.reload(); }"
            )
            page.wait_for_selector(".mascot.is-live", timeout=45000)
            page.wait_for_timeout(1600)
            dragged = measure("拖好后")
            page.evaluate(WHEEL, -1)
            up = measure("放大后")
            page.evaluate(WHEEL, 1)
            down = measure("缩回后")
            check("放大时中心不动", abs(up["cxr"] - dragged["cxr"]) <= 0.004 and abs(up["cyr"] - dragged["cyr"]) <= 0.004,
                  f"{dragged['cxr']}/{dragged['cyr']} → {up['cxr']}/{up['cyr']}")
            check("缩回后回到原位", abs(down["cxr"] - dragged["cxr"]) <= 0.004 and abs(down["cyr"] - dragged["cyr"]) <= 0.004,
                  f"{down['cxr']}/{down['cyr']}")
            page.screenshot(path=str(OUT / "mascot_zoom_dragged.png"), clip={"x": 0, "y": 120, "width": 520, "height": 480})

            print("\n[E] 模拟浏览器页面缩放（90% / 110%）位置比例不变")
            for w, h, tag in ((1214, 711, "90%"), (993, 582, "110%"), (1093, 640, "100%")):
                page.set_viewport_size({"width": w, "height": h})
                now = measure(f"{tag}")
                check(
                    f"[{tag}] 中心占屏比例不变",
                    abs(now["cxr"] - dragged["cxr"]) <= 0.006 and abs(now["cyr"] - dragged["cyr"]) <= 0.006,
                    f"{now['cxr']}/{now['cyr']} vs {dragged['cxr']}/{dragged['cyr']}",
                )

            check("全程没有 JS 报错", not errors, str(errors[:1]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_file.close()

    print(f"\n通过 {PASS} / {PASS + FAIL}，截图在 {OUT}")
    return 1 if FAIL else 0


sys.exit(main())
