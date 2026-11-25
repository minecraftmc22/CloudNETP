# -*- coding: utf-8 -*-
"""超长站点名称的布局回归（桌面 + 手机两套宽度）。

后台「系统设置 → 站点 → 站点名称」允许最长 64 字符；历史上名称一长，
标题栏的 .brand__name（white-space:nowrap 又没有溢出裁剪）会把右侧的
消息 / 主题 / 头像按钮挤出屏幕。本脚本把站点名设成 64 字上限，然后断言：

- 手机（390px）：页面无横向溢出，标题栏动作按钮仍在视口内，站名被省略号截断
- 桌面（1280px）：站名完整显示，无横向溢出
- 「关于」页（手机）：大标题正常折行，卡片不被撑破

用法（先在隔离环境起 uvicorn，再跑本脚本）：
    set DATABASE_URL=sqlite:///D:/tmp_cn_brand/dev.db
    set STORAGE_DIR=D:/tmp_cn_brand/storage
    python -m uvicorn app.main:app --port 8137
    python scripts/brand_ui.py 8137
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402
from app.security import create_token, hash_password  # noqa: E402
from app.settings_store import set_value  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8137
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
W, H = 1280, 800
MW, MH = 390, 844
SHOTS = Path("D:/tmp_cn_brand/shots")

ADMIN = "brandadmin"
PASSWORD = "BrandUi123!"
# 64 字上限的站名（AdminSettingsIn.site_name max_length=64）
LONG_NAME = "超长站点名称测试" * 8

CHECKS: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> None:
    CHECKS.append((bool(ok), msg))


def seed() -> str:
    """建管理员账号并把站点名改成 64 字上限，返回登录 token。"""
    with SessionLocal() as db:
        row = db.query(User).filter(User.username == ADMIN).first()
        if row is None:
            row = User(
                username=ADMIN,
                password_hash=hash_password(PASSWORD),
                email="brandadmin@example.com",
                email_verified=True,
                role=1,
                is_super=True,
                token_version=1,
            )
            db.add(row)
        set_value(db, "site_name", LONG_NAME)
        db.commit()
        db.refresh(row)
        return create_token(row.id, row.token_version)


def main() -> int:
    token = seed()
    SHOTS.mkdir(parents=True, exist_ok=True)
    profile = tempfile.mkdtemp(prefix="brand-ui-")
    proc = subprocess.Popen(
        [
            CHROME,
            f"--remote-debugging-port=9481",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--window-size={W},{H}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(80):
            try:
                pages = [
                    t
                    for t in json.load(
                        urllib.request.urlopen("http://127.0.0.1:9481/json/list", timeout=2)
                    )
                    if t.get("type") == "page"
                ]
                if pages:
                    target = pages[0]
                    break
            except Exception:
                time.sleep(0.4)
        assert target, "无法连接 Chrome CDP"
        ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=60, suppress_origin=True
        )
        mid = 0

        def send(method, **params):
            nonlocal mid
            mid += 1
            ws.send(json.dumps({"id": mid, "method": method, "params": params}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error']}")
                    return msg.get("result", {})

        def js(expr):
            return send(
                "Runtime.evaluate", expression=expr, returnByValue=True, userGesture=True
            )["result"].get("value")

        def wait_for(expr, timeout=25):
            end = time.time() + timeout
            while time.time() < end:
                try:
                    if js(expr):
                        return True
                except Exception:
                    pass
                time.sleep(0.25)
            return False

        def shot(name):
            data_ = send("Page.captureScreenshot", format="png")
            (SHOTS / name).write_bytes(base64.b64decode(data_["data"]))

        send("Page.enable")
        send("Runtime.enable")
        send("Network.enable")
        send(
            "Network.setCookie",
            name=settings.COOKIE_NAME,
            value=token,
            domain="127.0.0.1",
            path="/",
            httpOnly=True,
        )

        # ================= 桌面 =================
        send(
            "Emulation.setDeviceMetricsOverride",
            width=W,
            height=H,
            deviceScaleFactor=1,
            mobile=False,
        )
        send("Page.navigate", url=f"{BASE}/#/files")
        assert wait_for("!!document.querySelector('.titlebar__brand')", 30), "主界面没渲染"
        # meta 是异步拉取的，等站名真的写进标题栏再量（否则量到静态占位符 CloudNETP）
        assert wait_for(
            f"document.getElementById('brand-name').textContent === '{LONG_NAME}'", 15
        ), "站名没写进标题栏"
        time.sleep(0.8)

        desk = js(
            """
            (() => {
              const name = document.getElementById('brand-name');
              const actions = document.querySelector('.titlebar__actions');
              return {
                text: name ? name.textContent : '',
                overflowY: name ? name.scrollWidth : 0,
                clientW: name ? name.clientWidth : 0,
                ellipsis: name ? getComputedStyle(name).textOverflow : '',
                actionsRight: actions ? actions.getBoundingClientRect().right : 0,
                docOverflow: document.documentElement.scrollWidth,
              };
            })()
            """
        )
        check(desk["text"] == LONG_NAME, f"桌面站名已写入（{desk['text'][:12]}…）")
        check(desk["docOverflow"] <= W, f"桌面无横向溢出（{desk['docOverflow']}≤{W}）")
        check(desk["ellipsis"] == "ellipsis", f"桌面站名带省略号策略（{desk['ellipsis']}）")
        check(desk["actionsRight"] <= W, f"桌面动作按钮在视口内（{desk['actionsRight']}）")
        shot("desktop.png")

        # ================= 手机 =================
        send(
            "Emulation.setDeviceMetricsOverride",
            width=MW,
            height=MH,
            deviceScaleFactor=2,
            mobile=True,
        )
        send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        send("Page.navigate", url=f"{BASE}/?r=brand#/files")
        assert wait_for("!!document.querySelector('.titlebar__brand')", 30), "手机端没渲染"
        assert wait_for(
            f"document.getElementById('brand-name').textContent === '{LONG_NAME}'", 15
        ), "手机站名没写进标题栏"
        time.sleep(0.8)

        mob = js(
            """
            (() => {
              const name = document.getElementById('brand-name');
              const brand = document.querySelector('.titlebar__brand');
              const actions = document.querySelector('.titlebar__actions');
              const bar = document.querySelector('.titlebar');
              return {
                text: name ? name.textContent : '',
                clipped: name ? name.scrollWidth > name.clientWidth : false,
                brandW: brand ? brand.getBoundingClientRect().width : 0,
                actionsRight: actions ? actions.getBoundingClientRect().right : 0,
                actionsLeft: actions ? actions.getBoundingClientRect().left : 0,
                barRight: bar ? bar.getBoundingClientRect().right : 0,
                docOverflow: document.documentElement.scrollWidth,
                innerW: window.innerWidth,
              };
            })()
            """
        )
        check(mob["text"] == LONG_NAME, "手机站名已写入")
        check(mob["clipped"] is True, f"手机站名被截断（scroll {mob.get('brandW')}px 内）")
        check(mob["docOverflow"] <= MW + 1, f"手机无横向溢出（{mob['docOverflow']}≤{MW}）")
        check(
            mob["actionsLeft"] < mob["actionsRight"] <= MW,
            f"动作按钮没被挤出屏幕（右缘 {mob['actionsRight']}）",
        )
        check(mob["actionsLeft"] > mob["brandW"], f"动作按钮与站名不重叠（{mob['actionsLeft']}>{mob['brandW']}）")
        shot("mobile-files.png")

        # 手机「关于」页：大标题折行（SPA 内跳转走 location.hash，别整页 reload）
        js("location.hash = '#/about'")
        assert wait_for("!!document.querySelector('.about-hero__title h2')", 30), "关于页没渲染"
        time.sleep(1.0)
        about = js(
            """
            (() => {
              const h2 = document.querySelector('.about-hero__title h2');
              const r = h2 ? h2.getBoundingClientRect() : null;
              return {
                text: h2 ? h2.textContent : '',
                docOverflow: document.documentElement.scrollWidth,
                h2W: r ? r.width : 0,
                h2Lines: h2 ? Math.round(h2.getBoundingClientRect().height / parseFloat(getComputedStyle(h2).lineHeight)) : 0,
              };
            })()
            """
        )
        check(about["docOverflow"] <= MW + 1, f"关于页无横向溢出（{about['docOverflow']}≤{MW}）")
        check(about["h2Lines"] >= 2, f"关于页大标题折行（{about['h2Lines']} 行）")
        shot("mobile-about.png")

        # 手机后台系统设置页也要能看（输入框本身可滚动，不算溢出）
        js("location.hash = '#/admin/settings'")
        assert wait_for("!!document.querySelector('.admin-body')", 30), "后台没渲染"
        time.sleep(1.2)
        admin = js(
            """
            (() => {
              const input = document.querySelector('[data-key="siteName"]');
              return {
                hasInput: !!input,
                value: input ? input.value : '',
                docOverflow: document.documentElement.scrollWidth,
              };
            })()
            """
        )
        check(admin["hasInput"], "后台站点名称输入框存在")
        check(admin["value"] == LONG_NAME, "后台回显长站名")
        check(admin["docOverflow"] <= MW + 1, f"后台无横向溢出（{admin['docOverflow']}≤{MW}）")
        shot("mobile-admin-settings.png")

    finally:
        proc.terminate()

    ok = all(c for c, _ in CHECKS)
    print()
    for good, msg in CHECKS:
        print(("PASS  " if good else "FAIL  ") + msg)
    print(f"\n{sum(c for c, _ in CHECKS)}/{len(CHECKS)} 通过；截图在 {SHOTS}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
