# -*- coding: utf-8 -*-
"""合集分享页双击预览冒烟：双击文件出弹层 / 图片加载 / MD 正文 / PDF iframe / 关闭。

用法：python scripts/share_preview_smoke.py [port]
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import websocket

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from app import db_failover  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import Share, User  # noqa: E402
from app.security import create_token  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)

ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {label} {extra}")
    else:
        fail += 1
        print(f"  FAIL {label} {extra}")


def http(method, path, body=None, cookie=""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Origin", BASE)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {}


def main():
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    admin = db.query(User).filter(User.role == 1).order_by(User.id).first()
    cookie = f"{settings.COOKIE_NAME}={create_token(admin.id, admin.token_version)}"
    db.close()

    # 47=小图 png、12=pdf、99=markdown，都是现有真实文件
    st, d = http("POST", "/api/shares", {"nodeIds": [47, 12, 99], "expiresDays": 1}, cookie=cookie)
    check("创建合集分享 200", st == 200, st)
    share = d.get("share", {})
    share_id = share.get("id")
    token = share.get("token")
    check("返回 token", bool(token), token)
    check("类型 multi", share.get("type") == "multi", share.get("type"))

    profile = Path(tempfile.mkdtemp(prefix="cdp_sp_"))
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=9455",
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
                    for t in json.load(urllib.request.urlopen("http://127.0.0.1:9455/json/list", timeout=2))
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
        send("Emulation.setDeviceMetricsOverride", width=1280, height=1000, deviceScaleFactor=1, mobile=False)
        send("Page.addScriptToEvaluateOnNewDocument",
             source="window.__errs=[];addEventListener('error',e=>window.__errs.push(String(e.message)));")

        send("Page.navigate", url=f"{BASE}/s/{token}")
        assert wait_for("!!document.querySelector('.share-item')", 30), "分享列表没渲染"
        time.sleep(0.8)

        rows = js("""[...document.querySelectorAll('.share-item')].map(r => ({
          id: r.dataset.id, type: r.dataset.type,
          name: r.querySelector('.share-item__name').textContent,
          hasEye: !!r.querySelector('[data-preview]'),
          hasOldLink: !!r.querySelector('a[target="_blank"]'),
        }))""")
        print("列表行:", json.dumps(rows, ensure_ascii=False))
        check("三行文件", len(rows) == 3, len(rows))
        check("每行都有预览按钮", all(r["hasEye"] for r in rows), "")
        check("不再用新标签页预览", all(not r["hasOldLink"] for r in rows), "")
        shot("share_list_before.png")

        def dblclick_row(index):
            js(f"""(() => {{
              const row = document.querySelectorAll('.share-item')[{index}];
              row.dispatchEvent(new MouseEvent('dblclick', {{ bubbles: true }}));
            }})()""")

        # 1) 双击图片
        dblclick_row(0)
        assert wait_for("!document.querySelector('.share-preview-modal').hidden", 10), "双击没弹出预览"
        time.sleep(1.4)
        img_state = js("""(() => {
          const m = document.querySelector('.share-preview-modal');
          const img = m.querySelector('.share-preview-modal__body img');
          const panel = m.querySelector('.share-preview-modal__panel');
          const back = m.querySelector('.share-preview-modal__backdrop');
          const box = (el) => { const r = el.getBoundingClientRect(); return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]; };
          return {
            open: !m.hidden,
            name: m.querySelector('[data-modal-name]').textContent,
            meta: m.querySelector('[data-modal-meta]').textContent,
            hasImg: !!img,
            loaded: !!img && img.complete && img.naturalWidth > 0,
            download: m.querySelector('[data-modal-download]').getAttribute('href'),
            bodyLocked: document.body.classList.contains('is-preview-open'),
            theme: document.documentElement.dataset.theme,
            viewport: [innerWidth, innerHeight],
            modalBox: box(m), panelBox: box(panel), backBox: box(back),
            panelBg: getComputedStyle(panel).backgroundColor,
            backBg: getComputedStyle(back).backgroundColor,
            panelDisplay: getComputedStyle(m).display,
            modalPos: getComputedStyle(m).position,
            modalInset: getComputedStyle(m).inset,
            modalWidthCss: getComputedStyle(m).width,
            parentTag: m.parentElement.tagName,
            bodyBox: box(document.body),
            htmlBox: box(document.documentElement),
            bodyDisplay: getComputedStyle(document.body).display,
            errs: window.__errs,
          };
        })()""")
        print("图片预览:", json.dumps(img_state, ensure_ascii=False))
        check("弹层已打开", img_state.get("open") is True, "")
        check("标题是文件名", "png" in (img_state.get("name") or ""), img_state.get("name"))
        check("图片真实加载", img_state.get("loaded") is True, "")
        check("下载链接带 nodeId", "nodeId=" in (img_state.get("download") or ""), img_state.get("download"))
        check("打开时锁滚动", img_state.get("bodyLocked") is True, "")
        shot("share_preview_image.png")

        # 2) Esc 关闭
        js("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))")
        time.sleep(0.5)
        check("Esc 关闭", js("document.querySelector('.share-preview-modal').hidden") is True, "")
        check("关闭后解锁滚动", js("document.body.classList.contains('is-preview-open')") is False, "")
        check("关闭后清空内容", js("document.querySelector('[data-modal-body]').innerHTML === ''") is True, "")

        # 3) 点眼睛按钮预览 PDF
        js("document.querySelectorAll('.share-item')[1].querySelector('[data-preview]').click()")
        assert wait_for("!document.querySelector('.share-preview-modal').hidden", 10), "按钮没弹出预览"
        time.sleep(1.2)
        pdf_state = js("""(() => {
          const m = document.querySelector('.share-preview-modal');
          return {
            name: m.querySelector('[data-modal-name]').textContent,
            hasFrame: !!m.querySelector('.share-preview-modal__body iframe'),
            src: (m.querySelector('.share-preview-modal__body iframe') || {}).src || '',
          };
        })()""")
        print("PDF 预览:", json.dumps(pdf_state, ensure_ascii=False))
        check("PDF iframe 存在", pdf_state.get("hasFrame") is True, "")
        check("PDF 预览带 nodeId", "nodeId=12" in (pdf_state.get("src") or ""), pdf_state.get("src"))

        js("document.querySelector('.share-preview-modal [data-close]').click()")
        time.sleep(0.4)

        # 4) 双击 Markdown，正文渲染
        dblclick_row(2)
        assert wait_for("!document.querySelector('.share-preview-modal').hidden", 10), "双击 MD 没弹层"
        loaded = wait_for("(document.querySelector('[data-md]')||{}).innerText.trim().length > 20", 20)
        md_state = js("""(() => {
          const m = document.querySelector('.share-preview-modal');
          const md = m.querySelector('[data-md]');
          return { name: m.querySelector('[data-modal-name]').textContent,
                   len: md ? md.innerText.trim().length : 0,
                   hasH: !!(md && md.querySelector('h1,h2,h3')) };
        })()""")
        print("Markdown 预览:", json.dumps(md_state, ensure_ascii=False))
        check("MD 正文已渲染", loaded, md_state.get("len"))
        check("MD 有标题结构", md_state.get("hasH") is True, "")
        shot("share_preview_md.png")

        js("document.querySelector('.share-preview-modal [data-close]').click()")
        time.sleep(0.3)

        # 5) 双击行内按钮之外区域不会误触（点下载图标不应弹层）
        js("document.querySelectorAll('.share-item')[0].querySelector('a[download]').click()")
        time.sleep(0.6)
        check("点下载不弹预览", js("document.querySelector('.share-preview-modal').hidden") is True, "")

        # 6) 手机宽度
        send("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=True)
        time.sleep(0.8)
        dblclick_row(0)
        time.sleep(1.2)
        mobile_state = js("""(() => {
          const panel = document.querySelector('.share-preview-modal__panel');
          const r = panel.getBoundingClientRect();
          return { w: Math.round(r.width), h: Math.round(r.height),
                   vw: innerWidth, vh: innerHeight,
                   overflowX: document.documentElement.scrollWidth > innerWidth + 1 };
        })()""")
        print("手机弹层:", mobile_state)
        check("手机弹层占满宽度", abs(mobile_state["w"] - mobile_state["vw"]) <= 1, mobile_state["w"])
        check("手机无横向溢出", mobile_state["overflowX"] is False, "")
        shot("share_preview_mobile.png")

        print("最终错误:", js("window.__errs"))
        ws.close()
    finally:
        proc.terminate()
        # 清理测试分享
        db = db_failover.current_sessionmaker()()
        row = db.get(Share, share_id) if share_id else None
        if row:
            db.delete(row)
            db.commit()
        db.close()
        print("已删除测试分享", share_id)

    print(f"\n通过 {ok} 项，失败 {fail} 项")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
