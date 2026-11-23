# -*- coding: utf-8 -*-
"""手机端 CDP娘 页回归：她把本体搬进页面、对话框贴在下面、离开时搬回右下角。

用法：
    python scripts/mascot_page_ui.py [port]

需要一个已启动的实例（建议用隔离库），脚本自己签管理员会话。
"""
from __future__ import annotations

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

from app import db_failover  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import User  # noqa: E402
from app.security import create_token  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
W, H = 390, 844

CHECKS: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> None:
    CHECKS.append((bool(ok), msg))


def admin_token() -> str:
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    try:
        admin = db.query(User).filter(User.role == 1).order_by(User.id.asc()).first()
        assert admin is not None, "没有管理员账号"
        return create_token(admin.id, admin.token_version)
    finally:
        db.close()


def main() -> None:
    token = admin_token()
    profile = Path(tempfile.mkdtemp(prefix="cdp_mascot_ui_"))
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=9478",
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
                    for t in json.load(urllib.request.urlopen("http://127.0.0.1:9478/json/list", timeout=2))
                    if t.get("type") == "page"
                ]
                if pages:
                    target = pages[0]
                    break
            except Exception:
                time.sleep(0.4)
        assert target, "无法连接 Chrome CDP"
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
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
            return send("Runtime.evaluate", expression=expr, returnByValue=True, userGesture=True)["result"].get("value")

        def wait_for(expr, timeout=30):
            end = time.time() + timeout
            while time.time() < end:
                try:
                    if js(expr):
                        return True
                except Exception:
                    pass
                time.sleep(0.3)
            return False

        send("Page.enable")
        send("Runtime.enable")
        send("Network.enable")
        send("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=2, mobile=True)
        send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        send("Network.setCookie", name=settings.COOKIE_NAME, value=token, domain="127.0.0.1", path="/", httpOnly=True)
        send("Page.navigate", url=f"{BASE}/#/mascot")
        assert wait_for("!!document.querySelector('.cdp-hero')", 40), "CDP娘 页没渲染"
        assert wait_for("!!document.querySelector('.mascot__canvas')", 40), "看板娘没加载出来"
        time.sleep(2.2)

        layout = js(
            """
            (() => {
              const q = (s) => document.querySelector(s);
              // DOMRect 序列化不出来，手动摊平成普通对象
              const box = (el) => {
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
              };
              const mascot = q('#mascot');
              const stage = q('.cdp-hero__stage');
              const panel = q('.mchat');
              const feed = q('.mchat__feed');
              const compose = q('.mchat__compose');
              const send = q('.mchat__send');
              const nav = q('.nav');
              const cs = (el) => getComputedStyle(el);
              return {
                embedded: !!mascot && mascot.parentElement === stage,
                hasClass: !!mascot && mascot.classList.contains('is-embedded'),
                canvases: document.querySelectorAll('canvas.mascot__canvas').length,
                mascotBox: box(mascot),
                stageBox: box(stage),
                canvasBox: box(q('.mascot__canvas')),
                mascotZ: mascot ? cs(mascot).zIndex : '',
                panelZ: panel ? cs(panel).zIndex : '',
                panelInsideHero: !!panel && panel.parentElement === q('.cdp-hero'),
                headHidden: !!panel && cs(q('.mchat__head')).display === 'none',
                feedGradient: feed ? cs(feed).backgroundImage.startsWith('linear-gradient') : false,
                feedPointer: feed ? cs(feed).pointerEvents : '',
                composeBox: box(compose),
                sendRight: send ? Math.round(box(send).right) : 0,
                navTop: nav ? Math.round(box(nav).top) : 0,
                iw: innerWidth,
                sw: document.documentElement.scrollWidth,
                greet: !!q('.mchat__hello'),
                newBtn: !!q('.cdp-new'),
                activeNav: q('.nav-item.is-active')?.dataset.route || '',
              };
            })()
            """
        )

        check(layout["embedded"], "#mascot 已搬进 .cdp-hero__stage")
        check(layout["hasClass"], "#mascot 带上 is-embedded")
        check(layout["canvases"] == 1, f"只有一个 Live2D 画布（{layout['canvases']}）")
        cb, sb = layout["canvasBox"], layout["stageBox"]
        check(
            cb and sb and abs(cb["width"] - sb["width"]) <= 2 and abs(cb["height"] - sb["height"]) <= 2,
            f"画布铺满舞台（canvas {cb and (round(cb['width']), round(cb['height']))} / stage {sb and (round(sb['width']), round(sb['height']))}）",
        )
        check(cb and cb["height"] > 320, f"看板娘有足够高度（{cb and round(cb['height'])}px）")
        check(
            int(layout["mascotZ"] or 0) < int(layout["panelZ"] or 0),
            f"看板娘压在对话框下面（z {layout['mascotZ']} < {layout['panelZ']}）",
        )
        check(layout["panelInsideHero"], "对话面板挂在模型区里")
        check(layout["headHidden"], "面板自己的页头已收起（页头用页面上的）")
        check(layout["feedGradient"], "对话区底部有渐隐底色（字压在她身上也看得清）")
        check(layout["feedPointer"] == "none", "对话区空白不挡点击（能戳到她）")
        compose = layout["composeBox"]
        check(
            compose and compose["bottom"] <= layout["navTop"] + 1,
            f"输入框在导航栏上方（compose.bottom={compose and round(compose['bottom'])} navTop={layout['navTop']}）",
        )
        check(compose and compose["height"] >= 60, f"输入框高度正常（{compose and round(compose['height'])}px）")
        check(W - layout["sendRight"] <= 24, f"发送按钮贴右（距右侧 {W - layout['sendRight']}px）")
        check(layout["iw"] == W and layout["sw"] <= W + 1, f"无横向溢出（iw={layout['iw']} sw={layout['sw']}）")
        check(layout["newBtn"], "右上角有「开启新对话」")
        check(layout["activeNav"] == "mascot", f"底部导航选中 CDP娘（{layout['activeNav']}）")

        # 输入框能打字、能长高，且长高后舞台要让位
        js("document.querySelector('.mchat__input').focus()")
        js(
            "(() => { const i=document.querySelector('.mchat__input');"
            " i.value='测试多行输入\\n第二行\\n第三行'; i.dispatchEvent(new Event('input',{bubbles:true})); })()"
        )
        time.sleep(0.8)
        grown = js(
            """
            (() => {
              const c = document.querySelector('.mchat__compose').getBoundingClientRect();
              const s = document.querySelector('.cdp-hero__stage').getBoundingClientRect();
              const n = document.querySelector('.nav').getBoundingClientRect();
              return { composeH: Math.round(c.height), stageBottom: Math.round(s.bottom), navTop: Math.round(n.top),
                       footVar: getComputedStyle(document.querySelector('.cdp-hero')).getPropertyValue('--cdp-foot').trim() };
            })()
            """
        )
        check(grown["composeH"] > 60, f"输入框随内容长高（{grown['composeH']}px）")
        check(grown["footVar"] != "", f"舞台让位变量已写入（--cdp-foot: {grown['footVar']}）")
        check(grown["stageBottom"] <= grown["navTop"], "输入框长高后模型区仍然不越过导航栏")
        js("(() => { const i=document.querySelector('.mchat__input'); i.value=''; i.blur(); })()")
        time.sleep(0.6)

        # 开启新对话：回到问候语
        js("document.querySelector('.cdp-new').click()")
        time.sleep(0.8)
        check(js("!!document.querySelector('.mchat__hello')"), "「开启新对话」回到问候语")

        # 离开这一页：她要搬回 body，恢复右下角悬浮
        js("location.hash = '#/files'")
        wait_for("!!document.querySelector('.file-grid, .frow, .empty')")
        time.sleep(1.2)
        back = js(
            """
            (() => {
              const m = document.querySelector('#mascot');
              return {
                parent: m ? m.parentElement.tagName : '',
                embedded: m ? m.classList.contains('is-embedded') : true,
                canvases: document.querySelectorAll('canvas.mascot__canvas').length,
                display: m ? getComputedStyle(m).display : '',
              };
            })()
            """
        )
        check(back["parent"] == "BODY", f"离开后 #mascot 回到 body（{back['parent']}）")
        check(not back["embedded"], "离开后不再带 is-embedded")
        check(back["canvases"] == 1, f"全程只有一个画布（{back['canvases']}）")

        # 再进去一次：能重新收养，且不会多出一个画布
        js("location.hash = '#/mascot'")
        assert wait_for("!!document.querySelector('.cdp-hero')", 30), "二次进入没渲染"
        time.sleep(1.6)
        again = js(
            """
            (() => {
              const m = document.querySelector('#mascot');
              const stage = document.querySelector('.cdp-hero__stage');
              return {
                embedded: !!m && m.parentElement === stage,
                canvases: document.querySelectorAll('canvas.mascot__canvas').length,
                chat: !!document.querySelector('.mchat.is-embedded'),
              };
            })()
            """
        )
        check(again["embedded"], "二次进入仍能把她搬进页面")
        check(again["canvases"] == 1, f"二次进入没有复制画布（{again['canvases']}）")
        check(again["chat"], "二次进入对话面板仍是内联形态")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            pass

    print()
    ok = sum(1 for passed, _ in CHECKS if passed)
    for passed, msg in CHECKS:
        print(("  ✓ " if passed else "  ✗ ") + msg)
    print(f"\n{ok}/{len(CHECKS)} 通过")
    raise SystemExit(0 if ok == len(CHECKS) else 1)


if __name__ == "__main__":
    main()
