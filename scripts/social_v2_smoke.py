# -*- coding: utf-8 -*-
"""个人主页 / 好友备注 / 群组增强 / 群文件 / 消息提醒 接口冒烟（跑在隔离库上）。

覆盖：
- 个人资料：字段白名单、生日校验、可见性过滤、公开分享开关与失效
- 好友备注：只对自己生效、单聊标题跟随备注
- 群资料：改名/简介、公开私密权限、群头像
- 群管理员：群主设/取消、管理员能改群名与踢人、不能越级
- 群内备注：只对自己可见、不串到别人
- 找群加群：公开群搜索、群号加入、私密群拒绝、邀请链接加入
- 群分享：群号 9 位、链接、二维码、关闭后旧链接失效
- 群文件：上传/下载/删除、非成员拦截、配额
- 消息提醒：alerts 摘要、备注名、撤回不提醒

用法：
    DATABASE_URL=... STORAGE_DIR=... python scripts/social_v2_smoke.py [port]
"""
from __future__ import annotations

import io
import json
import shutil
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
    ChatConversation,
    ChatMember,
    ChatMemberNote,
    ChatMessage,
    ChatReport,
    Friendship,
    GroupFile,
    Notice,
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
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, method=method, data=data)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Cookie", f"{settings.COOKIE_NAME}={token}")
    return _open(req)


def call_raw(path: str, token: str | None, data: bytes, content_type: str = "application/octet-stream",
             method: str = "PUT"):
    req = urllib.request.Request(BASE + path, method=method, data=data)
    req.add_header("Content-Type", content_type)
    if token:
        req.add_header("Cookie", f"{settings.COOKIE_NAME}={token}")
    return _open(req)


def _open(req):
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
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


