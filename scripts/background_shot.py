# -*- coding: utf-8 -*-
"""自定义背景 + 图片取色 UI 验证：走真实上传链路并截图。

用法：python scripts/background_shot.py [port]
"""
from __future__ import annotations

import base64
import colorsys
import io
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from app import db_failover  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import User, UserSetting  # noqa: E402
from app.security import create_token  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)


def make_wallpaper(path: Path, bright: bool = False) -> tuple:
    """一张主色明显偏青绿的假壁纸，用来验证取色结果。

    bright=True 换成高亮风格（接近白天的天空 + 沙滩），专门压测暗色主题下
    导航 / 标题栏文字在最坏情况下还读不读得出来。
    """
    w, h = 1600, 1000
    base = (208, 232, 250) if bright else (38, 132, 120)
    img = Image.new("RGB", (w, h), base)
    draw = ImageDraw.Draw(img)
    for i in range(60):
        y = int(h * i / 60)
        shade = 1 + 0.12 * (i / 60) if bright else 1 - 0.35 * (i / 60)
        draw.line(
            [(0, y), (w, y)],
            fill=tuple(min(255, int(c * shade)) for c in base),
        )
    if bright:
        draw.ellipse((w * 0.62, h * 0.06, w * 0.9, h * 0.34), fill=(255, 253, 240))
        draw.rectangle((0, h * 0.84, w, h), fill=(240, 226, 200))
    else:
        draw.ellipse((w * 0.62, h * 0.08, w * 0.96, h * 0.42), fill=(250, 246, 232))
        draw.rectangle((0, h * 0.82, w, h), fill=(24, 30, 34))
    img.save(path, "PNG")
    return w, h


def hex_hue(hex_color: str) -> float:
    r = int(hex_color[1:3], 16) / 255
    g = int(hex_color[3:5], 16) / 255
    b = int(hex_color[5:7], 16) / 255
    return colorsys.rgb_to_hsv(r, g, b)[0] * 360


