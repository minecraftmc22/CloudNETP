# -*- coding: utf-8 -*-
"""好友 + 消息中心的接口冒烟（跑在隔离库上）。

覆盖：
- 管理员默认能看到三条待审核计数
- 用户提交反馈 → 管理员收到 feedback 通知、待处理 +1
- 好友申请 → 收件人收到 friend_request；同意 → 发起人收到 friend_accept
- 通知标记已读 / 全部已读
- 删除好友
- 公开举报 → 管理员收到 report 通知（带 #/admin/reports 深链）

用法：
    DATABASE_URL=... STORAGE_DIR=... python scripts/social_smoke.py [port]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from sqlalchemy import delete  # noqa: E402

from app import db_failover  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import Feedback, Friendship, Node, Notice, Share, ShareReport, User, utcnow  # noqa: E402
from app.security import create_token, hash_password  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
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
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw or b"{}")
        except Exception:
            return err.code, {"raw": raw.decode("utf-8", "replace")}


def main() -> None:
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    try:
        admin = db.query(User).filter(User.role == 1).order_by(User.id.asc()).first()
        assert admin, "隔离库里没有管理员账号"

        def ensure_user(name: str) -> User:
            row = db.query(User).filter(User.username == name).first()
            if row is None:
                row = User(
                    username=name,
                    password_hash=hash_password("SmokeTest123"),
                    role=0,
                    quota_bytes=1024**3,
                    used_bytes=0,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
            return row

        alice = ensure_user("smoke_alice")
        bob = ensure_user("smoke_bob")
        # 每次跑之前清掉这两个账号的历史关系 / 通知，结果才可重复
        db.execute(
            delete(Friendship).where(
                Friendship.requester_id.in_([alice.id, bob.id, admin.id])
                | Friendship.addressee_id.in_([alice.id, bob.id, admin.id])
            )
        )
        db.execute(delete(Notice).where(Notice.user_id.in_([alice.id, bob.id, admin.id])))
        db.commit()

        t_admin = create_token(admin.id, admin.token_version)
        t_alice = create_token(alice.id, alice.token_version)
        t_bob = create_token(bob.id, bob.token_version)

        # 1) 管理员基线：三个审核计数都在
        st, data = call("/api/notices/unread", t_admin)
        check(st == 200, "GET /api/notices/unread 200")
        pending = data.get("pending") or {}
        check(
            all(k in pending for k in ("reports", "appeals", "feedback", "friendRequests")),
            "管理员 pending 含 reports/appeals/feedback/friendRequests",
        )
        base_feedback = int(pending.get("feedback") or 0)
        base_unread = int(data.get("unread") or 0)

        # 2) 普通用户提交反馈 → 管理员收到通知
        st, data = call(
            "/api/feedback",
            t_alice,
            "POST",
            {"kind": "suggestion", "title": "冒烟反馈", "content": "这是一条冒烟测试反馈", "contact": ""},
        )
        check(st == 201, f"提交反馈 201（实际 {st}）")
        feedback_id = (data.get("item") or {}).get("id")

        st, data = call("/api/notices/unread", t_admin)
        check(int(data.get("unread") or 0) == base_unread + 1, "管理员未读数 +1")
        check(
            int((data.get("pending") or {}).get("feedback") or 0) == base_feedback + 1,
            "待处理反馈 +1",
        )
        st, data = call("/api/notices?limit=5", t_admin)
        latest = (data.get("items") or [{}])[0]
        check(latest.get("kind") == "feedback", "最新通知 kind=feedback")
        check(latest.get("link") == "#/admin/feedback", "反馈通知深链到后台反馈页")
        check(latest.get("read") is False, "新通知默认未读")

        # 3) 好友：搜索 → 申请 → 收件人看到 → 同意
        st, data = call("/api/friends/search?q=smoke_alice", t_bob)
        items = data.get("items") or []
        check(st == 200 and any(i.get("username") == "smoke_alice" for i in items), "搜索能命中 alice")
        hit = next((i for i in items if i["username"] == "smoke_alice"), {})
        check(hit.get("relation") == "none", "搜索结果显示 relation=none")

        st, data = call(
            "/api/friends/requests", t_bob, "POST", {"userId": alice.id, "message": "我是 bob"}
        )
        check(st == 201 and data.get("relation") == "pending_out", "bob 发出好友申请")

        st, data = call("/api/friends/requests", t_bob, "POST", {"userId": alice.id, "message": ""})
        check(st == 409, "重复申请返回 409")

        st, data = call("/api/friends/requests", t_bob, "POST", {"userId": bob.id, "message": ""})
        check(st == 400, "加自己为好友返回 400")

        st, data = call("/api/notices/unread", t_alice)
        check(
            int((data.get("pending") or {}).get("friendRequests") or 0) == 1,
            "alice 看到 1 条好友申请",
        )
        st, data = call("/api/notices?limit=5", t_alice)
        check(
            any(i.get("kind") == "friend_request" for i in (data.get("items") or [])),
            "alice 收到 friend_request 通知",
        )

        st, data = call("/api/friends", t_alice)
        incoming = data.get("incoming") or []
        check(len(incoming) == 1 and incoming[0].get("username") == "smoke_bob", "alice 的申请列表有 bob")
        req_id = incoming[0].get("requestId") if incoming else None

        st, data = call(f"/api/friends/requests/{req_id}/accept", t_alice, "POST")
        check(st == 200 and data.get("ok"), "alice 同意申请")

        st, data = call("/api/friends", t_alice)
        check(len(data.get("friends") or []) == 1, "alice 好友列表 1 人")
        st, data = call("/api/friends", t_bob)
        check(len(data.get("friends") or []) == 1, "bob 好友列表 1 人")
        st, data = call("/api/notices?limit=5", t_bob)
        check(
            any(i.get("kind") == "friend_accept" for i in (data.get("items") or [])),
            "bob 收到 friend_accept 通知",
        )

        # 4) 已读
        st, data = call("/api/notices/read", t_alice, "POST", {})
        check(st == 200 and int(data.get("unread") or 0) == 0, "全部已读后 alice 未读为 0")
        st, data = call("/api/notices?unreadOnly=true", t_alice)
        check(len(data.get("items") or []) == 0, "未读筛选为空")

        # 5) 删除好友
        st, data = call(f"/api/friends/{bob.id}", t_alice, "DELETE")
        check(st == 200, "删除好友 200")
        st, data = call("/api/friends", t_alice)
        check(len(data.get("friends") or []) == 0, "删除后好友列表为空")

        # 6) 举报 → 管理员通知（真的走一遍公开举报接口）
        node = Node(
            user_id=admin.id,
            parent_id=None,
            type="file",
            name="smoke-report.txt",
            size=0,
            blob_sha256=None,
            created_at=utcnow(),
        )
        db.add(node)
        db.commit()
        db.refresh(node)
        share = Share(
            user_id=admin.id,
            node_id=node.id,
            token=f"smoke{node.id}{int(utcnow().timestamp())}",
            created_at=utcnow(),
        )
        db.add(share)
        db.commit()
        st, data = call(
            "/api/public/shares/%s/report" % share.token,
            None,
            "POST",
            {"reason": "冒烟测试举报"},
        )
        check(st == 200, f"公开举报 200（实际 {st}）")
        st, data = call("/api/notices?limit=8", t_admin)
        report_notice = next(
            (i for i in (data.get("items") or []) if i.get("kind") == "report"), None
        )
        check(report_notice is not None, "管理员收到 report 通知")
        if report_notice:
            check(report_notice.get("link") == "#/admin/reports", "举报通知深链到审核页")

        # 清理本轮造的数据（不留脏数据给后面的截图脚本）
        if feedback_id:
            db.execute(delete(Feedback).where(Feedback.id == feedback_id))
        db.execute(delete(ShareReport).where(ShareReport.token == share.token))
        db.execute(delete(Share).where(Share.id == share.id))
        db.execute(delete(Node).where(Node.id == node.id))
        db.execute(
            delete(Friendship).where(
                Friendship.requester_id.in_([alice.id, bob.id])
                | Friendship.addressee_id.in_([alice.id, bob.id])
            )
        )
        db.execute(delete(Notice).where(Notice.user_id.in_([alice.id, bob.id, admin.id])))
        db.commit()
    finally:
        db.close()

    failed = [msg for ok, msg in RESULTS if not ok]
    for ok, msg in RESULTS:
        print(("  OK  " if ok else "  !!  ") + msg)
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
