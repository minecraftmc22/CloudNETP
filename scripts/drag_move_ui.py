# -*- coding: utf-8 -*-
"""拖动移动（拖到文件夹上松手）的回归检查。

钉住这几件事：
- 磁贴/列表行是可拖源（data-drag-id），文件夹同时是投放目标（data-drop-folder）
- 鼠标拖动：出现跟手克隆 + 目标高亮 + 「移到『X』」提示，松手后移动成功、界面清干净
- 松手那一下不能顺带把文件夹打开 / 把选中项清掉
- 拖到自己身上、拖到「当前所在目录」都不应该高亮、不应该发请求
- 面包屑能接：把文件拖回上层目录
- 多选后拖其中一项 → 整批一起走，克隆上带数量角标
- 触屏：长按 280ms 才算拖；手指先动就是滚列表，不能莫名其妙把文件挪走

用法（需要一个已启动的隔离实例）：
    python scripts/drag_move_ui.py [port]
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
from app.models import Node, User, utcnow  # noqa: E402
from app.security import create_token  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8127
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
W, H = 390, 844
DESK_W, DESK_H = 1280, 800

TARGET = "目标夹"
SRC_A = "拖A"
SRC_B = "拖B"
SRC_M = "拖M"
SRC_S = "拖S"
SRC_L = "拖L"
ALL_NAMES = (TARGET, SRC_A, SRC_B, SRC_M, SRC_S, SRC_L)

CHECKS: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> None:
    CHECKS.append((bool(ok), msg))


def _db():
    if not db_failover._started:
        db_failover.startup()
    return db_failover.current_sessionmaker()()


def _folder(db, user_id: int, name: str, parent_id: int | None):
    node = Node(user_id=user_id, parent_id=parent_id, type="folder", name=name, size=0,
                created_at=utcnow(), updated_at=utcnow())
    db.add(node)
    return node


def seed() -> str:
    """清掉上次的残留，重新铺 4 个空文件夹，返回管理员 token。"""
    db = _db()
    try:
        admin = db.query(User).filter(User.role == 1).order_by(User.id.asc()).first()
        assert admin, "隔离库里没有管理员账号"

        for old in db.query(Node).filter(Node.user_id == admin.id, Node.name.in_(ALL_NAMES)).all():
            db.delete(old)
        db.commit()

        target = _folder(db, admin.id, TARGET, None)
        _folder(db, admin.id, SRC_A, None)
        _folder(db, admin.id, SRC_B, None)
        _folder(db, admin.id, SRC_M, None)
        _folder(db, admin.id, SRC_S, None)
        _folder(db, admin.id, SRC_L, None)
        db.commit()
        print(f"seed: {TARGET}=#{target.id} 五个空文件夹已就位")
        return create_token(admin.id, admin.token_version)
    finally:
        db.close()


def parent_of(name: str):
    db = _db()
    try:
        node = db.query(Node).filter(Node.name == name, Node.deleted_at.is_(None)).order_by(Node.id.desc()).first()
        return None if node is None else node.parent_id
    finally:
        db.close()


def id_of(name: str):
    db = _db()
    try:
        node = db.query(Node).filter(Node.name == name, Node.deleted_at.is_(None)).order_by(Node.id.desc()).first()
        return None if node is None else node.id
    finally:
        db.close()


def main() -> None:
    token = seed()
    target_id = id_of(TARGET)
    profile = Path(tempfile.mkdtemp(prefix="cdp_drag_ui_"))
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=9479",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--disable-gpu",
            "--hide-scrollbars",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        target_page = None
        for _ in range(80):
            try:
                pages = [
                    t
                    for t in json.load(urllib.request.urlopen("http://127.0.0.1:9479/json/list", timeout=2))
                    if t.get("type") == "page"
                ]
                if pages:
                    target_page = pages[0]
                    break
            except Exception:
                time.sleep(0.4)
        assert target_page, "无法连接 Chrome CDP"
        ws = websocket.create_connection(target_page["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
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

        # ---------- 手势工具 ----------
        def rect_of(name):
            return js(
                """
                ((name) => {
                  const el = [...document.querySelectorAll('#view-root [data-drag-id]')].find((n) => {
                    const label = n.querySelector('.tile__name, .frow__name');
                    return (label?.textContent || '').trim().startsWith(name);
                  });
                  if (!el) return null;
                  const r = el.getBoundingClientRect();
                  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
                })(%s)
                """
                % json.dumps(name)
            )

        def crumb_rect(value):
            return js(
                """
                ((v) => {
                  const el = document.querySelector(`#view-root [data-crumb-id="${v}"]`);
                  if (!el) return null;
                  const r = el.getBoundingClientRect();
                  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
                })(%s)
                """
                % json.dumps(value)
            )

        def mouse_press(x, y):
            send("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", buttons=1, clickCount=1)

        def mouse_move(x, y):
            send("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, button="left", buttons=1)

        def mouse_release(x, y):
            send("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", buttons=0, clickCount=1)

        def dblclick(x, y):
            for count in (1, 2):
                send("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", buttons=1, clickCount=count)
                send("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", buttons=0, clickCount=count)
                time.sleep(0.06)

        def mouse_drag(src, dst, steps=8, hold_cb=None):
            """从 src 拖到 dst；hold_cb 在松手前调用（用来查拖动中的状态）"""
            mouse_press(src["x"], src["y"])
            for i in range(1, steps + 1):
                mouse_move(src["x"] + (dst["x"] - src["x"]) * i / steps, src["y"] + (dst["y"] - src["y"]) * i / steps)
                time.sleep(0.02)
            time.sleep(0.12)
            mid_state = hold_cb() if hold_cb else None
            mouse_release(dst["x"], dst["y"])
            return mid_state

        def touch_start(x, y):
            send("Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": x, "y": y, "id": 1}])

        def touch_move(x, y):
            send("Input.dispatchTouchEvent", type="touchMove", touchPoints=[{"x": x, "y": y, "id": 1}])

        def touch_end():
            send("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])

        def drag_state():
            return js(
                """
                (() => {
                  const ghost = document.querySelector('.drag-ghost');
                  const hint = document.querySelector('.drag-hint');
                  const hot = document.querySelector('#view-root .is-drop-target');
                  return {
                    ghost: !!ghost,
                    ghostCount: ghost?.querySelector('.drag-ghost__count')?.textContent || '',
                    hint: hint && !hint.hidden ? hint.textContent : '',
                    hot: hot ? (hot.dataset.dropName || (hot.querySelector('.tile__name, .frow__name')?.textContent || '').trim()) : '',
                    body: document.body.classList.contains('is-drag-moving'),
                    dragging: !!document.querySelector('#view-root .is-dragging'),
                  };
                })()
                """
            )

        def clean_state():
            return js(
                """
                (() => ({
                  ghost: !!document.querySelector('.drag-ghost'),
                  hint: !!document.querySelector('.drag-hint'),
                  hot: !!document.querySelector('.is-drop-target'),
                  body: document.body.classList.contains('is-drag-moving'),
                  dragging: !!document.querySelector('.is-dragging'),
                }))()
                """
            )

        def open_folder(name):
            js(
                """
                ((name) => {
                  const el = [...document.querySelectorAll('#view-root [data-drag-id]')].find((n) => {
                    const label = n.querySelector('.tile__name, .frow__name');
                    return (label?.textContent || '').trim().startsWith(name);
                  });
                  el.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
                })(%s)
                """
                % json.dumps(name)
            )

        def goto_root():
            js('location.hash = "#/files"')
            time.sleep(0.6)
            crumb = crumb_rect("")
            if crumb:
                js("document.querySelector('#view-root [data-crumb-id=\"\"]').click()")
            time.sleep(0.8)

        send("Page.enable")
        send("Runtime.enable")
        send("Network.enable")

        # ================= 1) 桌面：鼠标拖动 =================
        send("Emulation.setDeviceMetricsOverride", width=DESK_W, height=DESK_H, deviceScaleFactor=1, mobile=False)
        send("Network.setCookie", name=settings.COOKIE_NAME, value=token, domain="127.0.0.1", path="/", httpOnly=True)
        send("Page.navigate", url=f"{BASE}/#/files")
        assert wait_for("!!document.querySelector('#view-root .file-grid .tile')", 30), "文件页没渲染"
        time.sleep(1.6)

        # 1.1 可拖 / 可放属性
        attrs = js(
            """
            (() => {
              const tiles = [...document.querySelectorAll('#view-root .tile')];
              const pick = (name) => {
                const el = tiles.find((n) => (n.querySelector('.tile__name')?.textContent || '').trim().startsWith(name));
                return el ? { drag: el.dataset.dragId || '', drop: el.dataset.dropFolder ?? null } : null;
              };
              return { all: tiles.length, allDrag: tiles.every((t) => !!t.dataset.dragId), target: pick('目标夹'), src: pick('拖A') };
            })()
            """
        )
        check(attrs["allDrag"], f"所有磁贴都是可拖源（{attrs['all']} 个）")
        check(attrs["src"] and attrs["src"]["drag"], "文件夹有 data-drag-id")
        check(attrs["target"] and attrs["target"]["drop"] == str(target_id), f"文件夹是投放目标（{attrs['target']}）")

        # 1.1a 真实单击仍然只是「选中」（指针捕获别把 click 吃掉、也别顺手开文件夹）
        one = rect_of(SRC_L)
        mouse_press(one["x"], one["y"])
        mouse_release(one["x"], one["y"])
        time.sleep(0.4)
        check(js("document.querySelectorAll('#view-root .tile.is-selected').length") == 1, "真实单击照常选中一项")
        check(js("document.querySelectorAll('#view-root [data-crumb-id]').length") == 0, "单击不会打开文件夹")

        # 1.1b 双击照常打开（指针捕获不能把双击吃掉）
        spy = rect_of(SRC_S)
        dblclick(spy["x"], spy["y"])
        check(wait_for("document.querySelectorAll('#view-root [data-crumb-id]').length >= 2", 8), "双击文件夹照常打开")
        goto_root()
        time.sleep(0.8)

        # 1.2 拖 A → 目标夹
        src = rect_of(SRC_A)
        dst = rect_of(TARGET)
        check(bool(src and dst), "拖A / 目标夹 都在视野里")
        drag_mid = mouse_drag(src, dst, hold_cb=drag_state)
        check(drag_mid["ghost"] is True, "拖动时出现跟手克隆")
        check(drag_mid["body"] is True, "拖动时进入拖动态（光标抓取）")
        check(drag_mid["hot"].startswith(TARGET), f"目标文件夹高亮（{drag_mid['hot']}）")
        check(TARGET in (drag_mid["hint"] or ""), f"提示写明放到哪（{drag_mid['hint']!r}）")
        check(drag_mid["dragging"] is True, "原件淡出")

        gone = wait_for(
            "(() => [...document.querySelectorAll('#view-root .tile__name')].every((n) => !n.textContent.trim().startsWith('拖A')))()",
            12,
        )
        check(gone, "松手后列表刷新出结果")
        time.sleep(0.3)
        rest = clean_state()
        check(not rest["ghost"] and not rest["hint"] and not rest["hot"] and not rest["body"] and not rest["dragging"],
              f"松手后拖动态清干净（{rest}）")
        check(parent_of(SRC_A) == target_id, "拖A 的父目录已变成目标夹")
        check(js("document.querySelector('#view-root .crumb')?.children.length || 0") == 0, "松手那一下没把文件夹打开")

        # 1.3 进目标夹：面包屑成为投放目标
        open_folder(TARGET)
        assert wait_for("!!document.querySelector('#view-root [data-crumb-id=\"\"]')", 12), "没进到目标夹"
        time.sleep(0.8)
        crumb = crumb_rect("")
        check(crumb is not None, "面包屑根目录存在")
        check(js("!!document.querySelector('#view-root [data-crumb-id][data-drop-folder]')"), "面包屑项可接文件")

        # 1.4 拖到自己身上 / 拖到当前目录：都不该高亮
        inner = rect_of(SRC_A)
        now_crumb = crumb_rect(str(target_id))
        mid_self = mouse_drag(inner, inner, hold_cb=drag_state)
        check(mid_self["hot"] == "", "拖到自己身上不高亮")
        check(parent_of(SRC_A) == target_id, "拖到自己身上不会移动")

        mid_noop = mouse_drag(inner, now_crumb, hold_cb=drag_state)
        check(mid_noop["hot"] == "", "拖到当前所在目录不高亮（本来就在这儿）")
        check(parent_of(SRC_A) == target_id, "拖到当前目录不会发请求")

        # 1.5 拖回根目录（面包屑）
        root_crumb = crumb_rect("")
        mid_root = mouse_drag(rect_of(SRC_A), root_crumb, hold_cb=drag_state)
        check(root_crumb is not None and mid_root["hot"] != "", f"拖到面包屑「我的文件」会高亮（{mid_root['hot']}）")
        assert wait_for(
            "(() => [...document.querySelectorAll('#view-root .tile__name')].every((n) => !n.textContent.trim().startsWith('拖A')))()",
            12,
        ), "拖回根目录后列表没刷新"
        check(parent_of(SRC_A) is None, "拖A 已回到根目录")

        # 1.6 多选后拖一项 → 整批走
        goto_root()
        time.sleep(0.8)
        js(
            """
            (() => {
              const tiles = [...document.querySelectorAll('#view-root .tile')];
              const find = (name) => tiles.find((n) => (n.querySelector('.tile__name')?.textContent || '').trim().startsWith(name));
              for (const name of ['拖A', '拖B']) {
                find(name).dispatchEvent(new MouseEvent('click', { bubbles: true, ctrlKey: true }));
              }
            })()
            """
        )
        time.sleep(0.3)
        selected = js("document.querySelectorAll('#view-root .tile.is-selected').length")
        check(selected == 2, f"两项处于选中态（{selected}）")
        mid_multi = mouse_drag(rect_of(SRC_A), rect_of(TARGET), hold_cb=drag_state)
        check(mid_multi["ghostCount"] == "2", f"克隆上带数量角标（{mid_multi['ghostCount']}）")
        assert wait_for(
            "(() => [...document.querySelectorAll('#view-root .tile__name')].every((n) => !/^拖[AB]/.test(n.textContent.trim())))()",
            12,
        ), "多选移动后列表没刷新"
        check(parent_of(SRC_A) == target_id and parent_of(SRC_B) == target_id, "选中的两项都移进了目标夹")

        # 1.7 列表视图同样能拖
        js("document.querySelector('#view-root [data-view=list]').click()")
        time.sleep(0.9)
        rows = js(
            """
            (() => {
              const rows = [...document.querySelectorAll('#view-root .frow[data-drag-id]')];
              const folder = rows.find((n) => (n.querySelector('.frow__name')?.textContent || '').trim().startsWith('目标夹'));
              return { count: rows.length, folderDrop: folder?.dataset.dropFolder ?? null };
            })()
            """
        )
        check(rows["count"] > 0, f"列表行也是可拖源（{rows['count']} 行）")
        check(rows["folderDrop"] == str(target_id), "列表里的文件夹行可投放")
        list_mid = mouse_drag(rect_of(SRC_L), rect_of(TARGET), hold_cb=drag_state)
        check(list_mid["hot"].startswith(TARGET), f"列表视图拖动也高亮目标行（{list_mid['hot']}）")
        list_done = wait_for(
            "(() => [...document.querySelectorAll('#view-root .frow__name')].every((n) => !n.textContent.trim().startsWith('拖L')))()",
            12,
        )
        check(list_done and parent_of(SRC_L) == target_id, "列表视图拖放移动成功")
        js("document.querySelector('#view-root [data-view=grid]').click()")
        time.sleep(0.9)

        # ================= 2) 手机：触屏长按拖动 =================
        send("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=2, mobile=True)
        send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        send("Page.navigate", url=f"{BASE}/#/files")
        assert wait_for("!!document.querySelector('#view-root .tile')", 30), "手机端文件页没渲染"
        time.sleep(1.6)

        src = rect_of(SRC_M)
        dst = rect_of(TARGET)
        check(bool(src and dst), "手机上 拖M / 目标夹 都在视野里")

        # 2.1 长按 280ms 后进入拖动
        touch_start(src["x"], src["y"])
        time.sleep(0.12)
        check(js("!document.querySelector('.drag-ghost')") is True, "刚按下还没到 280ms，不算拖")
        time.sleep(0.4)
        held = drag_state()
        check(held["ghost"] is True, "长按后进入拖动")
        check(held["dragging"] is True, "长按后原件淡出")

        # 2.2 拖到目标上松手
        for i in range(1, 7):
            touch_move(src["x"] + (dst["x"] - src["x"]) * i / 6, src["y"] + (dst["y"] - src["y"]) * i / 6)
            time.sleep(0.04)
        time.sleep(0.15)
        over = drag_state()
        check(over["hot"].startswith(TARGET), f"手指移到目标上会高亮（{over['hot']}）")
        check(TARGET in (over["hint"] or ""), f"手机上也有「移到」提示（{over['hint']!r}）")
        touch_end()
        assert wait_for(
            "(() => [...document.querySelectorAll('#view-root .tile__name')].every((n) => !n.textContent.trim().startsWith('拖M')))()",
            12,
        ), "触屏移动后列表没刷新"
        check(parent_of(SRC_M) == target_id, "触屏拖放移动成功")
        time.sleep(0.3)
        rest2 = clean_state()
        check(not rest2["ghost"] and not rest2["body"], f"触屏松手后也清干净（{rest2}）")

        # 2.3 轻点一下不该拿起文件
        goto_root()
        time.sleep(0.9)
        src = rect_of(SRC_S)
        check(src is not None, "拖S 在根目录")
        touch_start(src["x"], src["y"])
        touch_end()
        time.sleep(0.6)
        check(js("!document.querySelector('.drag-ghost')") is True, "轻点一下不会进入拖动")
        check(parent_of(SRC_S) is None, "轻点不会移动文件")

        # 2.4 按住期间手指动了（超过 8px）→ 当成滚列表，不能拿起文件。
        #     这一条用合成指针事件来钉：真机上一帧就能收到 pointermove，
        #     但 CDP 派发 touchMove 每次要等几百毫秒，真实事件会晚于 280ms 的判定。
        js(
            """
            (() => {
              const el = [...document.querySelectorAll('#view-root [data-drag-id]')].find((n) =>
                (n.querySelector('.tile__name')?.textContent || '').trim().startsWith('拖S'));
              const r = el.getBoundingClientRect();
              window.__swipe = { el, x: r.left + r.width / 2, y: r.top + r.height / 2 };
              window.__swipe.el.dispatchEvent(new PointerEvent('pointerdown', {
                bubbles: true, cancelable: true, pointerId: 7, pointerType: 'touch', isPrimary: true,
                clientX: window.__swipe.x, clientY: window.__swipe.y,
              }));
            })()
            """
        )
        time.sleep(0.1)
        js(
            """
            window.dispatchEvent(new PointerEvent('pointermove', {
              bubbles: true, cancelable: true, pointerId: 7, pointerType: 'touch', isPrimary: true,
              clientX: window.__swipe.x, clientY: window.__swipe.y + 40,
            }));
            """
        )
        time.sleep(0.5)
        swiped = drag_state()
        check(swiped["ghost"] is False, "手指先动就不进入拖动（当成滚列表）")
        check(swiped["body"] is False, "手指先动时不留拖动态")
        js(
            """
            window.dispatchEvent(new PointerEvent('pointerup', {
              bubbles: true, cancelable: true, pointerId: 7, pointerType: 'touch', isPrimary: true,
              clientX: window.__swipe.x, clientY: window.__swipe.y + 40,
            }));
            """
        )
        time.sleep(0.5)
        check(parent_of(SRC_S) is None, "滑动列表不会移动文件")

    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        passed = sum(1 for ok, _ in CHECKS if ok)
        for ok, msg in CHECKS:
            print(("  OK  " if ok else "  NG  ") + msg)
        print(f"\n拖动移动：{passed}/{len(CHECKS)}")

    if passed != len(CHECKS):
        sys.exit(1)


if __name__ == "__main__":
    main()
