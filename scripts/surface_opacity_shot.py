# -*- coding: utf-8 -*-
"""模块不透明度 UI 验证：滑块联动 / CSS 变量 / 弹窗 / 持久化 / 恢复默认。

用法：python scripts/surface_opacity_shot.py [port]
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

from app import db_failover  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import User, UserSetting  # noqa: E402
from app.security import create_token  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)


def alpha_of(color: str) -> float | None:
    """从 computed backgroundColor 里取 alpha（兼容 rgb() / rgba() / color(srgb ...)）。"""
    if not color:
        return None
    if color.startswith("color(") and "/" in color:
        try:
            return round(float(color.rsplit("/", 1)[1].strip(" )")), 3)
        except Exception:
            return None
    if color.startswith("color("):
        return 1.0
    if color.startswith("rgba"):
        try:
            return round(float(color.rsplit(",", 1)[1].strip(" )")), 3)
        except Exception:
            return None
    if color.startswith("rgb"):
        return 1.0
    return None


def main() -> None:
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    admin = db.query(User).filter(User.role == 1).order_by(User.id.asc()).first()
    token = create_token(admin.id, admin.token_version)
    row = db.get(UserSetting, admin.id)
    backup = dict(row.surface_opacity or {}) if row else {}
    db.close()

    profile = Path(tempfile.mkdtemp(prefix="cdp_op_"))
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=9453",
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
                    for t in json.load(urllib.request.urlopen("http://127.0.0.1:9453/json/list", timeout=2))
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

        def set_slider(key, value):
            js(f"""(() => {{
              const el = document.querySelector('[data-opacity="{key}"]');
              el.value = {value};
              el.dispatchEvent(new Event('input', {{ bubbles: true }}));
              el.dispatchEvent(new Event('change', {{ bubbles: true }}));
              return el.value;
            }})()""")

        def read_state():
            return js("""(() => {
              const cs = getComputedStyle(document.documentElement);
              const c = (sel) => getComputedStyle(document.querySelector(sel)).backgroundColor;
              const vars = {};
              for (const k of ['nav','titlebar','panel','dialog'])
                vars[k] = cs.getPropertyValue('--op-' + k).trim();
              const sliders = {};
              for (const el of document.querySelectorAll('[data-opacity]'))
                sliders[el.dataset.opacity] = el.value + '|' + document.querySelector(`[data-opacity-val="${el.dataset.opacity}"]`).textContent;
              return {
                vars, sliders,
                nav: c('.nav'), title: c('.titlebar'), panel: c('.view-root'),
                defaults: {
                  nav: cs.getPropertyValue('--nav-a').trim(),
                  title: cs.getPropertyValue('--title-a').trim(),
                  panel: cs.getPropertyValue('--panel-a').trim(),
                  dialog: cs.getPropertyValue('--dialog-a').trim(),
                },
                errs: window.__errs,
              };
            })()""")

        send("Page.enable")
        send("Runtime.enable")
        send("DOM.enable")
        send("Emulation.setDeviceMetricsOverride", width=1280, height=1000, deviceScaleFactor=1, mobile=False)
        send("Network.setCookie", name=settings.COOKIE_NAME, value=token,
             domain="127.0.0.1", path="/", httpOnly=True)
        send("Page.addScriptToEvaluateOnNewDocument",
             source="window.__errs=[];addEventListener('error',e=>window.__errs.push(String(e.message)));")

        send("Page.navigate", url=f"{BASE}/#/settings")
        assert wait_for("!!document.querySelector('[data-opacity-list]')", 30), "透明度控件没渲染"
        time.sleep(0.8)

        base = read_state()
        print("默认滑块:", json.dumps(base["sliders"], ensure_ascii=False))
        print("默认浓度:", json.dumps(base["defaults"], ensure_ascii=False))
        print("默认计算值:", {k: alpha_of(base[k]) for k in ("nav", "title", "panel")})
        shot("op_settings_default.png")

        # 调成很极端的一组：侧栏全透明、标题栏全实、面板 22%、弹窗 55%
        for key, val in (("nav", 0), ("titlebar", 100), ("panel", 22), ("dialog", 55)):
            set_slider(key, val)
            time.sleep(0.35)
        time.sleep(1.4)

        st = read_state()
        print("调整后滑块:", json.dumps(st["sliders"], ensure_ascii=False))
        print("调整后变量:", json.dumps(st["vars"], ensure_ascii=False))
        print("调整后计算值:", {k: alpha_of(st[k]) for k in ("nav", "title", "panel")})

        # 临时塞一个对话框进来量它的底色
        dialog_bg = js("""(() => {
          const host = document.createElement('div');
          host.className = 'dialog-host is-open';
          host.id = 'op-probe';
          host.innerHTML = '<div class="dialog-backdrop"></div><div class="dialog">探针</div>';
          document.body.appendChild(host);
          const bg = getComputedStyle(host.querySelector('.dialog')).backgroundColor;
          host.remove();
          return bg;
        })()""")
        print("弹窗底色:", dialog_bg, "alpha =", alpha_of(dialog_bg))
        shot("op_settings_custom.png")

        # 文件页看效果
        js("location.hash = '#/files'")
        time.sleep(1.6)
        shot("op_files_custom.png")

        # 刷新确认持久化（先回设置页，reload 会保留 hash）
        js("location.hash = '#/settings'")
        time.sleep(0.8)
        send("Page.reload")
        assert wait_for("!!document.querySelector('[data-opacity-list]')", 30), "刷新后没渲染"
        time.sleep(1.0)
        after = read_state()
        print("刷新后滑块:", json.dumps(after["sliders"], ensure_ascii=False))
        print("刷新后变量:", json.dumps(after["vars"], ensure_ascii=False))
        print("刷新后计算值:", {k: alpha_of(after[k]) for k in ("nav", "title", "panel")})

        # 恢复默认
        js("document.querySelector('[data-act=\"opacity-reset\"]').click()")
        time.sleep(1.8)
        reset = read_state()
        print("恢复默认滑块:", json.dumps(reset["sliders"], ensure_ascii=False))
        print("恢复默认变量:", json.dumps(reset["vars"], ensure_ascii=False))
        print("恢复默认计算值:", {k: alpha_of(reset[k]) for k in ("nav", "title", "panel")})
        print("恢复后服务端:", js("fetch('/api/me').then(r=>r.json()).then(d=>d.user.settings.surfaceOpacity)"))

        # 手机宽度
        send("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=True)
        time.sleep(1.2)
        shot("op_settings_mobile.png")
        print("手机端溢出:", js("document.documentElement.scrollWidth > window.innerWidth + 1"))

        print("最终错误:", js("window.__errs"))
        ws.close()
    finally:
        proc.terminate()
        db = db_failover.current_sessionmaker()()
        row = db.get(UserSetting, admin.id)
        if row:
            row.surface_opacity = backup
            db.commit()
        db.close()
        print("已还原 surface_opacity =", backup)


if __name__ == "__main__":
    main()