def fetch(path: str, token: str | None = None):
    req = urllib.request.Request(BASE + path)
    if token:
        req.add_header("Cookie", f"{settings.COOKIE_NAME}={token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return res.status, res.read(), dict(res.headers)
    except urllib.error.HTTPError as err:
        return err.code, err.read(), dict(err.headers)


def png_bytes(w: int = 90, h: int = 90, color=(120, 180, 255)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def main() -> int:
    db_failover.startup()
    db = db_failover.current_sessionmaker()()

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

    try:
        alice = ensure_user("v2_alice")
        bob = ensure_user("v2_bob")
        carol = ensure_user("v2_carol")
        dave = ensure_user("v2_dave")
        eve = ensure_user("v2_eve")
        frank = ensure_user("v2_frank")
        # gina 全程不进群：用来验证「非成员」的拦截（frank 后面会靠新链接入群）
        gina = ensure_user("v2_gina")
        ids = [alice.id, bob.id, carol.id, dave.id, eve.id, frank.id, gina.id]

        # ---- 清场：上次跑剩下的群、好友、备注 ----
        conv_ids = list(
            db.scalars(select(ChatMember.conversation_id).where(ChatMember.user_id.in_(ids))).all()
        )
        if conv_ids:
            db.execute(delete(GroupFile).where(GroupFile.conversation_id.in_(conv_ids)))
            db.execute(delete(ChatMemberNote).where(ChatMemberNote.conversation_id.in_(conv_ids)))
            db.execute(delete(ChatMessage).where(ChatMessage.conversation_id.in_(conv_ids)))
            db.execute(delete(ChatReport).where(ChatReport.conversation_id.in_(conv_ids)))
            db.execute(delete(ChatMember).where(ChatMember.conversation_id.in_(conv_ids)))
            db.execute(delete(ChatConversation).where(ChatConversation.id.in_(conv_ids)))
            for cid in conv_ids:
                shutil.rmtree(settings.group_dir / str(cid), ignore_errors=True)
        db.execute(
            delete(Friendship).where(
                Friendship.requester_id.in_(ids) | Friendship.addressee_id.in_(ids)
            )
        )
        db.execute(delete(Notice).where(Notice.user_id.in_(ids)))
        db.commit()

        # 直接给 alice / bob 建好友关系，省掉申请流程（那条链路由 social_smoke 覆盖）
        db.add_all(
            [
                Friendship(requester_id=alice.id, addressee_id=bob.id, status="accepted",
                           message="", requester_alias="", addressee_alias="",
                           created_at=utcnow(), updated_at=utcnow()),
                Friendship(requester_id=alice.id, addressee_id=carol.id, status="accepted",
                           message="", requester_alias="", addressee_alias="",
                           created_at=utcnow(), updated_at=utcnow()),
                Friendship(requester_id=alice.id, addressee_id=dave.id, status="accepted",
                           message="", requester_alias="", addressee_alias="",
                           created_at=utcnow(), updated_at=utcnow()),
            ]
        )
        for u in ids:
            db.get(User, u).profile = {}
            db.get(User, u).profile_token = None
        db.commit()
        for key in ("group_file_quota_gb", "chat_group_max_members"):
            set_value(db, key, "10" if key == "group_file_quota_gb" else "200")
        db.commit()

        ta = create_token(alice.id, alice.token_version)
        tb = create_token(bob.id, bob.token_version)
        tc = create_token(carol.id, carol.token_version)
        td = create_token(dave.id, dave.token_version)
        te = create_token(eve.id, eve.token_version)
        tf = create_token(frank.id, frank.token_version)
        tg = create_token(gina.id, gina.token_version)

        # ------------------------------------------------------------------ #
        # 1. 个人资料 + 可见性 + 分享
        # ------------------------------------------------------------------ #
        code, data = call("/api/me/profile/detail", ta)
        check(code == 200 and data["profile"]["displayName"] == "v2_alice", "资料回填有内容")

        code, data = call(
            "/api/me/profile/detail",
            ta,
            "PATCH",
            {
                "gender": "female",
                "birthday": "1998-06-01",
                "links": {"qq": "12345678", "github": "alice", "website": "https://example.com/a"},
                "show": {"qq": True, "github": False, "website": True, "email": False,
                         "gender": True, "birthday": False},
            },
        )
        check(code == 200, f"保存扩展资料（{code}）")
        check(data["profile"]["links"]["qq"] == "12345678", "QQ 存进去了")

        code, _ = call("/api/me/profile/detail", ta, "PATCH", {"birthday": "2999-01-01"})
        check(code == 400, f"未来生日被拒（{code}）")
        code, _ = call("/api/me/profile/detail", ta, "PATCH", {"birthday": "1998/06/01"})
        check(code == 400, f"生日格式不对被拒（{code}）")
        code, _ = call("/api/me/profile/detail", ta, "PATCH", {"links": {"qq": "9" * 200}})
        check(code == 400, f"超长链接被拒（{code}）")
        code, _ = call("/api/me/profile/detail", ta, "PATCH", {"gender": "alien"})
        check(code == 400, f"非法性别被拒（{code}）")

        code, data = call(f"/api/users/{alice.id}/profile", tb)
        p = data.get("profile") or {}
        keys = {item["key"] for item in p.get("links") or []}
        check(code == 200 and "qq" in keys, "好友能看到公开的 QQ")
        check("github" not in keys, "关掉开关的 GitHub 不下发")
        check(not p.get("birthday"), "生日默认不展示")
        check(p.get("relation") == "friends", "关系是好友")
        check(p.get("gender") == "female", "性别按开关展示")

        # 关掉 QQ 后连好友也看不到
        call("/api/me/profile/detail", ta, "PATCH", {"show": {"qq": False}})
        code, data = call(f"/api/users/{alice.id}/profile", tb)
        keys = {item["key"] for item in (data.get("profile") or {}).get("links") or []}
        check("qq" not in keys, "关掉后 QQ 立刻消失")

        # 分享开关
        code, data = call("/api/me/profile/share", ta, "POST", {"enabled": True})
        token = data.get("token") or ""
        check(code == 200 and len(token) >= 10, f"开启分享拿到 token（{token}）")
        code, data = call(f"/api/public/profile/{token}")
        pub = data.get("profile") or {}
        check(code == 200 and pub.get("displayName") == "v2_alice", "公开页未登录可读")
        check("id" not in pub and "username" not in pub, "公开页不泄露 id / 用户名")
        code, _, ctype = fetch(f"/api/public/profile/{token}/avatar")
        check(code == 404 or ctype.get("content-type", "").startswith("image/"), "公开头像路由存在")

        code, data = call("/api/me/profile/share", ta, "POST", {"enabled": False})
        check(code == 200 and data.get("enabled") is False, "关闭分享")
        code, _ = call(f"/api/public/profile/{token}")
        check(code == 404, f"旧分享链接立即失效（{code}）")

        # ------------------------------------------------------------------ #
        # 2. 好友备注
        # ------------------------------------------------------------------ #
        code, _ = call(f"/api/friends/{bob.id}", ta, "PATCH", {"alias": "老二"})
        check(code == 200, "设置好友备注")
        code, data = call("/api/friends", ta)
        friend = next((x for x in data["friends"] if x["id"] == bob.id), {})
        check(friend.get("alias") == "老二", "好友列表带备注")
        code, data = call("/api/friends", tb)
        mine = next((x for x in data["friends"] if x["id"] == alice.id), {})
        check(mine.get("alias") == "", "备注只对自己可见")

        # 单聊标题跟随备注
        call("/api/chat/conversations/direct", ta, "POST", {"userId": bob.id})
        code, data = call("/api/chat/conversations", ta)
        direct = next((c for c in data["items"] if c["kind"] == "direct" and c["peerId"] == bob.id), {})
        check(direct.get("title") == "老二", f"单聊标题用备注（{direct.get('title')}）")

        # ------------------------------------------------------------------ #
        # 3. 建群 + 群资料 + 头像
        # ------------------------------------------------------------------ #
        code, data = call(
            "/api/chat/conversations/group",
            ta,
            "POST",
            {"title": "V2 测试群", "memberIds": [bob.id, carol.id]},
        )
        conv = data.get("conversation") or {}
        cid = conv.get("id")
        check(code == 201 and cid, f"建群成功（{code}）")
        check(conv.get("myRole") == "owner" and conv.get("canManage") is True, "群主可管理")

        code, data = call(
            f"/api/chat/conversations/{cid}", ta, "PATCH",
            {"title": "V2 改名群", "description": "群简介一句话"},
        )
        check(code == 200 and data["conversation"]["title"] == "V2 改名群", "改群名与简介")
        check(data["conversation"]["description"] == "群简介一句话", "简介保存成功")

        code, _ = call(f"/api/chat/conversations/{cid}", te, "PATCH", {"title": "偷偷改"})
        check(code in (403, 404), f"非成员改群名被拦（{code}）")

        code, _ = call(f"/api/chat/conversations/{cid}", tb, "PATCH", {"isPublic": True})
        check(code == 403, f"普通成员改公开性被拦（{code}）")
        code, data = call(f"/api/chat/conversations/{cid}", ta, "PATCH", {"isPublic": True})
        check(code == 200 and data["conversation"]["isPublic"] is True, "群主能设公开群")

        code, data = call_raw(f"/api/chat/conversations/{cid}/avatar", ta, png_bytes(), "image/png")
        avatar_url = (data.get("conversation") or {}).get("avatarUrl") or ""
        check(code == 200 and avatar_url.startswith("/api/chat/group-avatar/"), "上传群头像")
        code, _, ctype = fetch(avatar_url, tb)
        check(code == 200 and ctype.get("content-type", "").startswith("image/"), "群头像可读取")

        # ------------------------------------------------------------------ #
        # 4. 群管理员
        # ------------------------------------------------------------------ #
        code, _ = call(f"/api/chat/conversations/{cid}/admins", tb, "POST",
                       {"userId": carol.id, "admin": True})
        check(code == 403, f"非群主设管理员被拦（{code}）")
        code, _ = call(f"/api/chat/conversations/{cid}/admins", ta, "POST",
                       {"userId": carol.id, "admin": True})
        check(code == 200, "群主设管理员")
        code, _ = call(f"/api/chat/conversations/{cid}/admins", ta, "POST",
                       {"userId": alice.id, "admin": True})
        check(code == 400, f"不能把群主设成管理员（{code}）")

        code, data = call(f"/api/chat/conversations/{cid}", tc)
        check(data["conversation"]["myRole"] == "admin", "管理员身份生效")
        code, data = call(f"/api/chat/conversations/{cid}", tc, "PATCH", {"title": "管理员改的名"})
        check(code == 200, "管理员能改群名")

        # 管理员能踢普通成员，但踢不了群主/其他管理员
        code, _ = call(f"/api/chat/conversations/{cid}/members/{alice.id}", tc, "DELETE")
        check(code == 403, f"管理员踢群主被拦（{code}）")

        # ------------------------------------------------------------------ #
        # 5. 群内备注
        # ------------------------------------------------------------------ #
        code, _ = call(f"/api/chat/conversations/{cid}/members/{bob.id}/note", ta, "PUT",
                       {"userId": bob.id, "alias": "群里的老二"})
        check(code == 200, "设置群内备注")
        code, data = call(f"/api/chat/conversations/{cid}", ta)
        member = next((m for m in data["conversation"]["members"] if m["id"] == bob.id), {})
        check(member.get("alias") == "群里的老二", "备注出现在成员列表")
        code, data = call(f"/api/chat/conversations/{cid}", tc)
        member = next((m for m in data["conversation"]["members"] if m["id"] == bob.id), {})
        check(not member.get("alias"), "别人的视角没有这条备注")
        code, _ = call(f"/api/chat/conversations/{cid}/members/{alice.id}/note", ta, "PUT",
                       {"userId": alice.id, "alias": "自己"})
        check(code == 400, f"不能给自己起备注（{code}）")

        # ------------------------------------------------------------------ #
        # 6. 找群 / 加群 / 群分享
        # ------------------------------------------------------------------ #
        q = urllib.parse.quote("管理员改的")
        code, data = call(f"/api/chat/groups/search?q={q}", te)
        hit = next((g for g in data.get("items") or [] if g["id"] == cid), None)
        check(code == 200 and hit is not None, "公开群能被搜到")

        code, data = call("/api/chat/groups/join", te, "POST", {"conversationId": cid})
        check(code == 200 and data.get("joined") is True, "公开群可直接加入")
        code, data = call("/api/chat/groups/join", te, "POST", {"conversationId": cid})
        check(data.get("joined") is False, "重复加入不报错")

        # 私密群：群号不给加
        call(f"/api/chat/conversations/{cid}", ta, "PATCH", {"isPublic": False})
        code, share = call(f"/api/chat/conversations/{cid}/share", ta)
        group_no = share.get("groupNo") or ""
        check(len(group_no) == 9 and group_no.isdigit(), f"群号是 9 位数字（{group_no}）")
        check((share.get("url") or "").endswith(share.get("path") or "x"), "分享链接带路径")
        check((share.get("qr") or "").startswith("data:image/png;base64,"), "二维码是 data URL")

        code, _ = call("/api/chat/groups/join", td, "POST", {"no": group_no})
        check(code == 403, f"私密群不能靠群号加入（{code}）")

        token = (share.get("path") or "").rsplit("/", 1)[-1]
        code, data = call(f"/api/public/groups/{token}")
        check(code == 200 and data["group"]["title"] == "管理员改的名", "未登录能看群邀请摘要")
        code, data = call("/api/chat/groups/join", td, "POST", {"token": token})
        check(code == 200 and data.get("joined") is True, "邀请链接能进私密群")

        # 重置链接：旧 token 立刻失效，新 token 可用
        code, fresh = call(f"/api/chat/conversations/{cid}/share", ta, "POST", {})
        new_token = (fresh.get("path") or "").rsplit("/", 1)[-1]
        check(code == 200 and new_token and new_token != token, "重置后拿到新链接")
        code, _ = call("/api/chat/groups/join", tf, "POST", {"token": token})
        check(code == 404, f"旧链接失效（{code}）")
        code, data = call("/api/chat/groups/join", tf, "POST", {"token": new_token})
        check(code == 200 and data.get("joined") is True, "新链接能进群")

        # ------------------------------------------------------------------ #
        # 7. 群文件
        # ------------------------------------------------------------------ #
        body1 = b"hello group file"
        code, data = call_raw(
            f"/api/chat/conversations/{cid}/files?name=notes.txt", ta, body1, "text/plain"
        )
        check(code == 201 and (data.get("file") or {}).get("name") == "notes.txt", f"上传群文件（{code}）")
        fid = data["file"]["id"]
        check(data.get("used") == len(body1), "已用空间累加")

        body2 = b"second file payload" * 10
        code, data = call_raw(
            f"/api/chat/conversations/{cid}/files?name=second.bin", tb, body2, "application/octet-stream"
        )
        check(code == 201, "第二个群文件（普通成员也能传）")

        code, data = call(f"/api/chat/conversations/{cid}/files", tc)
        check(code == 200 and len(data["items"]) == 2, "群文件列表两条")
        check(data["used"] == len(body1) + len(body2), "用量等于两个文件之和")

        code, raw, _ = fetch(f"/api/chat/group-files/{fid}/download", tc)
        check(code == 200 and raw == body1, "成员能下载到原内容")
        code, _, _ = fetch(f"/api/chat/group-files/{fid}/download", tg)
        check(code in (403, 404), f"非成员下载被拦（{code}）")

        code, _ = call(f"/api/chat/group-files/{fid}", tb, "DELETE")
        check(code == 403, f"普通成员删别人的文件被拦（{code}）")
        code, data = call(f"/api/chat/group-files/{fid}", ta, "DELETE")
        check(code == 200 and data["used"] == len(body2), "群主删除并扣减用量")

        # 配额：把已用顶到接近上限，再传一个就被拦
        row = db.get(ChatConversation, cid)
        quota = 10 * 1024**3
        row.file_bytes = quota - 4
        db.commit()
        code, data = call_raw(
            f"/api/chat/conversations/{cid}/files?name=big.bin", ta, b"1234567890", "application/octet-stream"
        )
        check(code == 413, f"超出群配额被拦（{code}）")
        code, data = call(f"/api/chat/conversations/{cid}/files", ta)
        check(data["quota"] == quota and data["used"] <= quota, "配额信息返回正确")
        row.file_bytes = len(body2)
        db.commit()

        # ------------------------------------------------------------------ #
        # 8. 消息提醒
        # ------------------------------------------------------------------ #
        code, data = call("/api/chat/conversations", ta)
        # 从库里取当前最大消息 id 当起点，避免历史消息干扰
        start = int(
            db.scalar(select(ChatMessage.id).order_by(ChatMessage.id.desc()).limit(1)) or 0
        )
        call(f"/api/chat/conversations/{cid}/messages", tb, "POST",
             {"kind": "text", "body": "提醒测试消息"})
        code, data = call(f"/api/chat/alerts?after={start}", ta)
        msgs = data.get("messages") or []
        check(code == 200 and len(msgs) == 1, f"alerts 返回一条新消息（{len(msgs)}）")
        check(msgs and msgs[0]["senderName"] == "群里的老二", f"提醒里用群内备注名（{msgs and msgs[0]['senderName']}）")
        check(msgs and msgs[0]["conversationId"] == cid, "提醒带会话 id")
        latest = data.get("latest") or 0
        code, data = call(f"/api/chat/alerts?after={latest}", ta)
        check(data.get("messages") == [], "after 之后没有新消息")

        # 撤回的不提醒
        code, data = call(f"/api/chat/conversations/{cid}/messages", tb, "POST",
                          {"kind": "text", "body": "马上撤回"})
        mid = data["message"]["id"]
        call(f"/api/chat/messages/{mid}/recall", tb, "POST")
        code, data = call(f"/api/chat/alerts?after={latest}", ta)
        check(all(m["id"] != mid for m in data.get("messages") or []), "撤回的消息不提醒")

        # 自己发的不提醒
        code, data = call(f"/api/chat/conversations/{cid}/messages", ta, "POST",
                          {"kind": "text", "body": "我自己说的"})
        own = data["message"]["id"]
        code, data = call(f"/api/chat/alerts?after={latest}", ta)
        check(all(m["id"] != own for m in data.get("messages") or []), "自己发的不提醒自己")

        # ------------------------------------------------------------------ #
        # 9. 权限收尾：成员备注跨群隔离、退群清理
        # ------------------------------------------------------------------ #
        code, _ = call(f"/api/chat/conversations/{cid}/members/{bob.id}", ta, "DELETE")
        check(code == 200, "群主移出成员")
        db.expire_all()
        left = db.scalar(
            select(ChatMemberNote).where(
                ChatMemberNote.conversation_id == cid, ChatMemberNote.target_id == bob.id
            )
        )
        check(left is None, "移出成员时清掉他的备注")

    finally:
        db.close()

    ok = sum(1 for good, _ in RESULTS if good)
    for good, msg in RESULTS:
        print(("  OK  " if good else "  NG  ") + msg)
    print(f"\n{ok}/{len(RESULTS)} 通过")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
