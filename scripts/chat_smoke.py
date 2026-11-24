# -*- coding: utf-8 -*-
"""站内聊天接口冒烟（跑在隔离库上）。

覆盖：单聊开通 / 非好友拦截 / 发文本 / 未读与已读 / 图片上传 / 网盘文件转发与下载与保存 /
撤回 / 转发 / 群聊（建群、群消息、退群、改群名）/ 表情包（上传、发送、上限、删除）/
举报（重复拦截、后台下架、二次处理上报错）/ 越权读取 / 未读合计。

用法：
    DATABASE_URL=... STORAGE_DIR=... python scripts/chat_smoke.py [port]
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from PIL import Image  # noqa: E402
from sqlalchemy import delete, select, update  # noqa: E402

from app import db_failover, storage  # noqa: E402
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

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> None:
    RESULTS.append((bool(ok), msg))


def call(path: str, token: str | None = None, method: str = "GET", body=None):
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, method=method, data=data)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Cookie", f"{settings.COOKIE_NAME}={token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read()
            try:
                return res.status, json.loads(raw or b"{}")
            except Exception:
                return res.status, {"raw": raw}
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw or b"{}")
        except Exception:
            return err.code, {"raw": raw.decode("utf-8", "replace")}


def call_raw(path: str, token: str, data: bytes, content_type: str = "image/png", method: str = "PUT"):
    req = urllib.request.Request(BASE + path, method=method, data=data)
    req.add_header("Content-Type", content_type)
    req.add_header("Cookie", f"{settings.COOKIE_NAME}={token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as err:
        # 服务端按上限提前掐断时可能直接关连接，读 body 会 ConnectionReset
        try:
            raw = err.read()
        except Exception:
            raw = b""
        try:
            return err.code, json.loads(raw or b"{}")
        except Exception:
            return err.code, {"raw": raw.decode("utf-8", "replace")}
    except ConnectionError:
        # 大包被服务端拒收时也可能是「连接被重置」而不是 HTTPError
        return 413, {"detail": "连接被重置"}


def fetch_bytes(path: str, token: str):
    req = urllib.request.Request(BASE + path)
    req.add_header("Cookie", f"{settings.COOKIE_NAME}={token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def png_bytes(w: int = 60, h: int = 40, color=(255, 120, 180)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def noise_png(size: int = 800) -> bytes:
    """随机噪点 PNG：压不下去，用来撞「单张大小上限」。"""
    img = Image.frombytes("RGB", (size, size), os.urandom(size * size * 3))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def gif_bytes(frames: int = 4, size: int = 64, noise: bool = False) -> bytes:
    """多帧 GIF：size 小的时候体积也小，noise=True 用来堆出大文件。"""
    imgs = [
        Image.frombytes("RGB", (size, size), os.urandom(size * size * 3))
        if noise
        else Image.new("RGB", (size, size), (40 + index * 20, 120, 200 - index * 15))
        for index in range(frames)
    ]
    buf = io.BytesIO()
    imgs[0].save(buf, "GIF", save_all=True, append_images=imgs[1:], duration=80, loop=0)
    return buf.getvalue()


def fetch_detail(path: str, token: str):
    """和 fetch_bytes 一样，但顺带把响应头带出来（校验 Content-Type）。

    返回的是 ``email.message.Message``：HTTP 头大小写不敏感，转成 dict 会让
    ``headers["Content-Type"]`` 取不到值（服务端发的是小写）。
    """
    req = urllib.request.Request(BASE + path)
    req.add_header("Cookie", f"{settings.COOKIE_NAME}={token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, res.headers, res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()


def main() -> None:
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    try:
        admin = db.scalar(select(User).where(User.role == 1).order_by(User.id.asc()))
        assert admin, "隔离库里没有管理员账号"

        def ensure_user(name: str) -> User:
            row = db.scalar(select(User).where(User.username == name))
            if row is None:
                row = User(
                    username=name,
                    password_hash=hash_password("SmokeTest123"),
                    role=0,
                    quota_bytes=4 * 1024**3,
                    used_bytes=0,
                    display_name=name,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
            return row

        alice = ensure_user("chat_alice")
        bob = ensure_user("chat_bob")
        carol = ensure_user("chat_carol")

        # ---- 清场：删除这三个账号参与的会话（成员/消息靠外键级联）与好友关系 ----
        conv_ids = [
            r
            for r in db.scalars(
                select(ChatMember.conversation_id).where(
                    ChatMember.user_id.in_([alice.id, bob.id, carol.id])
                )
            ).all()
        ]
        if conv_ids:
            db.execute(delete(ChatMessage).where(ChatMessage.conversation_id.in_(conv_ids)))
            db.execute(delete(ChatReport).where(ChatReport.conversation_id.in_(conv_ids)))
            db.execute(delete(ChatMember).where(ChatMember.conversation_id.in_(conv_ids)))
            db.execute(delete(ChatConversation).where(ChatConversation.id.in_(conv_ids)))
        db.execute(delete(Sticker).where(Sticker.user_id.in_([alice.id, bob.id, carol.id])))
        db.execute(delete(Notice).where(Notice.user_id.in_([alice.id, bob.id, carol.id])))
        db.execute(
            delete(Friendship).where(
                Friendship.requester_id.in_([alice.id, bob.id, carol.id])
                | Friendship.addressee_id.in_([alice.id, bob.id, carol.id])
            )
        )
        db.add_all(
            [
                Friendship(requester_id=alice.id, addressee_id=bob.id, status="accepted",
                           message="", created_at=utcnow(), updated_at=utcnow()),
                Friendship(requester_id=alice.id, addressee_id=carol.id, status="accepted",
                           message="", created_at=utcnow(), updated_at=utcnow()),
            ]
        )
        db.commit()

        # 造一个 alice 的网盘文件（真写 blob，聊天里下载/保存都要用）
        payload = b"chat-file-payload-1234567890"
        import hashlib

        sha = hashlib.sha256(payload).hexdigest()
        target = storage.blob_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        for old in db.scalars(
            select(Node).where(Node.user_id == alice.id, Node.name == "chat-doc.txt")
        ).all():
            db.delete(old)
        if db.get(Blob, sha) is None:
            db.add(Blob(sha256=sha, size=len(payload), ref_count=1))
        doc = Node(
            user_id=alice.id,
            parent_id=None,
            type="file",
            name="chat-doc.txt",
            blob_sha256=sha,
            size=len(payload),
            mime="text/plain",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)

        ta = create_token(alice.id, alice.token_version)
        tb = create_token(bob.id, bob.token_version)
        tc = create_token(carol.id, carol.token_version)
        tn = create_token(admin.id, admin.token_version)
        alice_id, bob_id, carol_id = alice.id, bob.id, carol.id
        doc_id = doc.id
    finally:
        db.close()

    # ================= 1) 单聊 =================
    code, data = call("/api/chat/conversations/direct", tb, "POST", {"userId": carol_id})
    check(code == 403, f"非好友不能开单聊（{code}）")

    code, data = call("/api/chat/conversations/direct", ta, "POST", {"userId": bob_id})
    check(code == 201, f"好友之间能开单聊（{code}）")
    conv = data.get("conversation") or {}
    conv_id = conv.get("id")
    check(conv.get("kind") == "direct", "会话类型是单聊")
    check(conv.get("memberCount") == 2, "单聊有两位成员")

    code, data2 = call("/api/chat/conversations/direct", ta, "POST", {"userId": bob_id})
    check(code == 201 and (data2.get("conversation") or {}).get("id") == conv_id, "重复开单聊复用同一个会话")

    code, data = call(f"/api/chat/conversations/{conv_id}/messages", ta, "POST",
                      {"kind": "text", "body": "  你好 Bob  "})
    check(code == 201, f"发文本消息（{code}）")
    m1 = data.get("message") or {}
    check(m1.get("body") == "你好 Bob", "首尾空格被去掉")
    check(m1.get("sender", {}).get("id") == alice_id, "发送者是自己")

    code, data = call(f"/api/chat/conversations/{conv_id}/messages", ta, "POST",
                      {"kind": "text", "body": "   "})
    check(code == 400, f"空消息被拦（{code}）")

    code, data = call(f"/api/chat/conversations/{conv_id}/messages", ta, "POST",
                      {"kind": "text", "body": "x" * 2001})
    check(code == 422 or code == 400, f"超长消息被拦（{code}）")

    code, data = call("/api/chat/conversations", tb)
    items = data.get("items") or []
    hit = next((x for x in items if x["id"] == conv_id), None)
    check(hit is not None, "Bob 的会话列表里有这个单聊")
    check((hit or {}).get("unread") == 1, f"Bob 未读 1（{(hit or {}).get('unread')}）")
    check((hit or {}).get("lastMessage") == "你好 Bob", f"列表预览显示最后一条（{(hit or {}).get('lastMessage')}）")
    check((hit or {}).get("title") == "chat_alice", f"单聊标题取对方昵称（{(hit or {}).get('title')}）")

    code, data = call("/api/chat/unread", tb)
    check(data.get("unread") == 1, f"未读合计 1（{data.get('unread')}）")

    code, data = call(f"/api/chat/conversations/{conv_id}/messages", tb)
    check(code == 200 and len(data.get("items") or []) == 1, "Bob 能读到这条消息")

    msg1_id = m1.get("id")
    code, data = call(f"/api/chat/conversations/{conv_id}/read", tb, "POST",
                      {"messageId": msg1_id})
    check(code == 200, f"标记已读（{code}）")
    code, data = call("/api/chat/conversations", tb)
    hit = next((x for x in (data.get("items") or []) if x["id"] == conv_id), None)
    check((hit or {}).get("unread") == 0, "已读后未读归零")

    code, data = call(f"/api/chat/conversations/{conv_id}/messages", tb, "POST",
                      {"kind": "text", "body": "收到啦"})
    check(code == 201, f"Bob 回复（{code}）")
    code, data = call("/api/chat/unread", ta)
    check(data.get("unread") == 1, f"Alice 未读 1（{data.get('unread')}）")

    # ---- 引用回复 ----
    code, data = call(f"/api/chat/conversations/{conv_id}/messages", tb, "POST",
                      {"kind": "text", "body": "引用一下", "replyTo": msg1_id})
    check(code == 201, f"能发引用消息（{code}）")
    quote = (data.get("message") or {}).get("reply") or {}
    check(quote.get("id") == msg1_id, "引用带回原消息 id")
    check(quote.get("senderName") == "chat_alice", f"引用块带原发送者（{quote.get('senderName')}）")
    check(quote.get("text") == "你好 Bob", f"引用块带摘要（{quote.get('text')}）")

    code, data = call(f"/api/chat/conversations/{conv_id}/messages", ta)
    hit = next((m for m in (data.get("items") or []) if (m.get("reply") or {}).get("id") == msg1_id), None)
    check(hit is not None, "消息列表里也带引用信息")

    code, data = call(f"/api/chat/conversations/{conv_id}/messages", ta, "POST",
                      {"kind": "text", "body": "引用一条不存在的", "replyTo": 99999999})
    check(code == 400, f"引用不存在的消息被拦（{code}）")

    # 单聊新消息 → 消息中心一条（只在对方已读完时提示，不会每条都响）
    code, data = call("/api/notices?unreadOnly=true", tb)
    chat_notices = [n for n in (data.get("items") or []) if n.get("kind") == "chat"]
    check(len(chat_notices) == 1, f"消息中心有 1 条聊天通知（{len(chat_notices)}）")
    check((chat_notices[0] if chat_notices else {}).get("link") == f"#/chat/{conv_id}",
          "通知深链指向该会话")

    # 越权：carol 读 alice-bob 的消息
    code, data = call(f"/api/chat/conversations/{conv_id}/messages", tc)
    check(code == 403, f"不在会话里的人读不到消息（{code}）")

    # ================= 2) 图片 =================
    code, data = call_raw("/api/chat/images", ta, png_bytes())
    check(code == 200, f"上传聊天图片（{code}）")
    img = data or {}
    check(str(img.get("name", "")).startswith("img-"), "图片落在 img- 前缀下")
    check(img.get("width") == 60 and img.get("height") == 40, "返回图片宽高")
    code, raw = fetch_bytes(f"/api/chat/media/{img.get('name')}", tb)
    check(code == 200 and len(raw) > 0, f"Bob 能取到图片（{code}）")
    req = urllib.request.Request(BASE + f"/api/chat/media/{img.get('name')}")
    try:
        urllib.request.urlopen(req, timeout=10)
        code_anon = 200
    except urllib.error.HTTPError as err:
        code_anon = err.code
    check(code_anon == 401, f"未登录取不到图片（{code_anon}）")

    code, data = call(f"/api/chat/conversations/{conv_id}/messages", ta, "POST",
                      {"kind": "image", "payload": {"name": img.get("name"),
                                                    "width": img.get("width"),
                                                    "height": img.get("height")}})
    check(code == 201, f"发图片消息（{code}）")
    img_msg = data.get("message") or {}
    check((img_msg.get("payload") or {}).get("name") == img.get("name"), "图片消息带文件名")

    code, data = call(f"/api/chat/conversations/{conv_id}/messages", ta, "POST",
                      {"kind": "image", "payload": {"name": "../etc/passwd"}})
    check(code == 400, f"非法图片名被拦（{code}）")

    # ================= 3) 网盘文件 =================
    code, data = call(f"/api/chat/conversations/{conv_id}/messages", ta, "POST",
                      {"kind": "file", "payload": {"nodeIds": [doc_id]}})
    check(code == 201, f"发文件消息（{code}）")
    file_msg = data.get("message") or {}
    nodes = (file_msg.get("payload") or {}).get("nodes") or []
    check(len(nodes) == 1 and nodes[0].get("name") == "chat-doc.txt", "文件消息带文件清单")
    check(nodes and nodes[0].get("icon") == "i-text", "文件图标按类型给出")
    file_msg_id = file_msg.get("id")

    code, raw = fetch_bytes(f"/api/chat/messages/{file_msg_id}/files/0", tb)
    check(code == 200 and raw == b"chat-file-payload-1234567890", f"Bob 能下载文件（{code}）")
    code, raw = fetch_bytes(f"/api/chat/messages/{file_msg_id}/files/5", tb)
    check(code == 404, f"越界序号 404（{code}）")
    code, raw = fetch_bytes(f"/api/chat/messages/{file_msg_id}/files/0", tc)
    check(code == 403, f"非成员下载被拒（{code}）")

    code, data = call(f"/api/chat/messages/{file_msg_id}/save", tb, "POST", {})
    check(code == 200 and data.get("saved") == 1, f"Bob 保存到我的文件（{data}）")
    check(data.get("folder") == "聊天文件", "保存进「聊天文件」目录")

    # Alice 把文件删掉后，Bob 再下载应 404 而不是 500
    code, data = call(f"/api/chat/messages/{file_msg_id}/files/0", tb)
    check(code == 200, "删之前仍可下载")

    # ================= 4) 撤回 =================
    code, data = call(f"/api/chat/messages/{msg1_id}/recall", tb, "POST", {})
    check(code == 403, f"不能撤回别人的消息（{code}）")
    code, data = call(f"/api/chat/messages/{msg1_id}/recall", ta, "POST", {})
    check(code == 200, f"撤回自己的消息（{code}）")
    recalled = data.get("message") or {}
    check(recalled.get("recalled") is True and not recalled.get("body"), "撤回后正文清空")

    code, data = call(f"/api/chat/conversations/{conv_id}/messages", tb)
    hit = next((m for m in (data.get("items") or []) if m["id"] == msg1_id), None)
    check((hit or {}).get("recalled") is True, "对方看到的是「已撤回」")
    check(not (hit or {}).get("body"), "已撤回的消息不带正文")

    # 撤回后不能再被引用；已有的引用块会显示「消息已撤回」
    code, data = call(f"/api/chat/conversations/{conv_id}/messages", tb, "POST",
                      {"kind": "text", "body": "引用已撤回的", "replyTo": msg1_id})
    check(code == 400, f"已撤回的消息不能被引用（{code}）")
    code, data = call(f"/api/chat/conversations/{conv_id}/messages", ta)
    holder = next((m for m in (data.get("items") or []) if (m.get("reply") or {}).get("id") == msg1_id), None)
    check((holder or {}).get("reply", {}).get("recalled") is True, "引用块跟着变成「已撤回」")

    # ---- 撤回时限：后台设的分钟数要真的管用，前端也要拿到 ----
    code, data = call("/api/chat/friends", ta)
    check((data or {}).get("recallMinutes") == 10, f"选人面板带回撤回时限（{(data or {}).get('recallMinutes')}）")

    code, data = call(f"/api/chat/conversations/{conv_id}/messages", ta, "POST",
                      {"kind": "text", "body": "这条专门用来测撤回时限"})
    limit_id = (data.get("message") or {}).get("id")
    check((data.get("message") or {}).get("recallable") is True, "刚发的消息标记为可撤回")

    # 后台把时限改成 1 分钟：管理员改完，聊天侧立刻读到
    code, data = call("/api/admin/settings", tn, "PATCH", {"chatRecallMinutes": 1})
    check(code == 200, f"管理员改撤回时限（{code}）")
    check((data.get("settings") or {}).get("chatRecallMinutes") == 1, "设置回显新的撤回时限")
    code, data = call("/api/chat/friends", ta)
    check((data or {}).get("recallMinutes") == 1,
          f"聊天侧立刻读到新时限（{(data or {}).get('recallMinutes')}）")

    # 把消息挪到 2 分钟前 → 不可撤回，接口也拒
    db.execute(
        update(ChatMessage)
        .where(ChatMessage.id == limit_id)
        .values(created_at=utcnow() - timedelta(minutes=2))
    )
    db.commit()
    code, data = call(f"/api/chat/conversations/{conv_id}/messages", ta)
    hit = next((m for m in (data.get("items") or []) if m["id"] == limit_id), None)
    check((hit or {}).get("recallable") is False, "超过时限的消息标记为不可撤回")
    code, data = call(f"/api/chat/messages/{limit_id}/recall", ta, "POST", {})
    check(code == 400, f"超过时限撤回被拒（{code}）")

    # 时限 0 = 不限：同一条老消息又能撤回
    code, data = call("/api/admin/settings", tn, "PATCH", {"chatRecallMinutes": 0})
    check(code == 200, f"时限改成 0（{code}）")
    code, data = call(f"/api/chat/conversations/{conv_id}/messages", ta)
    hit = next((m for m in (data.get("items") or []) if m["id"] == limit_id), None)
    check((hit or {}).get("recallable") is True, "时限填 0 表示不限，老消息也可撤回")
    code, data = call(f"/api/chat/messages/{limit_id}/recall", ta, "POST", {})
    check(code == 200, f"不限时限时撤回成功（{code}）")

    # 还原默认值，后面的断言按 10 分钟走
    code, data = call("/api/admin/settings", tn, "PATCH", {"chatRecallMinutes": 10})
    check(code == 200 and (data.get("settings") or {}).get("chatRecallMinutes") == 10,
          f"撤回时限还原为 10（{code}）")

    code, data = call(f"/api/chat/messages/{file_msg_id}/forward", tb, "POST",
                      {"conversationIds": [conv_id]})
    check(code == 400, f"转发别人的文件消息被拦（{code}）")

    # ================= 5) 群聊 =================
    code, data = call("/api/chat/conversations/group", ta, "POST",
                      {"title": "", "memberIds": [bob_id, carol_id]})
    check(code == 201, f"建群（{code}）")
    gconv = data.get("conversation") or {}
    gid = gconv.get("id")
    check(gconv.get("kind") == "group" and gconv.get("memberCount") == 3, "群有 3 人")
    check(bool(gconv.get("title")), f"没填群名时自动拼一个（{gconv.get('title')}）")
    check(gconv.get("isOwner") is True, "建群的人是群主")

    code, data = call(f"/api/chat/conversations/{gid}/messages", ta)
    first = (data.get("items") or [])[0]
    check(first.get("kind") == "system", "建群后有一条系统消息")

    code, data = call(f"/api/chat/conversations/{gid}/messages", tc, "POST",
                      {"kind": "text", "body": "大家好"})
    check(code == 201, f"群成员都能发言（{code}）")
    code, data = call("/api/chat/conversations", tb)
    hit = next((x for x in (data.get("items") or []) if x["id"] == gid), None)
    check((hit or {}).get("unread") == 1, "群里未读也按 last_read_id 算")

    code, data = call(f"/api/chat/conversations/{gid}", ta, "PATCH", {"title": "周末组"})
    check(code == 200 and (data.get("conversation") or {}).get("title") == "周末组", "群主能改群名")
    code, data = call(f"/api/chat/conversations/{gid}", tb, "PATCH", {"title": "我不是群主"})
    check(code == 403, f"非群主不能改群名（{code}）")

    code, data = call(f"/api/chat/conversations/{gid}/members", tb, "POST", {"userIds": [carol_id]})
    check(code == 403, f"非群主不能邀请（{code}）")

    code, data = call(f"/api/chat/conversations/{gid}/leave", tc, "POST", {})
    check(code == 200 and data.get("left"), f"成员能退群（{data}）")
    code, data = call(f"/api/chat/conversations/{gid}/messages", tc)
    check(code == 403, f"退群后读不到消息（{code}）")
    code, data = call(f"/api/chat/conversations/{gid}", ta)
    check((data.get("conversation") or {}).get("memberCount") == 2, "群成员数减一")

    # ================= 6) 表情包 =================
    code, data = call_raw(
        "/api/chat/stickers?name=%E7%AC%91", ta, png_bytes(80, 80, (120, 200, 255)), method="POST"
    )
    check(code == 201, f"上传表情（{code}）")
    sticker = data.get("sticker") or {}
    sticker_file = str(sticker.get("url") or "").rsplit("/", 1)[-1]
    check(sticker.get("url", "").startswith("/api/chat/media/stk-"), "表情文件用 stk- 前缀")
    check(data.get("limit") == 100, f"默认上限 100（{data.get('limit')}）")

    code, data = call("/api/chat/stickers", tb)
    check(len(data.get("items") or []) == 0, "表情包按人隔离")

    # ---- GIF 动图：原样保存、按 image/gif 下发、帧数不丢 ----
    # 下面几条断言依赖默认 2 MB / 6 MB，先清掉上一次跑测试可能留下的后台改动
    tdb = db_failover.current_sessionmaker()()
    try:
        set_value(tdb, "sticker_max_mb", "2")
        set_value(tdb, "sticker_gif_max_mb", "6")
        tdb.commit()
    finally:
        tdb.close()
    gif = gif_bytes(4, 64)
    code, data = call_raw("/api/chat/stickers?name=gif", ta, gif, content_type="image/gif", method="POST")
    check(code == 201, f"上传 GIF 表情（{code}）")
    gif_sticker = data.get("sticker") or {}
    check(str(gif_sticker.get("url") or "").endswith(".gif"),
          f"GIF 存成 .gif（{gif_sticker.get('url')}）")
    gif_name = str(gif_sticker.get("url") or "").rsplit("/", 1)[-1]
    code, headers, raw = fetch_detail(f"/api/chat/media/{gif_name}", tb)
    check(code == 200 and headers.get("Content-Type", "").startswith("image/gif"),
          f"GIF 以 image/gif 下发（{headers.get('Content-Type')}）")
    check(raw == gif, "GIF 字节原样保存（没被转成静态 webp）")
    with Image.open(io.BytesIO(raw)) as img:
        frames = int(getattr(img, "n_frames", 1) or 1)
    check(frames == 4, f"动图帧数保留（{frames}）")

    # 表情面板要能拿到单张大小上限
    code, data = call("/api/chat/stickers", ta)
    check(
        data.get("maxMb") == 2 and data.get("gifMaxMb") == 6,
        f"默认单张上限 2 MB / GIF 6 MB（{data.get('maxMb')}/{data.get('gifMaxMb')}）",
    )
    code, data = call("/api/admin/settings", tn)
    conf = data.get("settings") or {}
    check(
        conf.get("stickerMaxMb") == 2 and conf.get("stickerGifMaxMb") == 6,
        f"后台设置里能看到表情大小（{conf.get('stickerMaxMb')}/{conf.get('stickerGifMaxMb')}）",
    )
    code, data = call("/api/admin/settings", tn, "PATCH",
                      {"stickerMaxMb": 1, "stickerGifMaxMb": 10})
    check(code == 200 and (data.get("settings") or {}).get("stickerMaxMb") == 1, "后台能改表情大小上限")

    big_png = noise_png(800)
    check(len(big_png) > 1024 * 1024, f"造出来的大 PNG 超过 1 MB（{len(big_png)}）")
    code, data = call_raw("/api/chat/stickers", ta, big_png, method="POST")
    check(code == 413, f"静态表情超过后台设定被拦（{code}）")
    code, data = call_raw("/api/chat/stickers", ta, png_bytes(40, 40, (9, 9, 9)), method="POST")
    check(code == 201, f"上限内的小图仍然能加（{code}）")
    extra_png_id = (data.get("sticker") or {}).get("id")

    code, data = call("/api/admin/settings", tn, "PATCH", {"stickerGifMaxMb": 1})
    check(code == 200, "后台能单独收窄动图上限")
    big_gif = gif_bytes(40, 160, noise=True)
    check(len(big_gif) > 1024 * 1024, f"造出来的大 GIF 超过 1 MB（{len(big_gif)}）")
    code, data = call_raw("/api/chat/stickers", ta, big_gif, content_type="image/gif", method="POST")
    check(code == 413, f"动图表情超过后台设定被拦（{code}）")

    # 帧数护栏：240 帧以上直接拒，避免一张图吃掉内存。
    # 帧内容必须逐帧不同，否则 Pillow 存 GIF 时会把重复帧合并掉，帧数根本涨不上去。
    code, data = call_raw(
        "/api/chat/stickers", ta, gif_bytes(250, 24, noise=True), content_type="image/gif", method="POST"
    )
    check(code == 400, f"帧数过多的动图被拦（{code}）")

    # 清掉这两张测试用表情，后面的「只收了一张」才成立
    for extra_id in (gif_sticker.get("id"), extra_png_id):
        if extra_id:
            call(f"/api/chat/stickers/{extra_id}", ta, "DELETE")

    tdb = db_failover.current_sessionmaker()()
    try:
        set_value(tdb, "sticker_max_mb", "2")
        set_value(tdb, "sticker_gif_max_mb", "6")
        tdb.commit()
    finally:
        tdb.close()
    code, data = call("/api/chat/stickers", ta)
    check(data.get("maxMb") == 2 and data.get("gifMaxMb") == 6, "改回去后上限恢复")

    code, data = call(f"/api/chat/conversations/{conv_id}/messages", ta, "POST",
                      {"kind": "sticker", "payload": {"stickerId": sticker.get("id")}})
    check(code == 201, f"发表情（{code}）")
    sticker_name = (data.get("message") or {}).get("payload", {}).get("name", "")
    code, raw = fetch_bytes(f"/api/chat/media/{sticker_name}", tb)
    check(code == 200, "对方能取到表情图")

    # 把上限改成 1，再加一张应被拦
    tdb = db_failover.current_sessionmaker()()
    try:
        set_value(tdb, "sticker_max_per_user", "1")
        tdb.commit()
    finally:
        tdb.close()
    code, data = call_raw("/api/chat/stickers", ta, png_bytes(50, 50, (10, 20, 30)), method="POST")
    check(code == 400, f"超过上限被拦（{code}）")
    tdb = db_failover.current_sessionmaker()()
    try:
        set_value(tdb, "sticker_max_per_user", "100")
        tdb.commit()
    finally:
        tdb.close()

    code, data = call(f"/api/chat/stickers/{sticker.get('id')}", ta, "DELETE")
    check(code == 200, f"能删表情（{code}）")
    code, raw = fetch_bytes(f"/api/chat/media/{sticker_file}", ta)
    check(code == 404, "删掉的表情文件也没了（用旧名取不到）")

    # ================= 7) 举报 =================
    code, data = call(f"/api/chat/messages/{file_msg_id}/report", ta, "POST", {"reason": "举报自己"})
    check(code == 400, f"不能举报自己（{code}）")
    code, data = call(f"/api/chat/messages/{file_msg_id}/report", tb, "POST", {"reason": "广告"})
    check(code == 201, f"举报别人的消息（{code}）")
    report_id = data.get("id")
    code, data = call(f"/api/chat/messages/{file_msg_id}/report", tb, "POST", {"reason": "再来一次"})
    check(code == 409, f"重复举报被拦（{code}）")

    code, data = call("/api/admin/chat-reports?status=pending", tn)
    rows = data.get("items") or []
    check(any(r["id"] == report_id for r in rows), "后台能看到这条聊天举报")
    check(any(r["id"] == report_id and "广告" == r["reason"] for r in rows), "举报理由正确")
    check(any(r["id"] == report_id and "chat-doc.txt" in (r["snapshot"] or "") for r in rows),
          "举报快照保留了文件名")

    code, data = call("/api/admin/chat-reports?status=pending", tb)
    check(code == 404, f"普通用户进不了后台审核接口（{code}）")

    code, data = call(f"/api/admin/chat-reports/{report_id}/approve", tn, "POST", {"reason": "确认违规"})
    check(code == 200 and data.get("status") == "approved", f"管理员下架消息（{data}）")
    code, data = call(f"/api/chat/conversations/{conv_id}/messages", tb)
    hit = next((m for m in (data.get("items") or []) if m["id"] == file_msg_id), None)
    check((hit or {}).get("recalled") is True, "被举报的消息已下架（显示为撤回）")
    code, raw = fetch_bytes(f"/api/chat/messages/{file_msg_id}/files/0", tb)
    check(code == 404, "下架后文件也取不到")

    code, data = call(f"/api/admin/chat-reports/{report_id}/approve", tn, "POST", {})
    check(code == 409, f"重复处理被拦（{code}）")

    code, data = call("/api/notices?unreadOnly=true", ta)
    body = " ".join((n.get("title") or "") + (n.get("body") or "") for n in (data.get("items") or []))
    check("下架" in body or "举报" in body, "发送者/举报人都收到了处理通知")

    # ================= 8) 选项接口 =================
    code, data = call("/api/chat/friends", ta)
    names = {f.get("username") for f in (data.get("friends") or [])}
    check({"chat_bob", "chat_carol"} <= names, f"选人面板返回好友（{sorted(names)}）")
    check(data.get("stickerLimit") == 100 and data.get("groupLimit") == 200, "带回上限配置")

    # ================= 9) 把对方的表情收进我的表情（哈希去重）=================
    code, data = call_raw(
        "/api/chat/stickers?name=bob", tb, png_bytes(64, 64, (30, 180, 90)), method="POST"
    )
    bob_sticker = data.get("sticker") or {}
    code, data = call(f"/api/chat/conversations/{conv_id}/messages", tb, "POST",
                      {"kind": "sticker", "payload": {"stickerId": bob_sticker.get("id")}})
    check(code == 201, f"Bob 发表情给 Alice（{code}）")
    sticker_msg_id = (data.get("message") or {}).get("id")

    code, data = call(f"/api/chat/messages/{sticker_msg_id}/sticker", ta, "POST", {})
    check(code == 201 and data.get("created") is True, f"把对方的表情收进我的（{code}）")
    first_id = (data.get("sticker") or {}).get("id")
    code, data = call(f"/api/chat/messages/{sticker_msg_id}/sticker", ta, "POST", {})
    check(data.get("created") is False, "同一张表情再收一次不重复存（哈希相同）")
    check((data.get("sticker") or {}).get("id") == first_id, "重复收的仍是同一条记录")
    code, data = call("/api/chat/stickers", ta)
    check(len(data.get("items") or []) == 1, f"我的表情只有一张（{len(data.get('items') or [])}）")

    code, data = call(f"/api/chat/messages/{sticker_msg_id}/sticker", tc, "POST", {})
    check(code == 403, f"不在会话里的人收不了这张表情（{code}）")

    # ================= 10) 清空 / 删除 / 拉黑 =================
    code, data = call(f"/api/chat/conversations/{conv_id}/clear", tb, "POST", {})
    check(code == 200, f"清空聊天记录（{code}）")
    code, data = call(f"/api/chat/conversations/{conv_id}/messages", tb)
    check(not (data.get("items") or []), "清空后自己这边看不到历史")
    code, data = call(f"/api/chat/conversations/{conv_id}/messages", ta)
    check(bool(data.get("items")), "对方的历史消息一条没少")
    code, data = call("/api/chat/conversations", tb)
    check(next((x for x in (data.get("items") or []) if x["id"] == conv_id), {}).get("unread") == 0,
          "清空后未读也归零")

    code, data = call(f"/api/chat/conversations/{conv_id}", tb, "DELETE")
    check(code == 200, f"删除聊天（{code}）")
    code, data = call("/api/chat/conversations", tb)
    check(not any(x["id"] == conv_id for x in (data.get("items") or [])), "删除后会话从列表里消失")
    code, data = call(f"/api/chat/conversations/{conv_id}/messages", tb)
    check(code == 200 and not (data.get("items") or []), "删除等于同时清空")

    code, data = call(f"/api/chat/conversations/{conv_id}/messages", ta, "POST",
                      {"kind": "text", "body": "还在吗"})
    check(code == 201, f"对方仍能发消息（{code}）")
    code, data = call("/api/chat/conversations", tb)
    hit = next((x for x in (data.get("items") or []) if x["id"] == conv_id), None)
    check(hit is not None, "对方再发消息，删掉的会话重新出现")
    check((hit or {}).get("unread") == 1, "重新出现的会话带 1 条未读")

    code, data = call("/api/chat/blocks", ta, "POST", {"userId": bob_id})
    check(code == 201, f"拉黑（{code}）")
    code, data = call("/api/chat/conversations", ta)
    check(not any(x["id"] == conv_id for x in (data.get("items") or [])), "拉黑同时把聊天删掉")
    code, data = call(f"/api/chat/conversations/{conv_id}/messages", ta, "POST",
                      {"kind": "text", "body": "发不出去"})
    check(code == 403, f"拉黑后自己发不出（{code}）")
    code, data = call(f"/api/chat/conversations/{conv_id}/messages", tb, "POST",
                      {"kind": "text", "body": "？"})
    check(code == 403, f"被拉黑的人发不进来（{code}）")
    code, data = call("/api/chat/conversations/direct", ta, "POST", {"userId": bob_id})
    check(code == 403, f"拉黑后不能开新会话（{code}）")

    code, data = call("/api/friends", ta)
    check(not any(f["id"] == bob_id for f in (data.get("friends") or [])), "拉黑同时断了好友关系")
    code, data = call("/api/friends/requests", tb, "POST", {"userId": alice_id})
    check(code == 409, f"被拉黑的人发不了好友申请（{code}）")

    code, data = call("/api/chat/blocks", ta)
    check(any(x["id"] == bob_id for x in (data.get("items") or [])), "拉黑名单里有他")
    code, data = call("/api/friends/search?q=chat_bob", ta)
    hit = next((x for x in (data.get("items") or []) if x["id"] == bob_id), None)
    check((hit or {}).get("relation") == "blocked",
          f"搜到已拉黑的人显示「已拉黑」而不是「添加」（{data}）")
    code, data = call(f"/api/chat/blocks/{bob_id}", ta, "DELETE")
    check(code == 200 and data.get("blocked") is False, f"能解除拉黑（{code}）")
    code, data = call("/api/chat/conversations/direct", ta, "POST", {"userId": bob_id})
    check(code == 403, f"解除拉黑不等于恢复好友，仍要先加好友（{code}）")

    passed = sum(1 for ok, _ in RESULTS if ok)
    for ok, msg in RESULTS:
        print(("  OK  " if ok else "  NG  ") + msg)
    print(f"\n聊天接口：{passed}/{len(RESULTS)}")
    if passed != len(RESULTS):
        sys.exit(1)


if __name__ == "__main__":
    main()
