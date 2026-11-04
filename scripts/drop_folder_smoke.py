# -*- coding: utf-8 -*-
"""拖入文件夹上传冒烟：目录结构保留 / 同名合并 / 忽略系统文件 / 空目录。

用 DataTransferItem.webkitGetAsEntry 的伪实现喂一棵目录树，走真实上传接口。

用法：python scripts/drop_folder_smoke.py [port]
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
from app.models import User  # noqa: E402
from app.security import create_token  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)
ROOT_NAME = f"拖拽测试_{int(time.time()) % 100000}"

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


def list_dir(cookie, parent_id=None):
    path = "/api/nodes?sort=name&order=asc"
    if parent_id is not None:
        path += f"&parentId={parent_id}"
    st, d = http("GET", path, cookie=cookie)
    return d.get("items", []) if st == 200 else []


def wait_api(fn, timeout=45, interval=0.6):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return None


def main():
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    admin = db.query(User).filter(User.role == 1).order_by(User.id).first()
    token = create_token(admin.id, admin.token_version)
    cookie = f"{settings.COOKIE_NAME}={token}"
    db.close()

    # 保险：先清掉同名残留
    for item in list_dir(cookie):
        if item["type"] == "folder" and item["name"].startswith("拖拽测试_"):
            http("DELETE", f"/api/nodes/{item['id']}/purge", cookie=cookie)

    profile = Path(tempfile.mkdtemp(prefix="cdp_drop_"))
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=9459",
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
                    for t in json.load(urllib.request.urlopen("http://127.0.0.1:9459/json/list", timeout=2))
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

        def wait_for(expr, timeout=40):
            end = time.time() + timeout
            while time.time() < end:
                if js(expr):
                    return True
                time.sleep(0.3)
            return False

        send("Page.enable")
        send("Runtime.enable")
        send("Emulation.setDeviceMetricsOverride", width=1280, height=1000, deviceScaleFactor=1, mobile=False)
        send("Network.setCookie", name=settings.COOKIE_NAME, value=token,
             domain="127.0.0.1", path="/", httpOnly=True)
        send("Page.addScriptToEvaluateOnNewDocument",
             source="window.__errs=[];addEventListener('error',e=>window.__errs.push(String(e.message)));")

        send("Page.navigate", url=f"{BASE}/#/files")
        assert wait_for("!!document.querySelector('[data-drop]')", 30), "文件页没渲染"
        time.sleep(1.0)

        # 造一棵伪目录树 + 覆盖 webkitGetAsEntry
        setup = r"""
        window.__makeDrop = (rootName, extra) => {
          const f = (name, text) => ({
            isFile: true, isDirectory: false, name,
            file: (ok) => ok(new File([text], name, { type: 'text/plain' })),
          });
          const d = (name, children) => ({
            isFile: false, isDirectory: true, name,
            createReader: () => {
              let done = false;
              return { readEntries: (ok) => { if (done) ok([]); else { done = true; ok(children); } } };
            },
          });
          const kids = [
            f('a.txt', 'hello-a'),
            f('.DS_Store', 'junk'),
            d('sub', [
              f('b.txt', 'hello-b'),
              d('deep', [f('c.txt', 'hello-c')]),
            ]),
            d('empty', []),
          ];
          if (extra) kids.push(f(extra, 'hello-extra'));
          const root = d(rootName, kids);
          const proto = DataTransferItem.prototype;
          if (!proto.__origEntry) {
            proto.__origEntry = proto.webkitGetAsEntry;
          }
          proto.webkitGetAsEntry = () => root;
          const dt = new DataTransfer();
          dt.items.add(new File(['trigger'], 'trigger.txt', { type: 'text/plain' }));
          const ev = new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt });
          document.querySelector('.view-root').dispatchEvent(ev);
          return true;
        };
        window.__makeEmptyDrop = (rootName) => {
          const d = (name, children) => ({
            isFile: false, isDirectory: true, name,
            createReader: () => {
              let done = false;
              return { readEntries: (ok) => { if (done) ok([]); else { done = true; ok(children); } } };
            },
          });
          const root = d(rootName, [d('inner', [])]);
          DataTransferItem.prototype.webkitGetAsEntry = () => root;
          const dt = new DataTransfer();
          dt.items.add(new File(['trigger'], 'trigger.txt', { type: 'text/plain' }));
          document.querySelector('.view-root').dispatchEvent(
            new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt })
          );
          return true;
        };
        """
        js(setup)

        print("== 第一次拖入（含空目录 / 系统文件） ==")
        js(f"window.__makeDrop({json.dumps(ROOT_NAME)}, null)")
        root = wait_api(lambda: next((i for i in list_dir(cookie) if i["name"] == ROOT_NAME), None), 45)
        check("根文件夹已创建", bool(root), root and root["name"])
        if not root:
            raise SystemExit(1)

        kids = wait_api(
            lambda: (lambda arr: arr if len(arr) >= 3 else None)(list_dir(cookie, root["id"])), 20
        ) or []
        names = {i["name"]: i for i in kids}
        print("根目录内容:", sorted(names))
        check("a.txt 传入根目录", "a.txt" in names, "")
        check("sub 目录创建", "sub" in names and names["sub"]["type"] == "folder", "")
        check("empty 空目录保留", "empty" in names and names["empty"]["type"] == "folder", "")
        check("忽略 .DS_Store", ".DS_Store" not in names, "")

        sub = names.get("sub")
        if sub:
            subkids = wait_api(
                lambda: (lambda arr: arr if len(arr) >= 2 else None)(list_dir(cookie, sub["id"])), 20
            ) or []
            subnames = {i["name"]: i for i in subkids}
            print("sub 内容:", sorted(subnames))
            check("sub/b.txt", "b.txt" in subnames, "")
            check("sub/deep 目录", "deep" in subnames and subnames["deep"]["type"] == "folder", "")
            deep = subnames.get("deep")
            if deep:
                deepkids = wait_api(
                    lambda: (lambda arr: arr if arr else None)(list_dir(cookie, deep["id"])), 20
                ) or []
                check("deep/c.txt", any(i["name"] == "c.txt" for i in deepkids), [i["name"] for i in deepkids])

        # 等上传面板收尾，截个图
        time.sleep(1.0)
        shot("drop_folder_done.png")

        print("== 第二次拖入同名目录（应合并） ==")
        js(f"window.__makeDrop({json.dumps(ROOT_NAME)}, 'extra.txt')")
        merged = wait_api(
            lambda: (lambda arr: arr if any(i["name"] == "extra.txt" for i in arr) else None)(
                list_dir(cookie, root["id"])
            ),
            45,
        ) or []
        root_items = list_dir(cookie)
        same = [i for i in root_items if i["type"] == "folder" and i["name"] == ROOT_NAME]
        same2 = [i for i in root_items if i["type"] == "folder" and i["name"].startswith(ROOT_NAME + " (")]
        check("同名目录没有新建副本", len(same) == 1 and not same2, [i["name"] for i in same + same2])
        check("第二次的文件进同一目录", any(i["name"] == "extra.txt" for i in merged), [i["name"] for i in merged])
        check("旧文件还在", any(i["name"] == "a.txt" for i in merged), "")

        print("== 只拖空文件夹 ==")
        empty_name = ROOT_NAME + "_空"
        js(f"window.__makeEmptyDrop({json.dumps(empty_name)})")
        empty_root = wait_api(
            lambda: next((i for i in list_dir(cookie) if i["name"] == empty_name), None), 20
        )
        check("空文件夹也被创建", bool(empty_root), empty_root and empty_root["name"])
        if empty_root:
            inner = list_dir(cookie, empty_root["id"])
            check("空目录层级保留", any(i["name"] == "inner" for i in inner), [i["name"] for i in inner])

        # 点开文件夹看看
        js(f"""(() => {{
          const tile = [...document.querySelectorAll('[data-id]')].find(el => el.textContent.includes({json.dumps(ROOT_NAME)}));
          if (tile) tile.dispatchEvent(new MouseEvent('dblclick', {{ bubbles: true }}));
        }})()""")
        time.sleep(1.2)

        print("== 拖入单个普通文件（回归） ==")
        js("DataTransferItem.prototype.webkitGetAsEntry = DataTransferItem.prototype.__origEntry;")
        js("""(() => {
          const dt = new DataTransfer();
          dt.items.add(new File(['flat-content'], '扁平文件.txt', { type: 'text/plain' }));
          document.querySelector('.view-root').dispatchEvent(
            new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt })
          );
          return true;
        })()""")
        flat = wait_api(
            lambda: (lambda arr: arr if any(i["name"] == "扁平文件.txt" for i in arr) else None)(
                list_dir(cookie, root["id"])
            ),
            30,
        )
        check("普通文件落到当前目录", bool(flat), [i["name"] for i in flat or []])
        shot("drop_folder_inside.png")

        print("最终错误:", js("window.__errs"))
        ws.close()
    finally:
        proc.terminate()
        # 清理测试目录
        for item in list_dir(cookie):
            if item["type"] == "folder" and item["name"].startswith("拖拽测试_"):
                st, _ = http("DELETE", f"/api/nodes/{item['id']}/purge", cookie=cookie)
                print("清理", item["name"], st)

    print(f"\n通过 {ok} 项，失败 {fail} 项")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