def main() -> None:
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    admin = db.query(User).filter(User.role == 1).order_by(User.id.asc()).first()
    token = create_token(admin.id, admin.token_version)
    row = db.get(UserSetting, admin.id)
    backup = (
        (row.background_image, row.wallpaper, row.accent_color, row.theme_mode)
        if row
        else (None, "sakura", "#c084fc", 2)
    )
    db.close()

    tmp_png = Path(tempfile.gettempdir()) / "cn_test_wallpaper.png"
    make_wallpaper(tmp_png, bright="--bright" in sys.argv)

    profile = Path(tempfile.mkdtemp(prefix="cdp_bg_"))
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=9451",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--hide-scrollbars",
            "--window-size=1280,1000",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(60):
            try:
                pages = [
                    t
                    for t in json.load(urllib.request.urlopen("http://127.0.0.1:9451/json/list", timeout=2))
                    if t.get("type") == "page"
                ]
                if pages:
                    target = pages[0]
                    break
            except Exception:
                time.sleep(0.4)
        assert target, "无法连接 Chrome CDP"

        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=90, suppress_origin=True)
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
            res = send("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True,
                       userGesture=True)
            return res["result"].get("value")

        def shot(name):
            res = send("Page.captureScreenshot", format="png")
            p = OUT / name
            p.write_bytes(base64.b64decode(res["data"]))
            print("截图", p)

        def wait_for(expr, timeout=25):
            end = time.time() + timeout
            while time.time() < end:
                if js(expr):
                    return True
                time.sleep(0.3)
            return False

        send("Page.enable")
        send("Runtime.enable")
        send("DOM.enable")
        send(
            "Emulation.setDeviceMetricsOverride", width=1280, height=1000, deviceScaleFactor=1, mobile=False
        )
        send("Network.setCookie", name=settings.COOKIE_NAME, value=token,
             domain="127.0.0.1", path="/", httpOnly=True)
        send("Page.addScriptToEvaluateOnNewDocument",
             source="window.__errs=[];addEventListener('error',e=>window.__errs.push(String(e.message)));")

        send("Page.navigate", url=f"{BASE}/#/settings")
        assert wait_for("!!document.querySelector('[data-bg-tile]')", 30), "设置页没渲染"
        time.sleep(0.8)
        print("取色块:", js("!!document.querySelector('[data-swatch-pick] input[type=color]')"))
        print("自定义壁纸块:", js("document.querySelector('[data-bg-tile]').innerText.trim()"))
        print("取色按钮禁用:", js("document.querySelector('[data-act=pick-bg]').disabled"))
        shot("bg_settings_before.png")

        # 走真实上传链路：设置隐藏 input 的文件
        doc = send("DOM.getDocument", depth=-1)
        node = send("DOM.querySelector", nodeId=doc["root"]["nodeId"], selector="[data-bg-input]")
        print("file input nodeId:", node.get("nodeId"))
        send("DOM.setFileInputFiles", nodeId=node["nodeId"], files=[str(tmp_png)])
        ok = wait_for("document.documentElement.dataset.wallpaper === 'custom'", 40)
        print("上传后切到 custom:", ok)
        time.sleep(2.2)
        info = js("""(() => ({
          wp: document.documentElement.dataset.wallpaper,
          img: getComputedStyle(document.documentElement).getPropertyValue('--bg-image').trim(),
          accent: getComputedStyle(document.documentElement).getPropertyValue('--accent-user').trim(),
          active: [...document.querySelectorAll('.wallpaper-opt')].map(b => [b.dataset.wallpaper, b.classList.contains('is-active')]),
          actions: !document.querySelector('[data-bg-actions]').hidden,
          thumb: (document.querySelector('[data-bg-thumb]').style.backgroundImage || '').slice(0, 40),
          toast: document.querySelector('.toast') ? document.querySelector('.toast').innerText.replace(/\\n/g, ' ') : null,
          errs: window.__errs,
        }))()""")
        print("界面状态:", json.dumps(info, ensure_ascii=False))
        shot("bg_settings_after.png")

        accent = (info.get("accent") or "").strip()
        if accent:
            print("取色结果:", accent, "色相 %.1f°" % hex_hue(accent))
        server = js("fetch('/api/me').then(r=>r.json()).then(d=>({wp:d.user.settings.wallpaper,"
                    "bg:d.user.settings.backgroundImage, acc:d.user.settings.accentColor}))")
        print("服务端已存:", json.dumps(server, ensure_ascii=False))

        # 换回预设主题色，确认自定义色块退出选中态
        js("[...document.querySelectorAll('[data-color]')].find(b=>b.dataset.color==='#34d399').click()")
        time.sleep(1.2)
        print("预设改色后:", js("""(() => ({
          accent: getComputedStyle(document.documentElement).getPropertyValue('--accent-user').trim(),
          pickActive: document.querySelector('[data-swatch-pick]').classList.contains('is-active'),
        }))()"""))

        # 取色器：模拟拖动 + 松手，确认本地即时生效并落库
        js("""(() => {
          const i = document.querySelector('[data-color-pick]');
          i.value = '#ff8800';
          i.dispatchEvent(new Event('input', { bubbles: true }));
        })()""")
        time.sleep(0.5)
        print("拖动中即时预览:", js(
            "getComputedStyle(document.documentElement).getPropertyValue('--accent-user').trim()"))
        js("""(() => {
          const i = document.querySelector('[data-color-pick]');
          i.dispatchEvent(new Event('change', { bubbles: true }));
        })()""")
        time.sleep(1.6)
        print("松手后落库:", js("fetch('/api/me').then(r=>r.json()).then(d=>d.user.settings.accentColor)"))
        print("取色块状态:", js("""(() => {
          const s = document.querySelector('[data-swatch-pick]');
          return {active: s.classList.contains('is-active'), bg: s.style.background};
        })()"""))

        # 再从背景取色
        js("document.querySelector('[data-act=pick-bg]').click()")
        time.sleep(2.0)
        print("重新取色后:", js("""(() => ({
          accent: getComputedStyle(document.documentElement).getPropertyValue('--accent-user').trim(),
          pickActive: document.querySelector('[data-swatch-pick]').classList.contains('is-active'),
          toast: document.querySelector('.toast') ? document.querySelector('.toast').innerText.replace(/\\n/g,' ') : null,
        }))()"""))
        shot("bg_settings_picked.png")

        # 深色主题下看蒙层是否压得住
        js("document.querySelector('[data-mode=\"1\"]').click()")
        time.sleep(1.4)
        shot("bg_settings_dark.png")
        # 亮色主题
        js("document.querySelector('[data-mode=\"0\"]').click()")
        time.sleep(1.4)
        shot("bg_settings_light.png")

        # 窄屏
        send("Emulation.setDeviceMetricsOverride", width=390, height=880, deviceScaleFactor=2, mobile=False)
        send("Page.navigate", url=f"{BASE}/#/settings")
        wait_for("!!document.querySelector('[data-bg-tile]')", 30)
        time.sleep(1.2)
        shot("bg_settings_mobile.png")

        # 主界面（文件列表）也看一眼，确认背景不会把列表压花
        send("Emulation.setDeviceMetricsOverride", width=1280, height=900,
             deviceScaleFactor=1, mobile=False)
        send("Page.navigate", url=f"{BASE}/#/files")
        wait_for("document.querySelectorAll('.file-item, .file-card').length > 0", 30)
        time.sleep(1.5)
        shot("bg_files_list.png")

        print("最终错误:", js("window.__errs"))
        ws.close()
    finally:
        proc.terminate()
        # 还原测试账号的外观设置与文件
        db = db_failover.current_sessionmaker()()
        row = db.get(UserSetting, admin.id)
        if row:
            if row.background_image:
                p = settings.background_dir / row.background_image
                if p.is_file():
                    p.unlink()
            (
                row.background_image,
                row.wallpaper,
                row.accent_color,
                row.theme_mode,
            ) = backup
            db.commit()
        db.close()
        print("已还原设置与测试图片:", backup)


if __name__ == "__main__":
    main()
