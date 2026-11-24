# -*- coding: utf-8 -*-
"""聊天页的浏览器回归（桌面 + 手机两套宽度）。

覆盖：
- 桌面：左右两栏、会话列表（标题/预览/未读角标）、气泡左右分、图片加载、
  文件卡片带下载、回车发送、表情面板发包、消息菜单（转发/举报/撤回）、撤回后的样子
- 群聊：群头像、头部成员数、群信息弹窗
- 手机：只显示列表 → 点会话进消息流（有返回）→ 返回列表；无横向溢出

用法（需要一个已启动的隔离实例）：
    python scripts/chat_ui.py [port]
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

import websocket

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from PIL import Image  # noqa: E402
from sqlalchemy import delete, select, update  # noqa: E402

from app import db_failover, profile_service as ps, storage  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import (  # noqa: E402
    Blob,
    ChatConversation,
    ChatMember,
    ChatMessage,
    ChatReport,
    Friendship,
    Node,
    Notice,
    Sticker,
    User,
    utcnow,
)
from app.security import create_token, hash_password  # noqa: E402
from app.settings_store import set_value  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8131
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
W, H = 1280, 800
MW, MH = 390, 844

CHECKS: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> None:
    CHECKS.append((bool(ok), msg))


def call(path: str, token: str, method: str = "GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, method=method, data=data)
    req.add_header("Content-Type", "application/json")
    req.add_header("Cookie", f"{settings.COOKIE_NAME}={token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw or b"{}")
        except Exception:
            return err.code, {"raw": raw.decode("utf-8", "replace")}


def png_bytes(w: int = 120, h: int = 80, color=(255, 150, 190)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def gif_bytes(frames: int = 4, size: int = 96) -> bytes:
    """多帧 GIF：帧颜色各不相同，Pillow 才不会把重复帧合并掉。"""
    imgs = [
        Image.new("RGB", (size, size), (60 + index * 45, 120, 210 - index * 40))
        for index in range(frames)
    ]
    buf = io.BytesIO()
    imgs[0].save(buf, "GIF", save_all=True, append_images=imgs[1:], duration=120, loop=0)
    return buf.getvalue()


def seed() -> tuple[str, int, int]:
    """准备两个好友账号、一个单聊、一个群聊，返回 (主账号 token, 单聊 id, 群 id)。"""
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    try:
        def ensure(name: str, display: str) -> User:
            row = db.scalar(select(User).where(User.username == name))
            if row is None:
                row = User(
                    username=name,
                    display_name=display,
                    password_hash=hash_password("SmokeTest123"),
                    role=0,
                    quota_bytes=4 * 1024**3,
                    used_bytes=0,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
            return row

        a = ensure("ui_chat_a", "小樱")
        b = ensure("ui_chat_b", "阿蓝")
        c = ensure("ui_chat_c", "可可")

        conv_ids = [
            r
            for r in db.scalars(
                select(ChatMember.conversation_id).where(
                    ChatMember.user_id.in_([a.id, b.id, c.id])
                )
            ).all()
        ]
        if conv_ids:
            db.execute(delete(ChatMessage).where(ChatMessage.conversation_id.in_(conv_ids)))
            db.execute(delete(ChatReport).where(ChatReport.conversation_id.in_(conv_ids)))
            db.execute(delete(ChatMember).where(ChatMember.conversation_id.in_(conv_ids)))
            db.execute(delete(ChatConversation).where(ChatConversation.id.in_(conv_ids)))
        db.execute(delete(Sticker).where(Sticker.user_id.in_([a.id, b.id, c.id])))
        db.execute(delete(Notice).where(Notice.user_id.in_([a.id, b.id, c.id])))
        db.execute(
            delete(Friendship).where(
                Friendship.requester_id.in_([a.id, b.id, c.id])
                | Friendship.addressee_id.in_([a.id, b.id, c.id])
            )
        )
        db.add_all(
            [
                Friendship(requester_id=a.id, addressee_id=b.id, status="accepted",
                           message="", created_at=utcnow(), updated_at=utcnow()),
                Friendship(requester_id=a.id, addressee_id=c.id, status="accepted",
                           message="", created_at=utcnow(), updated_at=utcnow()),
            ]
        )
        # 给 A 一个网盘文件，用来发文件消息
        payload = b"ui-chat-attachment-bytes"
        import hashlib

        sha = hashlib.sha256(payload).hexdigest()
        target = storage.blob_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        if db.get(Blob, sha) is None:
            db.add(Blob(sha256=sha, size=len(payload), ref_count=1))
        for old in db.scalars(
            select(Node).where(Node.user_id == a.id, Node.name == "会议记录.txt")
        ).all():
            db.delete(old)
        db.add(
            Node(
                user_id=a.id,
                parent_id=None,
                type="file",
                name="会议记录.txt",
                blob_sha256=sha,
                size=len(payload),
                mime="text/plain",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        # 给 B 也放一个文件：对方发来的文件消息要同时有「下载」和「保存到我的文件」
        payload_b = b"ui-chat-from-b-bytes"
        sha_b = hashlib.sha256(payload_b).hexdigest()
        target_b = storage.blob_path(sha_b)
        target_b.parent.mkdir(parents=True, exist_ok=True)
        target_b.write_bytes(payload_b)
        if db.get(Blob, sha_b) is None:
            db.add(Blob(sha256=sha_b, size=len(payload_b), ref_count=1))
        for old in db.scalars(
            select(Node).where(Node.user_id == b.id, Node.name == "阿蓝的附件.txt")
        ).all():
            db.delete(old)
        db.add(
            Node(
                user_id=b.id,
                parent_id=None,
                type="file",
                name="阿蓝的附件.txt",
                blob_sha256=sha_b,
                size=len(payload_b),
                mime="text/plain",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        # 给 B 一张头像：A（普通用户）在会话列表里应该看得见，这条同时钉住
        # /api/avatar 的读取权限（曾经只有本人和管理员能读，普通用户看到 404）
        if not b.avatar:
            b.avatar = ps.save_avatar(b.id, png_bytes(256, 256, (90, 150, 230)))
        db.commit()
        return (
            create_token(a.id, a.token_version),
            create_token(b.id, b.token_version),
            create_token(c.id, c.token_version),
            a.id,
            b.id,
            c.id,
        )
    finally:
        db.close()


def main() -> None:
    ta, tb, tc, a_id, b_id, c_id = seed()

    # ---- 用接口造聊天内容（比手写 TABLE 行更接近真实路径）----
    code, data = call("/api/chat/conversations/direct", ta, "POST", {"userId": b_id})
    assert code == 201, data
    direct_id = data["conversation"]["id"]
    call(f"/api/chat/conversations/{direct_id}/messages", tb, "POST",
         {"kind": "text", "body": "在吗？我把整理好的表格发你"})
    # 图片（B 发）
    req = urllib.request.Request(BASE + "/api/chat/images", method="PUT", data=png_bytes())
    req.add_header("Content-Type", "image/png")
    req.add_header("Cookie", f"{settings.COOKIE_NAME}={tb}")
    with urllib.request.urlopen(req, timeout=20) as res:
        img = json.loads(res.read())
    call(f"/api/chat/conversations/{direct_id}/messages", tb, "POST",
         {"kind": "image", "payload": {"name": img["name"], "width": img["width"],
                                        "height": img["height"]}})
    # 文件（A 发）
    db = db_failover.current_sessionmaker()()
    try:
        doc = db.scalar(select(Node).where(Node.user_id == a_id, Node.name == "会议记录.txt"))
        doc_id = doc.id
    finally:
        db.close()
    call(f"/api/chat/conversations/{direct_id}/messages", ta, "POST",
         {"kind": "file", "payload": {"nodeIds": [doc_id]}})
    # 对方也发一个文件：文件卡片上「下载」和「保存到我的文件」都要能点
    db = db_failover.current_sessionmaker()()
    try:
        doc_b = db.scalar(select(Node).where(Node.user_id == b_id, Node.name == "阿蓝的附件.txt"))
        doc_b_id = doc_b.id
    finally:
        db.close()
    call(f"/api/chat/conversations/{direct_id}/messages", tb, "POST",
         {"kind": "file", "payload": {"nodeIds": [doc_b_id]}})
    # 表情（A 存一张再发）
    req = urllib.request.Request(BASE + "/api/chat/stickers?name=%E5%B0%8F%E7%86%8A",
                                 method="POST", data=png_bytes(96, 96, (120, 200, 255)))
    req.add_header("Content-Type", "image/png")
    req.add_header("Cookie", f"{settings.COOKIE_NAME}={ta}")
    with urllib.request.urlopen(req, timeout=20) as res:
        sticker = json.loads(res.read())["sticker"]
    call(f"/api/chat/conversations/{direct_id}/messages", ta, "POST",
         {"kind": "sticker", "payload": {"stickerId": sticker["id"]}})
    # 对方（B）也发一张：A 在气泡上右键应能「添加到我的表情」
    req = urllib.request.Request(BASE + "/api/chat/stickers?name=%E9%98%BF%E8%93%9D",
                                 method="POST", data=png_bytes(88, 88, (250, 170, 60)))
    req.add_header("Content-Type", "image/png")
    req.add_header("Cookie", f"{settings.COOKIE_NAME}={tb}")
    with urllib.request.urlopen(req, timeout=20) as res:
        sticker_b = json.loads(res.read())["sticker"]
    call(f"/api/chat/conversations/{direct_id}/messages", tb, "POST",
         {"kind": "sticker", "payload": {"stickerId": sticker_b["id"]}})
    # GIF 表情（B 发）：界面上应直接按 .gif 加载，不能被压成静态图
    req = urllib.request.Request(BASE + "/api/chat/stickers?name=gif",
                                 method="POST", data=gif_bytes())
    req.add_header("Content-Type", "image/gif")
    req.add_header("Cookie", f"{settings.COOKIE_NAME}={tb}")
    with urllib.request.urlopen(req, timeout=20) as res:
        sticker_gif = json.loads(res.read())["sticker"]
    call(f"/api/chat/conversations/{direct_id}/messages", tb, "POST",
         {"kind": "sticker", "payload": {"stickerId": sticker_gif["id"]}})
    # 群聊
    code, data = call("/api/chat/conversations/group", ta, "POST",
                      {"title": "周末小组", "memberIds": [b_id, c_id]})
    assert code == 201, data
    group_id = data["conversation"]["id"]
    call(f"/api/chat/conversations/{group_id}/messages", tc, "POST",
         {"kind": "text", "body": "周六上午出发可以吗"})

    profile = Path(tempfile.mkdtemp(prefix="cdp_chat_ui_"))
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=9479",
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
                    for t in json.load(urllib.request.urlopen("http://127.0.0.1:9479/json/list", timeout=2))
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
            import base64

            path = Path("D:/tmp_cn_chat/shots")
            path.mkdir(parents=True, exist_ok=True)
            (path / name).write_bytes(base64.b64decode(data_["data"]))

        send("Page.enable")
        send("Runtime.enable")
        send("Network.enable")
        send("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
        send("Network.setCookie", name=settings.COOKIE_NAME, value=ta, domain="127.0.0.1",
             path="/", httpOnly=True)
        send("Page.navigate", url=f"{BASE}/#/chat")
        assert wait_for("!!document.querySelector('.chat-side .chat-item')", 30), "聊天页没渲染"
        time.sleep(1.2)

        # ================= 桌面 =================
        layout = js(
            """
            (() => {
              const side = document.querySelector('.chat-side').getBoundingClientRect();
              const main = document.querySelector('.chat-main').getBoundingClientRect();
              const items = [...document.querySelectorAll('.chat-item')].map((el) => ({
                name: el.querySelector('.chat-item__name')?.textContent.trim(),
                preview: el.querySelector('.chat-item__preview')?.textContent.trim(),
                badge: el.querySelector('.chat-item__badge')?.textContent.trim() || '',
              }));
              return { side: side.width, main: main.width, items, overflow: document.documentElement.scrollWidth };
            })()
            """
        )
        check(layout["side"] > 100 and layout["main"] > 300, f"左右两栏（{layout['side']}/{layout['main']}）")
        names = [x["name"] for x in layout["items"]]
        check("阿蓝" in names and "周末小组" in names, f"会话列表有单聊与群聊（{names}）")
        check(any(x["badge"] for x in layout["items"]), "未读会话有角标")
        check(any("周六" in (x["preview"] or "") for x in layout["items"]), "群聊预览显示最后一条")

        # 对方头像：普通用户（A）也要能加载出 B 的头像图，而不是 404 破图
        ava = js(
            """
            (() => {
              const img = document.querySelector('.chat-ava img.avatar-img');
              if (!img) return { none: true };
              return {
                none: false,
                loaded: img.complete && img.naturalWidth > 0,
                src: img.getAttribute('src') || '',
              };
            })()
            """
        )
        check(not ava.get("none"), "会话列表给单聊画了头像图")
        check(ava.get("loaded") is True, f"普通用户能看到对方头像（{ava.get('src')}）")

        # 打开单聊
        js(f"document.querySelector('.chat-item[data-conv=\"{direct_id}\"]').click()")
        assert wait_for("!!document.querySelector('.chat-feed .msg')", 20), "消息没加载"
        time.sleep(1.2)
        check(js("location.hash") == f"#/chat/{direct_id}", f"地址栏同步（{js('location.hash')}）")

        bubbles = js(
            """
            (() => {
              const msgs = [...document.querySelectorAll('.chat-feed .msg')];
              const imgs = [...document.querySelectorAll('.msg__image img')];
              return {
                count: msgs.length,
                mine: msgs.filter((m) => m.classList.contains('msg--mine')).length,
                texts: msgs.map((m) => (m.querySelector('.msg__text')?.textContent || '').trim()).filter(Boolean),
                files: document.querySelectorAll('.msg-file').length,
                download: document.querySelectorAll('.msg-file [data-act="download"]').length,
                saves: document.querySelectorAll('.msg__save').length,
                peerFile: (() => {
                  const m = [...document.querySelectorAll('.chat-feed .msg[data-kind="file"]')]
                    .find((x) => !x.classList.contains('msg--mine'));
                  return m ? {
                    download: !!m.querySelector('[data-act="download"]'),
                    save: !!m.querySelector('[data-act="save"]'),
                    name: m.querySelector('[data-act="download"]')?.dataset.name || '',
                  } : null;
                })(),
                imgLoaded: imgs.length ? imgs.every((i) => i.complete && i.naturalWidth > 0) : false,
                sticker: document.querySelectorAll('.msg__sticker').length,
                gifStickers: [...document.querySelectorAll('.msg__sticker')]
                  .map((i) => ({ src: i.currentSrc || i.src, w: i.naturalWidth }))
                  .filter((i) => /\.gif(\?|$)/.test(i.src)),
                more: document.querySelectorAll('.msg__more').length,
                overflow: document.documentElement.scrollWidth,
              };
            })()
            """
        )
        check(bubbles["count"] >= 4, f"四条消息都渲染了（{bubbles['count']}）")
        check(bubbles["mine"] >= 2, f"自己的消息靠右（{bubbles['mine']} 条 mine）")
        check(any("在吗" in t for t in bubbles["texts"]), "对方文字显示出来了")
        check(bubbles["files"] == 2 and bubbles["download"] == 2, "两张文件卡片都带下载按钮")
        check(bubbles["saves"] == 1, "对方发来的文件消息带「保存到我的文件」")
        check((bubbles["peerFile"] or {}).get("download") is True
              and (bubbles["peerFile"] or {}).get("save") is True,
              "同一个文件消息里下载和保存两个入口都在")
        check((bubbles["peerFile"] or {}).get("name") == "阿蓝的附件.txt",
              f"下载按钮带上文件名（{(bubbles['peerFile'] or {}).get('name')}）")
        check(bubbles["imgLoaded"], "图片消息真的加载出来了")
        check(bubbles["sticker"] >= 2, f"表情消息渲染成小图（{bubbles['sticker']}）")
        check(len(bubbles["gifStickers"]) == 1, f"GIF 表情按 .gif 引用（{bubbles['gifStickers']}）")
        check(bool(bubbles["gifStickers"]) and bubbles["gifStickers"][0]["w"] > 0,
              "GIF 表情真的加载出来了")
        check(bubbles["overflow"] <= W + 1, f"桌面无横向溢出（{bubbles['overflow']}）")

        # 输入区：发文件/图片/表情三个按钮在文本框内侧左下角，发送按钮在同一行右下角
        compose = js(
            """
            (() => {
              const box = document.querySelector('.chat-compose__input');
              const tools = box?.querySelector('.chat-compose__tools');
              if (!box || !tools) return { none: true };
              const b = box.getBoundingClientRect();
              const t = tools.getBoundingClientRect();
              const rects = [...tools.querySelectorAll('.chat-tool')].map((el) => el.getBoundingClientRect());
              const send = tools.querySelector('.chat-send');
              const s = send ? send.getBoundingClientRect() : null;
              const ta = box.querySelector('textarea');
              const tr = ta ? ta.getBoundingClientRect() : null;
              return {
                none: false,
                n: rects.length,
                inside: rects.every((r) => r.left >= b.left - 1 && r.right <= b.right + 1
                                        && r.top >= b.top - 1 && r.bottom <= b.bottom + 1),
                leftGap: rects.length ? rects[0].left - b.left : -1,
                bottomGap: rects.length ? b.bottom - rects[0].bottom : -1,
                btnsWidth: rects.length ? rects[rects.length - 1].right - rects[0].left : -1,
                boxWidth: b.width,
                sendInside: !!s && s.left >= b.left && s.right <= b.right + 1
                                  && s.top >= b.top && s.bottom <= b.bottom + 1,
                sendRightGap: s ? b.right - s.right : -1,
                sendBottomGap: s ? b.bottom - s.bottom : -1,
                sendAfterTools: !!s && rects.length > 0
                                  ? s.left >= rects[rects.length - 1].right : false,
                taHeight: tr ? tr.height : 0,
              };
            })()
            """
        )
        check(not compose.get("none"), "输入区有工具按钮组")
        check(compose["n"] == 3, f"发文件/图片/表情三个按钮（{compose['n']}）")
        check(compose["inside"] is True, "按钮都在文本框内侧")
        check(0 <= compose["leftGap"] < 16, f"按钮贴着左侧（{compose['leftGap']}px）")
        check(0 <= compose["bottomGap"] < 12, f"按钮贴着下沿（{compose['bottomGap']}px）")
        check(compose["btnsWidth"] < compose["boxWidth"] * 0.6,
              f"三个按钮只占左侧一小段（{compose['btnsWidth']:.0f}/{compose['boxWidth']:.0f}）")
        check(compose["sendInside"] is True, "发送按钮在输入框里面")
        check(0 <= compose["sendRightGap"] < 16, f"发送按钮贴着右侧（{compose['sendRightGap']}px）")
        check(0 <= compose["sendBottomGap"] < 12, f"发送按钮贴着下沿（{compose['sendBottomGap']}px）")
        check(compose["sendAfterTools"] is True, "发送按钮排在三个工具按钮之后（不挤在一起）")
        check(compose["taHeight"] >= 44, f"输入框高度够打字（{compose['taHeight']:.0f}px）")

        # 回车发消息
        js(
            """
            (() => {
              const input = document.querySelector('.chat-compose__input textarea');
              input.focus();
              input.value = '界面测试：回车发送';
              input.dispatchEvent(new Event('input', { bubbles: true }));
            })()
            """
        )
        before = js("document.querySelectorAll('.chat-feed .msg').length")
        js(
            """
            (() => {
              const input = document.querySelector('.chat-compose__input textarea');
              input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
            })()
            """
        )
        check(wait_for(f"document.querySelectorAll('.chat-feed .msg').length > {before}", 12),
              "回车后新消息出现在列表里")
        time.sleep(0.4)
        check(js("document.querySelector('.chat-compose__input textarea').value") == "",
              "发送后输入框被清空")

        # 表情面板
        js("document.querySelector('[data-act=\"open-stickers\"]').click()")
        check(wait_for("!document.querySelector('[data-sticker-panel]').hidden", 8), "表情面板能打开")
        check(wait_for("document.querySelectorAll('.sticker-item').length > 0", 8), "面板里有已存的表情")
        hint = js("(document.querySelector('[data-sticker-hint]')?.textContent || '').trim()")
        check("MB" in hint and "GIF" in hint, f"表情面板显示当前大小上限（{hint}）")
        before = js("document.querySelectorAll('.chat-feed .msg').length")
        js("document.querySelector('.sticker-item__btn').click()")
        check(wait_for(f"document.querySelectorAll('.chat-feed .msg').length > {before}", 12),
              "点表情直接发出去")
        check(js("document.querySelector('[data-sticker-panel]').hidden") is True,
              "发完表情面板自动收起")

        # 会话右键：清空 / 删除
        js(
            """
            (() => {
              const el = document.querySelector('.chat-item');
              const r = el.getBoundingClientRect();
              el.dispatchEvent(new MouseEvent('contextmenu', {
                bubbles: true, cancelable: true, clientX: r.left + 30, clientY: r.top + 10,
              }));
            })()
            """
        )
        check(wait_for("!!document.querySelector('.popover')", 8), "会话右键弹出菜单")
        conv_menu = js(
            "[...document.querySelectorAll('.popover .menu-item')].map((b) => b.textContent.trim())"
        )
        check(any("清空" in x for x in conv_menu), f"会话菜单有清空聊天记录（{conv_menu}）")
        check(any("删除" in x or "退出" in x for x in conv_menu), "会话菜单有删除/退群")
        js("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))")
        check(wait_for("!document.querySelector('.popover')", 5), "Esc 能关掉会话菜单")

        # 表情气泡右键：把对方的表情收进我的
        opened = js(
            """
            (() => {
              const msg = [...document.querySelectorAll('.chat-feed .msg')]
                .find((m) => !m.classList.contains('msg--mine') && m.querySelector('.msg__sticker'));
              if (!msg) return 'no-msg';
              const r = msg.getBoundingClientRect();
              msg.dispatchEvent(new MouseEvent('contextmenu', {
                bubbles: true, cancelable: true, clientX: r.left + 8, clientY: r.top + 8,
              }));
              return 'ok';
            })()
            """
        )
        check(opened == "ok", f"找到对方发的表情气泡（{opened}）")
        check(wait_for("!!document.querySelector('.popover')", 8), "表情气泡右键弹出菜单")
        sticker_menu = js(
            "[...document.querySelectorAll('.popover .menu-item')].map((b) => b.textContent.trim())"
        )
        check(any("添加到我的表情" in x for x in sticker_menu),
              f"表情菜单有「添加到我的表情」（{sticker_menu}）")
        js(
            """
            (() => {
              const btn = [...document.querySelectorAll('.popover .menu-item')]
                .find((b) => b.textContent.includes('添加到我的表情'));
              if (btn) btn.click();
            })()
            """
        )
        time.sleep(1.0)
        code, data = call("/api/chat/stickers", ta)
        check(len(data.get("items") or []) == 2,
              f"收藏后我的表情变成两张（{len(data.get('items') or [])}）")

        # 没配右键的地方：浏览器菜单和自定义菜单都不该出现
        guard = js(
            """
            (() => {
              const ev = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
              document.querySelector('.view-title')?.dispatchEvent(ev);
              return { prevented: ev.defaultPrevented, popover: !!document.querySelector('.popover') };
            })()
            """
        )
        check(guard["prevented"] is True, "普通区域右键被吃掉（不弹浏览器菜单）")
        check(guard["popover"] is False, "普通区域右键也不弹自定义菜单")

        # 消息菜单
        js(
            """
            (() => {
              const btn = [...document.querySelectorAll('.chat-feed .msg .msg__more')][0];
              btn.click();
            })()
            """
        )
        check(wait_for("!!document.querySelector('.popover')", 8), "点「⋯」弹出菜单")
        menu = js("[...document.querySelectorAll('.popover .menu-item')].map((b) => b.textContent.trim())")
        check(any("转发" in x for x in menu), f"菜单有转发（{menu}）")
        check(any("举报" in x for x in menu) or any("撤回" in x for x in menu), "菜单有举报或撤回")

        # 撤回自己发的文字消息（菜单里选「撤回」）
        idx = js(
            """
            (() => {
              const msgs = [...document.querySelectorAll('.chat-feed .msg')];
              const i = msgs.findIndex((m) => m.classList.contains('msg--mine') && m.querySelector('.msg__text'));
              return i;
            })()
            """
        )
        if idx is not None and idx >= 0:
            js(f"document.querySelectorAll('.chat-feed .msg .msg__more')[{idx}].click()")
            assert wait_for("!!document.querySelector('.popover')", 8), "菜单没出来"
            js(
                """
                (() => {
                  const btn = [...document.querySelectorAll('.popover .menu-item')].find((b) => b.textContent.trim() === '撤回');
                  if (btn) btn.click();
                })()
                """
            )
            assert wait_for("!!document.querySelector('.dialog') || !!document.querySelector('[data-dialog]')", 8), "确认框没出来"
            clicked = js(
                """
                (() => {
                  const btns = [...document.querySelectorAll('.dialog button, [data-dialog] button')];
                  const ok = btns.find((b) => /撤回/.test(b.textContent));
                  if (ok) { ok.click(); return true; }
                  return false;
                })()
                """
            )
            check(clicked is True, "撤回确认框可点")
            check(wait_for("!!document.querySelector('.msg__recalled')", 12), "撤回后显示「已撤回」")
        else:
            check(False, "没找到自己发的文字消息（撤回用例跳过）")

        # 撤回时限：超时的消息，菜单里就不该再出现「撤回」
        code, data = call(f"/api/chat/conversations/{direct_id}/messages", ta, "POST",
                          {"kind": "text", "body": "这条很快就会过期"})
        stale_id = (data.get("message") or {}).get("id")
        cdb = db_failover.current_sessionmaker()()
        try:
            set_value(cdb, "chat_recall_minutes", "1")
            cdb.execute(
                update(ChatMessage)
                .where(ChatMessage.id == stale_id)
                .values(created_at=utcnow() - timedelta(minutes=3))
            )
            cdb.commit()
        finally:
            cdb.close()
        js("location.reload()")
        check(wait_for("document.querySelectorAll('.chat-feed .msg').length > 0", 25),
              "刷新后重新拉到消息")
        time.sleep(1.2)
        opened = js(
            f"""
            (() => {{
              const msg = [...document.querySelectorAll('.chat-feed .msg')]
                .find((m) => m.dataset.mid === '{stale_id}');
              if (!msg) return 'no-msg';
              const btn = msg.querySelector('.msg__more');
              if (!btn) return 'no-btn';
              btn.click();
              return 'ok';
            }})()
            """
        )
        if opened == "ok":
            assert wait_for("!!document.querySelector('.popover')", 8), "过期消息的菜单没出来"
            stale_menu = js(
                "[...document.querySelectorAll('.popover .menu-item')].map((b) => b.textContent.trim())"
            )
            check(not any("撤回" in x for x in stale_menu),
                  f"超时的消息菜单里没有「撤回」（{stale_menu}）")
            # 菜单是 hover 之外的浮层，不关掉会挡住后面点会话列表
            js("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))")
            check(wait_for("!document.querySelector('.popover')", 5), "按 Esc 能关掉菜单")
        else:
            check(False, f"过期消息没能打开菜单（{opened}）")
        # 还原时限与时间戳，后面的用例还要按默认走
        cdb = db_failover.current_sessionmaker()()
        try:
            set_value(cdb, "chat_recall_minutes", "10")
            cdb.execute(
                update(ChatMessage).where(ChatMessage.id == stale_id).values(created_at=utcnow())
            )
            cdb.commit()
        finally:
            cdb.close()

        # 群聊
        assert wait_for(
            f"!!document.querySelector('.chat-item[data-conv=\"{group_id}\"]')", 15
        ), "会话列表里没有群聊"
        js(f"document.querySelector('.chat-item[data-conv=\"{group_id}\"]').click()")
        assert wait_for("!!document.querySelector('.chat-thread__title')", 15), "群聊没打开"
        time.sleep(1)
        group = js(
            """
            (() => ({
              title: document.querySelector('.chat-thread__title')?.textContent.trim(),
              sub: document.querySelector('.chat-thread__sub')?.textContent.trim(),
              sys: [...document.querySelectorAll('.msg__system')].map((n) => n.textContent.trim()),
              info: !!document.querySelector('[data-act="group-info"]'),
            }))()
            """
        )
        check(group["title"] == "周末小组", f"群名显示正确（{group['title']}）")
        check("3" in (group["sub"] or ""), f"头部显示成员数（{group['sub']}）")
        check(any("创建了群聊" in x for x in group["sys"]), f"有建群系统消息（{group['sys']}）")
        check(group["info"], "群聊头部有「群聊信息」入口")
        js("document.querySelector('[data-act=\"group-info\"]').click()")
        check(wait_for("!!document.querySelector('.group-info__row')", 8), "群信息弹窗能打开")
        shot("chat-desktop-group.png")
        js("document.querySelector('.picker-backdrop')?.remove()")

        # 返回单聊并截图
        js(f"document.querySelector('.chat-item[data-conv=\"{direct_id}\"]').click()")
        time.sleep(1.2)
        shot("chat-desktop.png")

        # 好友页：更多菜单里能删除 / 拉黑，拉黑页签也能打开
        js("location.hash = '#/friends'")
        assert wait_for("!!document.querySelector('.friend-row [data-act=\"friend-more\"]')", 20), \
            "好友页没有好友行"
        js("document.querySelector('.friend-row [data-act=\"friend-more\"]').click()")
        check(wait_for("!!document.querySelector('.popover')", 8), "好友「更多」能弹出菜单")
        fmenu = js(
            "[...document.querySelectorAll('.popover .menu-item')].map((b) => b.textContent.trim())"
        )
        check(any("删除好友" in x for x in fmenu), f"好友菜单有删除（{fmenu}）")
        check(any("拉黑" in x for x in fmenu), "好友菜单有拉黑并删除")
        js("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))")
        check(wait_for("!document.querySelector('.popover')", 5), "Esc 关掉好友菜单")
        js("document.querySelector('[data-friend-tab=\"blocked\"]').click()")
        time.sleep(0.9)
        check(js("!!document.querySelector('[data-pane] .empty') || !!document.querySelector('[data-pane] .friend-list')"),
              "拉黑页签能打开")

        # ================= 手机 =================
        send("Emulation.setDeviceMetricsOverride", width=MW, height=MH, deviceScaleFactor=2, mobile=True)
        send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        js("location.hash = '#/chat'")
        time.sleep(1.6)
        mobile = js(
            """
            (() => {
              const side = document.querySelector('.chat-side');
              const main = document.querySelector('.chat-main');
              return {
                sideShown: side ? getComputedStyle(side).display !== 'none' : false,
                mainShown: main ? getComputedStyle(main).display !== 'none' : false,
                overflow: document.documentElement.scrollWidth,
                width: window.innerWidth,
              };
            })()
            """
        )
        check(mobile["sideShown"] and not mobile["mainShown"], "手机上先只显示会话列表")
        check(mobile["overflow"] <= MW + 1, f"手机列表页无横向溢出（{mobile['overflow']}）")

        js(f"document.querySelector('.chat-item[data-conv=\"{direct_id}\"]').click()")
        assert wait_for("document.querySelector('.chat-body').classList.contains('is-thread')", 15), "没进消息流"
        time.sleep(1.2)
        thread = js(
            """
            (() => {
              const back = document.querySelector('[data-act="back"]');
              return {
                mainShown: getComputedStyle(document.querySelector('.chat-main')).display !== 'none',
                sideShown: getComputedStyle(document.querySelector('.chat-side')).display !== 'none',
                backShown: back ? back.getBoundingClientRect().width > 0 : false,
                overflow: document.documentElement.scrollWidth,
                compose: !!document.querySelector('.chat-compose__input textarea'),
              };
            })()
            """
        )
        check(thread["mainShown"] and not thread["sideShown"], "点会话后只显示消息流")
        check(thread["backShown"], "手机上有返回按钮")
        check(thread["compose"], "手机上有输入框")
        check(thread["overflow"] <= MW + 1, f"手机消息流无横向溢出（{thread['overflow']}）")
        shot("chat-mobile.png")

        js("document.querySelector('[data-act=\"back\"]').click()")
        time.sleep(1)
        check(js("!document.querySelector('.chat-body').classList.contains('is-thread')"), "返回后回到列表")
        check(js("location.hash") == "#/chat", f"返回后地址回到 #/chat（{js('location.hash')}）")
        shot("chat-mobile-list.png")
    finally:
        try:
            proc.terminate()
        except Exception:
            pass

    passed = sum(1 for ok, _ in CHECKS if ok)
    for ok, msg in CHECKS:
        print(("  OK  " if ok else "  NG  ") + msg)
    print(f"\n聊天界面：{passed}/{len(CHECKS)}")
    if passed != len(CHECKS):
        sys.exit(1)


if __name__ == "__main__":
    main()
