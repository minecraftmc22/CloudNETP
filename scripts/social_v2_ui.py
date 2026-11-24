# -*- coding: utf-8 -*-
"""0.6.0 新界面的浏览器回归：个人主页 / 公开主页 / 头像名片 / 群内备注。

只跑隔离实例：
    python scripts/social_v2_ui.py [port]

覆盖：
- 自己的主页：名称、简介、社交账号、编辑资料弹窗（18 项 + 显示开关）
- 好友的主页：出现「发消息」，不出现「编辑资料」
- /u/<token> 公开主页：未登录可见、隐藏字段不泄露、无横向溢出
- 聊天里头像悬停 2.5 秒出名片，名片带「发消息」
- 群聊里对方显示我为 ta 起的群备注
"""
from __future__ import annotations

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

from PIL import Image  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from app import db_failover  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import (  # noqa: E402
    ChatConversation,
    ChatMember,
    ChatMemberNote,
    ChatMessage,
    Friendship,
    Notice,
    User,
    utcnow,
)
from app.security import create_token, hash_password  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8141
BASE = f"http://127.0.0.1:{PORT}"
W, H = 1280, 880

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
if not Path(CHROME).is_file():
    CHROME = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> None:
    RESULTS.append((bool(ok), msg))


def main() -> int:
    db_failover.startup()
    db = db_failover.current_sessionmaker()()

    def ensure_user(name: str) -> User:
        row = db.scalar(select(User).where(User.username == name))
        if not row:
            row = User(
                username=name,
                password_hash=hash_password("Passw0rd!23"),
                display_name=name,
                role=0,
                token_version=0,
                created_at=utcnow(),
            )
            db.add(row)
            db.flush()
        row.token_version = int(row.token_version or 0)
        return row

    alice = ensure_user("ui2_alice")
    bob = ensure_user("ui2_bob")
    carol = ensure_user("ui2_carol")
    dave = ensure_user("ui2_dave")
    ids = [alice.id, bob.id, carol.id, dave.id]

    # 清场
    conv_ids = list(db.scalars(select(ChatMember.conversation_id).where(ChatMember.user_id.in_(ids))).all())
    if conv_ids:
        db.execute(delete(ChatMemberNote).where(ChatMemberNote.conversation_id.in_(conv_ids)))
        db.execute(delete(ChatMessage).where(ChatMessage.conversation_id.in_(conv_ids)))
        db.execute(delete(ChatMember).where(ChatMember.conversation_id.in_(conv_ids)))
        db.execute(delete(ChatConversation).where(ChatConversation.id.in_(conv_ids)))
    db.execute(delete(Friendship).where(Friendship.requester_id.in_(ids) | Friendship.addressee_id.in_(ids)))
    db.execute(delete(Notice).where(Notice.user_id.in_(ids)))
    db.commit()

    alice.display_name = "小樱"
    alice.bio = "在写一个网盘，日常折腾前端"
    alice.email = "sakura@example.com"
    alice.email_verified = True
    alice.profile = {
        "gender": "female",
        "birthday": "2000-05-20",
        "links": {
            "qq": "10001",
            "github": "https://github.com/sakura",
            "bilibili": "https://space.bilibili.com/1",
            "wechat": "sakura_wx",
        },
        "show": {"github": True, "bilibili": True, "qq": True, "wechat": False, "email": False},
    }
    alice.profile_token = "uitoken060aaaaaaa"
    bob.display_name = "阿蓝"
    bob.bio = "只写代码"
    bob.profile = {
        "gender": "male",
        "birthday": "",
        "links": {"github": "https://github.com/alan"},
        "show": {"github": True},
    }
    bob.profile_token = None
    carol.profile = {}
    carol.profile_token = None
    dave.display_name = "小d"
    dave.profile = {}
    dave.profile_token = None

    db.add_all(
        [
            Friendship(requester_id=alice.id, addressee_id=bob.id, status="accepted",
                       message="", requester_alias="", addressee_alias="",
                       created_at=utcnow(), updated_at=utcnow()),
            Friendship(requester_id=alice.id, addressee_id=carol.id, status="accepted",
                       message="", requester_alias="", addressee_alias="",
                       created_at=utcnow(), updated_at=utcnow()),
            # 一条待处理的好友申请 + 对应的消息中心通知（老数据链接是 #/friends，
            # 用来验证点进去仍然落到「收到的」）
            Friendship(requester_id=dave.id, addressee_id=alice.id, status="pending",
                       message="我是小d", requester_alias="", addressee_alias="",
                       created_at=utcnow(), updated_at=utcnow()),
            Notice(user_id=alice.id, kind="friend_request", title="小d 想加你为好友",
                   body="我是小d", link="#/friends", created_at=utcnow()),
        ]
    )
    db.commit()

    ta = create_token(alice.id, alice.token_version)
    tb = create_token(bob.id, bob.token_version)

    def call(path: str, token: str, method: str = "GET", body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(BASE + path, method=method, data=data)
        req.add_header("Content-Type", "application/json")
        req.add_header("Cookie", f"{settings.COOKIE_NAME}={token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.status, json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as err:
            try:
                return err.code, json.loads(err.read() or b"{}")
            except Exception:
                return err.code, {}

    code, data = call("/api/chat/conversations/direct", ta, "POST", {"userId": bob.id})
    direct_id = data["conversation"]["id"]
    call(f"/api/chat/conversations/{direct_id}/messages", tb, "POST",
         {"kind": "text", "body": "名片测试：你好小樱"})
    code, data = call("/api/chat/conversations/group", ta, "POST",
                      {"title": "界面验证群", "memberIds": [bob.id, carol.id]})
    assert code == 201, data
    group_id = data["conversation"]["id"]
    call(f"/api/chat/conversations/{group_id}/messages", tb, "POST",
         {"kind": "text", "body": "群里的第一句话"})
    call(f"/api/chat/conversations/{group_id}/members/{bob.id}/note", ta, "PUT",
         {"userId": bob.id, "alias": "老二"})

    profile = Path(tempfile.mkdtemp(prefix="cdp_social2_"))
    shots = Path("D:/tmp_cn_chat/shots")
    shots.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=9481",
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
                    for t in json.load(urllib.request.urlopen("http://127.0.0.1:9481/json/list", timeout=2))
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
            return send("Runtime.evaluate", expression=expr, returnByValue=True,
                        userGesture=True)["result"].get("value")

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

            (shots / name).write_bytes(base64.b64decode(data_["data"]))

        def go(hash_path: str, wait_expr: str, timeout=30):
            js(f"location.hash = '{hash_path}'")
            return wait_for(wait_expr, timeout)

        send("Page.enable")
        send("Runtime.enable")
        send("Network.enable")
        send("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
        # 无头浏览器默认拿不到焦点，document.hasFocus() 为 false → 会走「后台通知」分支，
        # 站内卡片永远不弹。打开焦点仿真才测得到卡片。
        send("Emulation.setFocusEmulationEnabled", enabled=True)
        send("Network.setCookie", name=settings.COOKIE_NAME, value=ta, domain="127.0.0.1",
             path="/", httpOnly=True)
        send("Page.navigate", url=f"{BASE}/#/profile/{alice.id}")
        assert wait_for("!!document.querySelector('.profile-card')", 40), "个人主页没渲染"
        time.sleep(0.8)

        # ---- 入口：左侧边栏 + 右上角头像菜单 ----
        nav = js(
            """
            (() => {
              const a = document.querySelector('.nav-item[data-route="profile"]');
              return a ? {
                text: (a.textContent || '').trim(),
                href: a.getAttribute('href') || '',
                active: a.classList.contains('is-active'),
              } : null;
            })()
            """
        )
        check(bool(nav) and "个人主页" in (nav or {}).get("text", ""), f"侧边栏有「个人主页」（{nav}）")
        check(bool(nav) and nav["href"].endswith(f"/{alice.id}"), f"侧边栏入口指向本人主页（{nav}）")
        check(bool(nav) and nav["active"], "停在主页时侧边栏高亮该项")

        def esc():
            js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")

        go("#/files", "!!document.querySelector('.files-view, .files-layout, .view-body')", 20)
        time.sleep(0.4)
        js("document.getElementById('user-chip').click()")
        check(wait_for("!!document.querySelector('.popover .menu-item')", 8), "头像菜单能打开")
        menu_texts = js(
            "[...document.querySelectorAll('.popover .menu-item')].map((b) => (b.textContent || '').trim())"
        )
        check(
            any("个人主页" in x for x in (menu_texts or [])),
            f"头像菜单里有「个人主页」（{menu_texts}）",
        )
        js(
            "[...document.querySelectorAll('.popover .menu-item')]"
            ".find((b) => (b.textContent || '').includes('个人主页'))?.click()"
        )
        check(wait_for(f"location.hash === '#/profile/{alice.id}'", 10), "从头像菜单点进去落到本人主页")
        check(wait_for("!!document.querySelector('.profile-card')", 15), "从菜单进入后主页渲染出来")
        esc()
        time.sleep(0.3)

        # ---- 自己的主页 ----
        me_view = js(
            """
            (() => {
              const txt = (sel) => (document.querySelector(sel)?.textContent || '').trim();
              return {
                name: txt('.profile-card__name'),
                handle: txt('.profile-card__meta'),
                bio: txt('.profile-card__bio'),
                links: [...document.querySelectorAll('.profile-link')].map(
                  (a) => (a.textContent || '').trim()
                ),
                ops: [...document.querySelectorAll('.profile-card__ops button')]
                  .map((b) => (b.textContent || '').trim()),
                editBtn: !!document.querySelector('[data-act="edit"]'),
                shareBtn: !!document.querySelector('[data-act="share"]'),
              };
            })()
            """
        )
        body_text = js("document.body.innerText")
        check(bool(me_view) and "小樱" in (me_view.get("name") or ""), f"主页显示名称（{me_view.get('name') if me_view else None}）")
        check(bool(me_view) and "网盘" in (me_view.get("bio") or ""), f"主页显示简介（{me_view.get('bio') if me_view else None}）")
        check(bool(me_view) and me_view.get("editBtn"), "自己的主页有「编辑资料」")
        check(bool(me_view) and me_view.get("shareBtn"), "自己的主页有「分享主页」")
        check(bool(me_view) and any("GitHub" in x for x in me_view.get("links") or []),
              f"社交账号渲染出来（{me_view.get('links') if me_view else None}）")
        # 自己的主页能看到全部（包括自己关掉显示的那些），公开页才过滤
        check("sakura_wx" in body_text, "自己视角能看到被隐藏的微信")
        check("sakura@example.com" not in body_text, "邮箱在主页上默认不展开")
        shot("profile-self.png")

        # ---- 主页背景图 ----
        cover_before = js(
            """
            (() => {
              const el = document.querySelector('.profile-card__cover');
              const btns = [...document.querySelectorAll('.profile-cover__ops button')]
                .map((b) => (b.textContent || '').trim());
              return { has: !!el && el.classList.contains('has-image'), h: el ? Math.round(el.getBoundingClientRect().height) : 0, btns };
            })()
            """
        )
        check(bool(cover_before) and not cover_before["has"], f"默认用渐变、没有背景图（{cover_before}）")
        check(
            bool(cover_before) and any("设置背景" in x for x in cover_before["btns"]),
            f"自己的主页有「设置背景」（{cover_before and cover_before['btns']}）",
        )

        cover_png = profile / "cover.png"
        img = Image.new("RGB", (900, 300), (70, 120, 200))
        for yy in range(0, 300, 100):
            for xx in range(0, 900, 100):
                if (xx // 100 + yy // 100) % 2 == 0:
                    img.paste((240, 200, 120), (xx, yy, xx + 100, yy + 100))
        img.save(cover_png)

        send("DOM.enable")
        root_node = send("DOM.getDocument")["root"]["nodeId"]
        file_node = send("DOM.querySelector", nodeId=root_node, selector="[data-cover-input]")["nodeId"]
        send("DOM.setFileInputFiles", files=[str(cover_png)], nodeId=file_node)
        check(wait_for("!!document.querySelector('.profile-card__cover.has-image')", 25), "上传后主页出现背景图")

        cover = js(
            """
            (() => {
              const el = document.querySelector('.profile-card__cover');
              if (!el) return null;
              const cs = getComputedStyle(el);
              return {
                image: cs.backgroundImage,
                h: Math.round(el.getBoundingClientRect().height),
                btns: [...document.querySelectorAll('.profile-cover__ops button')]
                  .map((b) => (b.textContent || '').trim()),
              };
            })()
            """
        )
        check("/api/profile-cover/" in ((cover or {}).get("image") or ""), f"背景图指向封面上传接口（{cover and cover['image'][:80]}）")
        check(bool(cover) and cover["h"] >= 150, f"有图时横幅加高（{cover and cover['h']}px）")
        check(
            bool(cover) and any("更换背景" in x for x in cover["btns"]) and any("移除" in x for x in cover["btns"]),
            f"有图后按钮变成「更换背景 / 移除」（{cover and cover['btns']}）",
        )
        shot("profile-cover.png")

        js(
            "[...document.querySelectorAll('.profile-cover__ops button')]"
            ".find((b) => (b.textContent || '').includes('移除'))?.click()"
        )
        check(wait_for("!!document.querySelector('.dialog [data-act=\"ok\"]')", 8), "移除背景会先确认")
        js("document.querySelector('.dialog [data-act=\"ok\"]').click()")
        check(wait_for("!document.querySelector('.profile-card__cover.has-image')", 20), "确认后背景被移除")

        # ---- 编辑资料弹窗 ----
        js("document.querySelector('[data-act=\"edit\"]').click()")
        check(wait_for("!!document.querySelector('.profile-form__links')", 10), "编辑资料弹窗能打开")
        edit = js(
            """
            (() => {
              const box = document.querySelector('.picker');
              if (!box) return null;
              return {
                fields: box.querySelectorAll('.profile-form__link').length,
                toggles: box.querySelectorAll('input[data-show]').length,
                hasGender: /性别/.test(box.innerText),
                hasBirthday: /出生日期/.test(box.innerText),
                hasGithub: /GitHub/.test(box.innerText),
                hasEmail: /邮箱/.test(box.innerText),
              };
            })()
            """
        )
        check(bool(edit) and edit["fields"] >= 18, f"编辑弹窗里 18 个可填项（{edit and edit['fields']}）")
        check(bool(edit) and edit["toggles"] >= 18, f"每一项都能单独决定是否显示（{edit and edit['toggles']}）")
        check(bool(edit) and edit["hasGender"] and edit["hasBirthday"], "弹窗里有性别与出生日期")
        check(bool(edit) and edit["hasGithub"], "弹窗里有社交账号输入框")
        shot("profile-edit.png")
        js("document.querySelector('.picker [data-act=\"cancel\"]')?.click()")
        time.sleep(0.6)

        # ---- 好友的主页 ----
        js(f"location.hash = '#/profile/{bob.id}'")
        check(wait_for("!!document.querySelector('.profile-card')", 25), "能打开别人的主页")
        time.sleep(0.8)
        check(
            not js(
                "document.querySelector('.nav-item[data-route=\"profile\"]').classList.contains('is-active')"
            ),
            "看别人主页时侧边栏的「个人主页」不高亮",
        )
        check(
            not js("!!document.querySelector('.profile-cover__ops')"),
            "别人的主页没有「换背景」按钮",
        )
        other = js(
            """
            (() => {
              const txt = (sel) => (document.querySelector(sel)?.textContent || '').trim();
              return {
                name: txt('.profile-card__name'),
                edit: !!document.querySelector('[data-act="edit"]'),
                chat: !!document.querySelector('[data-act="chat"]'),
                add: !!document.querySelector('[data-act="add"]'),
                ops: [...document.querySelectorAll('.profile-card__ops button')]
                  .map((b) => (b.textContent || '').trim()),
              };
            })()
            """
        )
        check(bool(other) and "阿蓝" in (other.get("name") or ""), f"对方主页显示昵称（{other and other.get('name')}）")
        check(bool(other) and not other["edit"], "别人的主页没有「编辑资料」")
        check(bool(other) and other["chat"], f"好友的主页有「发消息」（{other and other.get('ops')}）")
        shot("profile-other.png")

        # ---- 公开主页（不带 cookie）----
        send("Network.clearBrowserCookies")
        send("Page.navigate", url=f"{BASE}/u/{alice.profile_token}")
        check(wait_for("!!document.querySelector('.profile-card')", 30), "公开主页能打开")
        time.sleep(0.8)
        pub = js(
            """
            (() => ({
              text: document.body.innerText,
              links: document.querySelectorAll('.profile-link').length,
              name: (document.querySelector('.profile-card__name')?.textContent || '').trim(),
              overflow: document.documentElement.scrollWidth,
            }))()
            """
        )
        check(bool(pub) and "小樱" in (pub.get("text") or ""), "公开页显示名称")
        check(bool(pub) and "网盘" in (pub.get("text") or ""), "公开页显示简介")
        check(bool(pub) and "sakura_wx" not in pub["text"], "公开页不含被隐藏的微信")
        check(bool(pub) and "sakura@example.com" not in pub["text"], "公开页不含邮箱")
        check(bool(pub) and pub["links"] >= 1, f"公开页有社交账号（{pub and pub['links']}）")
        check(bool(pub) and pub["overflow"] <= W + 1, f"公开页无横向溢出（{pub and pub['overflow']}）")
        shot("profile-public.png")

        # ---- 回到 App：头像名片 ----
        send("Network.setCookie", name=settings.COOKIE_NAME, value=ta, domain="127.0.0.1",
             path="/", httpOnly=True)
        send("Page.navigate", url=f"{BASE}/#/chat/{direct_id}")
        assert wait_for("!!document.querySelector('.chat-feed .msg')", 40), "聊天没打开"
        time.sleep(1.0)
        js(
            """
            (() => {
              const ava = document.querySelector('.msg__avatar[data-uid]');
              if (ava) ava.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
            })()
            """
        )
        check(wait_for("!!document.querySelector('.usercard')", 12), "头像悬停出名片")
        card = js(
            """
            (() => {
              const el = document.querySelector('.usercard');
              if (!el) return null;
              const r = el.getBoundingClientRect();
              return {
                name: (el.querySelector('.usercard__name')?.textContent || '').trim(),
                ops: [...el.querySelectorAll('.usercard__ops button, .usercard__ops a')]
                  .map((b) => (b.textContent || '').trim()),
                links: el.querySelectorAll('.usercard__link').length,
                inside: r.left >= 0 && r.top >= 0 && r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1,
              };
            })()
            """
        )
        check(bool(card) and "阿蓝" in (card.get("name") or ""), f"名片显示昵称（{card and card.get('name')}）")
        check(bool(card) and any("发消息" in x for x in card.get("ops") or []),
              f"好友的名片给「发消息」（{card and card.get('ops')}）")
        check(bool(card) and card["links"] >= 1, f"名片里带社交账号（{card and card['links']}）")
        check(bool(card) and card["inside"], "名片没跑出可视区")
        shot("chat-usercard.png")
        js(
            """
            (() => {
              const ava = document.querySelector('.msg__avatar[data-uid]');
              if (ava) ava.dispatchEvent(new MouseEvent('mouseout', { bubbles: true }));
            })()
            """
        )
        check(wait_for("!document.querySelector('.usercard')", 6), "移开鼠标名片消失")

        # ---- 群聊：群内备注 ----
        js(f"location.hash = '#/chat/{group_id}'")
        check(wait_for("!!document.querySelector('.chat-feed .msg .msg__name')", 30), "群聊打开且有名字行")
        time.sleep(0.6)
        names = js(
            "[...document.querySelectorAll('.chat-feed .msg__name')].map((n) => n.textContent.trim())"
        )
        check(any("老二" in n for n in names or []), f"群里显示我给对方起的备注（{names}）")
        check(all("阿蓝" not in n for n in names or []), "备注盖住了原昵称")

        # ---- 消息提醒卡片（页面在前台时右下角小卡片）----
        call(f"/api/chat/conversations/{direct_id}/messages", tb, "POST",
             {"kind": "text", "body": "提醒卡片测试：在忙吗"})
        check(wait_for("!!document.querySelector('.notify-card')", 45), "别的会话来消息会弹提醒卡片")
        note = js(
            """
            (() => {
              const card = document.querySelector('.notify-card');
              if (!card) return null;
              const r = card.getBoundingClientRect();
              return {
                text: card.innerText,
                title: (card.querySelector('.notify-card__title')?.textContent || '').trim(),
                right: window.innerWidth - r.right,
                inside: r.bottom <= window.innerHeight + 1 && r.right <= window.innerWidth + 1,
              };
            })()
            """
        )
        check(bool(note) and "提醒卡片测试" in (note.get("text") or ""), f"卡片带消息内容（{note and note.get('title')}）")
        check(bool(note) and "阿蓝" in (note.get("title") or ""), f"卡片带发送者（{note and note.get('title')}）")
        check(bool(note) and note["inside"] and note["right"] < 40, "卡片贴在右下角且不出屏")
        shot("chat-notify.png")
        js("document.querySelector('.notify-card__main').click()")
        check(wait_for(f"location.hash === '#/chat/{direct_id}'", 12), "点卡片跳到对应会话")

        # ---- 消息中心里的好友申请：点进去落到「好友 - 收到的」 ----
        js("location.hash = '#/notices'")
        check(wait_for("document.querySelectorAll('.notice-item').length >= 1", 25), "消息中心有通知")
        req = js(
            """
            (() => {
              const item = [...document.querySelectorAll('.notice-item')]
                .find((n) => /想加你为好友/.test(n.innerText));
              return item ? { link: item.dataset.link, text: item.innerText.trim().slice(0, 30) } : null;
            })()
            """
        )
        check(bool(req), f"消息中心里有好友申请（{req}）")
        js(
            """
            [...document.querySelectorAll('.notice-item')]
              .find((n) => /想加你为好友/.test(n.innerText))?.click()
            """
        )
        check(wait_for("location.hash === '#/friends/incoming'", 15),
              f"好友申请跳到好友-收到的（{js('location.hash')}）")
        time.sleep(0.9)
        check(js(
            "document.querySelector('[data-friend-tab=\"incoming\"]')?.classList.contains('is-active')"
        ) is True, "「收到的」页签被选中")
        incoming = js("document.querySelector('[data-pane]')?.innerText || ''")
        check("小d" in (incoming or ""), f"收到的申请里能看到对方（{incoming.strip()[:30]}）")
        shot("notices-friend-request.png")
    finally:
        try:
            ws.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        db.close()

    ok = sum(1 for flag, _ in RESULTS if flag)
    for flag, msg in RESULTS:
        print(("  OK  " if flag else "  NG  ") + msg)
    print(f"\n新界面回归：{ok}/{len(RESULTS)}")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
