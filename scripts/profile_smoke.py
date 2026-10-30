"""个人资料 / 头像 / 分享者展示 / 移动 的端到端校验。

用法：python -m scripts.profile_smoke
会注册一个临时账号 profprobe，跑完全部断言后把它连同数据一起清掉。
"""

from __future__ import annotations

import http.cookiejar
import io
import json
import sys
import urllib.error
import urllib.request

from PIL import Image

from app import profile_service as ps
from app.admin_service import purge_user
from app.db import SessionLocal
from app.models import AuditLog, Node, User

BASE = "http://127.0.0.1:8123"
USER = "profprobe"
PASSWORD = "Probe#2026x"

passed = 0
failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [ok]   {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label} {detail}")


class Client:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, path: str, method: str = "GET", payload=None, raw: bytes | None = None,
             headers: dict | None = None):
        data = None
        hdrs = {"Accept": "application/json"}
        if raw is not None:
            data = raw
            hdrs["Content-Type"] = "application/octet-stream"
        elif payload is not None:
            data = json.dumps(payload).encode()
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(BASE + path, data=data, headers=hdrs, method=method)
        try:
            with self.opener.open(req) as res:
                body = res.read()
                ctype = res.headers.get("content-type", "")
                return res.status, (json.loads(body) if "json" in ctype else body), ctype
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = body
            return exc.code, parsed, exc.headers.get("content-type", "")

    def get(self, path: str):
        return self.call(path)

    def post(self, path: str, payload=None):
        return self.call(path, "POST", payload=payload)

    def patch(self, path: str, payload=None):
        return self.call(path, "PATCH", payload=payload)

    def put_raw(self, path: str, blob: bytes):
        return self.call(path, "PUT", raw=blob)

    def delete(self, path: str):
        return self.call(path, "DELETE")


