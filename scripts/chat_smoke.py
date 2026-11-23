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
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from PIL import Image  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

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
        raw = err.read()
        try:
            return err.code, json.loads(raw or b"{}")
        except Exception:
            return err.code, {"raw": raw.decode("utf-8", "replace")}


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

    passed = sum(1 for ok, _ in RESULTS if ok)
    for ok, msg in RESULTS:
        print(("  OK  " if ok else "  NG  ") + msg)
    print(f"\n聊天接口：{passed}/{len(RESULTS)}")
    if passed != len(RESULTS):
        sys.exit(1)


if __name__ == "__main__":
    main()