def png_bytes(width: int = 300, height: int = 200) -> bytes:
    img = Image.new("RGB", (width, height), (255, 140, 190))
    for x in range(0, width, 10):
        for y in range(0, height, 10):
            img.putpixel((x, y), (120, 80, 200))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def main() -> int:
    api = Client()

    # ---------- 准备账号 ----------
    status, body, _ = api.post("/api/auth/register", {"username": USER, "password": PASSWORD})
    if status == 409:
        with SessionLocal() as db:
            old = db.query(User).filter(User.username == USER).one()
            purge_user(db, old)
            db.commit()
        status, body, _ = api.post("/api/auth/register", {"username": USER, "password": PASSWORD})
    if status != 201:
        print(f"注册失败：{status} {body}")
        return 1
    user = body["user"]
    check("注册测试账号", status == 201)
    check("默认昵称回落为用户名", user["displayName"] == USER, user.get("displayName"))
    check("默认简介为空", user["bio"] == "")
    check("默认没有头像", user["avatarUrl"] is None)
    check("默认不在分享页公开完整昵称", user["settings"]["shareShowName"] is False)

    # ---------- 昵称 / 简介 ----------
    status, body, _ = api.patch("/api/me/profile", {"displayName": "  樱花   云管家 ", "bio": "  测试简介，会显示在分享页  "})
    check("修改昵称与简介", status == 200 and body["user"]["displayName"] == "樱花 云管家", body)
    check("简介首尾空白被裁掉", body["user"]["bio"] == "测试简介，会显示在分享页", body["user"].get("bio"))

    status, _, _ = api.patch("/api/me/profile", {"displayName": "樱" * 33})
    check("超长昵称被拒绝（400）", status == 400, status)

    status, body, _ = api.patch("/api/me/profile", {"displayName": ""})
    check("清空昵称回落到用户名", status == 200 and body["user"]["displayName"] == USER, body)
    api.patch("/api/me/profile", {"displayName": "樱花 云管家"})

    # ---------- 头像 ----------
    status, body, _ = api.put_raw("/api/me/avatar", png_bytes())
    avatar_url = body["user"]["avatarUrl"] if status == 200 else None
    check("上传头像", status == 200 and bool(avatar_url), body)
    check("头像 URL 形态正确", bool(avatar_url and avatar_url.startswith("/api/avatar/")), avatar_url)

    status, blob, ctype = api.get(avatar_url)
    ok_shape = False
    if status == 200 and isinstance(blob, bytes):
        with Image.open(io.BytesIO(blob)) as img:
            ok_shape = img.size == (256, 256)
    check("取回头像", status == 200, status)
    check("服务端裁成 256×256 方图", ok_shape)
    check("头像返回 webp", "webp" in (ctype or ""), ctype)

    other = avatar_url.replace(str(user["id"]) + "-", "999999-")
    status, _, _ = api.get(other)
    check("别人的头像 URL 取不到（404）", status == 404, status)

    status, _, _ = api.put_raw("/api/me/avatar", b"not-an-image-at-all")
    check("非图片数据被拒绝（400）", status == 400, status)

    status, _, _ = api.put_raw("/api/me/avatar", b"\x00" * (5 * 1024 * 1024))
    check("超大文件被拒绝（413）", status == 413, status)

    old_url = avatar_url
    status, body, _ = api.delete("/api/me/avatar")
    check("移除头像", status == 200 and body["user"]["avatarUrl"] is None, body)
    status, _, _ = api.get(old_url)
    check("旧头像文件已被删除（404）", status == 404, status)

    # ---------- 分享页分享者 ----------
    status, body, _ = api.put_raw("/api/me/avatar", png_bytes(120, 120))
    check("重新上传头像供分享页使用", status == 200 and body["user"]["avatarUrl"], body)

    with SessionLocal() as db:
        owner = db.query(User).filter(User.username == USER).one()
        node = Node(user_id=owner.id, parent_id=None, type="file", name="季度报表.pdf", size=1234, mime="application/pdf")
        db.add(node)
        db.commit()
        node_id = node.id

    status, body, _ = api.post("/api/shares", {"nodeId": node_id, "expiresDays": 7})
    token = body["share"]["token"] if status == 200 else ""
    check("创建分享", status == 200 and bool(token), body)

    anon = Client()
    status, body, _ = anon.get(f"/api/public/shares/{token}")
    author = (body.get("share") or {}).get("author") or {}
    check("分享页带出分享者信息", status == 200 and bool(author.get("name")), body)
    check("默认只露昵称前两位", author.get("name") == "樱花" and author.get("masked") is True, author)
    check("默认不下发头像", author.get("avatar") is None, author)

    status, _, _ = anon.get(f"/api/public/shares/{token}/avatar")
    check("未开启时头像接口 404", status == 404, status)

    status, body, _ = api.patch("/api/me/settings", {"shareShowName": True})
    check("开启「显示完整昵称」", status == 200 and body["settings"]["shareShowName"] is True, body)

    status, body, _ = anon.get(f"/api/public/shares/{token}")
    author = (body.get("share") or {}).get("author") or {}
    check("开启后显示完整昵称", author.get("name") == "樱花 云管家" and author.get("masked") is False, author)
    check("开启后下发头像地址", bool(author.get("avatar")), author)

    status, blob, ctype = anon.get(f"/api/public/shares/{token}/avatar")
    check("公开头像可访问", status == 200 and "image" in (ctype or ""), (status, ctype))

    api.patch("/api/me/settings", {"shareShowName": False})
    status, body, _ = anon.get(f"/api/public/shares/{token}")
    author = (body.get("share") or {}).get("author") or {}
    check("关闭后立刻回到脱敏显示", author.get("name") == "樱花" and author.get("masked") is True, author)

    # ---------- 移动 ----------
    with SessionLocal() as db:
        owner = db.query(User).filter(User.username == USER).one()
        src = Node(user_id=owner.id, parent_id=None, type="folder", name="待移动")
        dst = Node(user_id=owner.id, parent_id=None, type="folder", name="目标目录")
        db.add_all([src, dst])
        db.commit()
        src_id, dst_id = src.id, dst.id

    status, body, _ = api.post(f"/api/nodes/{src_id}/move", {"parentId": dst_id})
    check("移动到子目录", status == 200 and body["node"]["parentId"] == dst_id, body)

    status, body, _ = api.post(f"/api/nodes/{src_id}/move", {"parentId": None})
    check("移动回根目录（parentId=null）", status == 200 and body["node"]["parentId"] is None, body)

    # ---------- 审计 ----------
    with SessionLocal() as db:
        owner = db.query(User).filter(User.username == USER).one()
        actions = {
            row.action
            for row in db.query(AuditLog).filter(AuditLog.user_id == owner.id).all()
        }
    check("资料变更写入审计", "profile_update" in actions, sorted(actions))
    check("头像变更写入审计", {"avatar_update", "avatar_remove"} <= actions, sorted(actions))

    # ---------- 清理 ----------
    with SessionLocal() as db:
        owner = db.query(User).filter(User.username == USER).one()
        avatar = owner.avatar
        purge_user(db, owner)
        db.commit()
    ps.drop_avatar(avatar)
    check("测试账号已清理", True)

    print(f"\n通过 {passed} 项，失败 {failed} 项")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
