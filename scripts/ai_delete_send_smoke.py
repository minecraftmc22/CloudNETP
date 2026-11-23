# -*- coding: utf-8 -*-
"""AI 删除 / 快传发送 两个工具离线联调（不经过模型，直接调 run_tool_safe）。

  A delete_item 没勾「删除」权限时拒绝，并提示去哪开
  B delete_item 默认只进回收站：节点还在，deleted_at 非空
  C delete_item 文件夹：整棵子树一起进回收站
  D delete_item permanent=true：节点行消失，blob 引用递减
  E empty_trash 不带 confirm 拒绝执行
  F empty_trash 指定名字：只删这一项，名字对不上时报错并列出可选项
  G empty_trash 清空全部：节点行全部消失，used_bytes 回落
  H send_to_user 没勾「快传」权限时拒绝
  I send_to_user 收件人不存在 / 发给自己：如实报错
  J send_to_user 成功：建 QuickTransfer、暂存副本带 pending_send_id、接收方收到通知
  K send_to_user 发文件夹：子文件一起进暂存区
  L 没开权限时 tool_specs 不下发新工具（三层权限里的第二层）

    ./.venv/Scripts/python.exe scripts/ai_delete_send_smoke.py
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-ai-del-")).resolve()
DB = TMP / "tool.db"


def snapshot_db(dst: Path) -> None:
    src_path = ROOT / "storage" / "db" / "fallback.db"
    if not src_path.is_file():
        src_path = ROOT / "dev.db"
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        src.close()
        out.close()


snapshot_db(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB.as_posix()}"
os.environ["STORAGE_DIR"] = str(TMP / "storage")
os.environ["DB_FALLBACK_ENABLED"] = "0"
os.environ["DB_PROBE_INTERVAL"] = "0"
os.environ["DB_MIRROR_INTERVAL"] = "0"
sys.path.insert(0, str(ROOT))

passed = 0
failed = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {label} {extra}")
    else:
        failed += 1
        print(f"  FAIL {label} {extra}")


def main() -> int:
    from app import ai_service, node_service as ns, quick_service as qs, storage
    from app.db import SessionLocal, init_db
    from app.models import Blob, Node, Notice, QuickTransfer, User

    init_db()
    db = SessionLocal()
    admin = db.query(User).filter(User.role == 1).order_by(User.id).first()
    if admin is None:
        raise SystemExit("库里没有管理员")

    stamp = str(int(time.time()))
    # 收件人：没有第二个用户就造一个（只在这个临时库里）
    receiver = db.query(User).filter(User.role != 1, User.id != admin.id).first()
    if receiver is None:
        from app.security import hash_password

        receiver = User(
            username=f"ai_peer_{stamp}",
            display_name="收件人",
            password_hash=hash_password("SmokeTest123"),
            role=0,
            quota_bytes=1024**3,
            used_bytes=0,
        )
        db.add(receiver)
        db.commit()
        db.refresh(receiver)
    admin_id, receiver_id = admin.id, receiver.id

    def make_file(name: str, parent_id: int | None = None) -> Node:
        content = f"{name}-{stamp}".encode() * 30
        sha = hashlib.sha256(content).hexdigest()
        if not storage.blob_exists(sha, len(content)):
            tmp = storage.new_tmp_path()
            tmp.write_bytes(content)
            storage.commit_tmp(tmp, sha)
        node = ns.create_file(db, admin, parent_id, name, sha, len(content), "text/plain")
        db.commit()
        return node

    f1 = make_file(f"AI删除样本-{stamp}.txt")
    folder = ns.create_folder(db, admin, None, f"AI删除目录-{stamp}")
    db.commit()
    f2 = make_file(f"目录里的文件-{stamp}.txt", folder.id)
    f3 = make_file(f"彻底删除样本-{stamp}.txt")
    f4 = make_file(f"清空样本-{stamp}.txt")
    f5 = make_file(f"快传样本-{stamp}.txt")
    ns.recalc_usage(db, admin)
    db.commit()
    db.refresh(admin)
    used_before = admin.used_bytes or 0
    # 记下 id：后面的工具调用会删行，过期对象再读属性会抛 ObjectDeletedError
    f1_id, folder_id, f3_id, f4_id = f1.id, folder.id, f3.id, f4.id
    f3_sha, f5_id = f3.blob_sha256, f5.id

    print("\n[A] 没勾删除权限")
    out = ai_service.run_tool_safe(
        admin.id, "delete_item", {"paths": [f1.name]}, {"delete": False}
    )
    check("拒绝执行", "失败" in out.text and "删除" in out.text, out.text[:70])
    db.expire_all()
    still = db.query(Node).filter(Node.id == f1_id).first()
    check("文件没被动", still is not None and still.deleted_at is None, "")

    print("\n[B] 默认进回收站")
    out = ai_service.run_tool_safe(
        admin.id, "delete_item", {"paths": [f1.name]}, {"delete": True}
    )
    check("回报已放进回收站", "回收站" in out.text and "还原" in out.text, out.text[:80])
    db.expire_all()
    row = db.query(Node).filter(Node.id == f1_id).first()
    check("节点还在、带删除标记", row is not None and row.deleted_at is not None, "")
    tops = [n.name for n in ai_service._trash_tops(db, admin.id)]
    check("出现在回收站顶层", f1.name in tops, str(tops[:4]))

    print("\n[C] 删除文件夹")
    out = ai_service.run_tool_safe(
        admin.id, "delete_item", {"paths": [folder.name]}, {"delete": True}
    )
    db.expire_all()
    kids = db.query(Node).filter(Node.parent_id == folder_id).all()
    check("父目录进回收站", db.get(Node, folder_id).deleted_at is not None, "")
    check("子文件一起进回收站", bool(kids) and all(k.deleted_at for k in kids), str(len(kids)))

    print("\n[D] 彻底删除")
    db.expire_all()
    blob_before = db.query(Blob).filter(Blob.sha256 == f3_sha).first()
    ref_before = blob_before.ref_count if blob_before else 0
    out = ai_service.run_tool_safe(
        admin.id, "delete_item", {"paths": [f3.name], "permanent": True}, {"delete": True}
    )
    check("回报无法恢复", "无法恢复" in out.text, out.text[:70])
    db.expire_all()
    check("节点行被删掉", db.query(Node).filter(Node.id == f3_id).first() is None, "")
    blob_after = db.query(Blob).filter(Blob.sha256 == f3_sha).first()
    check(
        "blob 引用递减",
        (blob_after.ref_count if blob_after else 0) == max(0, ref_before - 1),
        f"{ref_before} -> {blob_after.ref_count if blob_after else 0}",
    )

    print("\n[E] empty_trash 不带 confirm")
    out = ai_service.run_tool_safe(admin.id, "empty_trash", {}, {"delete": True})
    check("拒绝执行并说明原因", "无法" in out.text or "不可恢复" in out.text, out.text[:70])
    db.expire_all()
    check("回收站没被清空", bool(ai_service._trash_tops(db, admin.id)), "")

    print("\n[F] 指定名字清空")
    out = ai_service.run_tool_safe(
        admin.id, "empty_trash", {"names": ["绝对不存在的名字"], "confirm": True}, {"delete": True}
    )
    check("报错并列出可选项", "没有" in out.text and "现有" in out.text, out.text[:80])
    out = ai_service.run_tool_safe(
        admin.id, "empty_trash", {"names": [f1.name], "confirm": True}, {"delete": True}
    )
    check("只删掉点名那一项", "1 项" in out.text, out.text[:70])
    db.expire_all()
    check("该项已消失", db.query(Node).filter(Node.id == f1_id).first() is None, "")
    check("其他项还在", db.query(Node).filter(Node.id == folder_id).first() is not None, "")

    print("\n[G] 清空整个回收站")
    db.expire_all()
    f4 = db.query(Node).filter(Node.id == f4_id).first()
    out = ai_service.run_tool_safe(
        admin.id, "delete_item", {"paths": [f4.name]}, {"delete": True}
    )
    out = ai_service.run_tool_safe(admin.id, "empty_trash", {"confirm": True}, {"delete": True})
    check("回报已彻底删除", "彻底删除" in out.text, out.text[:70])
    db.expire_all()
    check("回收站空了", not ai_service._trash_tops(db, admin.id), "")
    check("目录子树也清了", db.query(Node).filter(Node.id == folder_id).first() is None, "")
    db.refresh(admin)
    check("配额回落到删除前", (admin.used_bytes or 0) < used_before, f"{used_before} -> {admin.used_bytes}")

    print("\n[H] 没勾快传权限")
    out = ai_service.run_tool_safe(
        admin.id, "send_to_user", {"paths": [f5.name], "to": receiver.username}, {"quick": False}
    )
    check("拒绝执行", "失败" in out.text and "快传" in out.text, out.text[:70])

    print("\n[I] 收件人异常")
    out = ai_service.run_tool_safe(
        admin.id, "send_to_user", {"paths": [f5.name], "to": "no_such_user_zzz"}, {"quick": True}
    )
    check("报找不到用户", "找不到" in out.text, out.text[:60])
    out = ai_service.run_tool_safe(
        admin.id, "send_to_user", {"paths": [f5.name], "to": admin.username}, {"quick": True}
    )
    check("不能发给自己", "自己" in out.text, out.text[:60])
    out = ai_service.run_tool_safe(
        admin.id, "send_to_user", {"paths": ["不存在的路径.txt"], "to": receiver.username},
        {"quick": True},
    )
    check("路径不存在如实报错", "找不到" in out.text, out.text[:60])

    print("\n[J] 用好友昵称定位收件人")
    from app.models import Friendship
    from app.models import utcnow as _utcnow

    db.expire_all()
    admin = db.query(User).filter(User.id == admin_id).first()
    peer = db.query(User).filter(User.id == receiver_id).first()
    qs.accept_terms(db, admin)
    peer.display_name = f"好友昵称{stamp}"
    db.query(Friendship).filter(
        Friendship.requester_id == admin.id, Friendship.addressee_id == peer.id
    ).delete()
    db.add(
        Friendship(
            requester_id=admin.id,
            addressee_id=peer.id,
            status="accepted",
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
    )
    db.commit()
    out = ai_service.run_tool_safe(
        admin.id,
        "send_to_user",
        {"paths": [f5.name], "to": f"好友昵称{stamp}"},
        {"quick": True},
    )
    check("按昵称也发得出去", "已通过站内快传" in out.text, out.text[:70])
    check("回报里带用户名", peer.username in out.text, out.text[:100])

    print("\n[K] 发送成功")
    # 快传要先同意条款
    db.expire_all()
    admin = db.query(User).filter(User.id == admin.id).first()
    qs.accept_terms(db, admin)
    db.commit()
    notice_before = (
        db.query(Notice).filter(Notice.user_id == receiver.id, Notice.kind == "quick").count()
    )
    out = ai_service.run_tool_safe(
        admin.id,
        "send_to_user",
        {"paths": [f5.name], "to": receiver.username, "message": "这个给你"},
        {"quick": True},
    )
    check("回报发送成功", "已通过站内快传" in out.text, out.text[:80])
    check("说明要等对方同意", "同意" in out.text, "")
    db.expire_all()
    qt = (
        db.query(QuickTransfer)
        .filter(QuickTransfer.kind == "send", QuickTransfer.owner_id == admin.id)
        .order_by(QuickTransfer.id.desc())
        .first()
    )
    check("建了快传记录", qt is not None and qt.peer_id == receiver.id, str(getattr(qt, "peer_id", None)))
    staged = (
        db.query(Node)
        .filter(Node.pending_send_id == qt.id, Node.user_id == receiver.id)
        .all()
        if qt
        else []
    )
    check("暂存副本打上 pending_send_id", bool(staged), str(len(staged)))
    check("暂存副本不占配额", all(n.quick_owned for n in staged), "")
    notice_after = (
        db.query(Notice).filter(Notice.user_id == receiver.id, Notice.kind == "quick").count()
    )
    check("接收方收到消息中心通知", notice_after == notice_before + 1, f"{notice_before} -> {notice_after}")
    check("通知带附言", any("这个给你" in (n.body or "") for n in db.query(Notice).filter(Notice.user_id == receiver.id, Notice.kind == "quick").all()), "")

    print("\n[L] 发文件夹")
    folder2 = ns.create_folder(db, admin, None, f"AI快传目录-{stamp}")
    db.commit()
    inner = make_file(f"夹里的文件-{stamp}.txt", folder2.id)
    out = ai_service.run_tool_safe(
        admin.id, "send_to_user", {"paths": [folder2.name], "to": receiver.username}, {"quick": True}
    )
    check("文件夹也能发", "已通过站内快传" in out.text, out.text[:70])
    db.expire_all()
    qt2 = (
        db.query(QuickTransfer)
        .filter(QuickTransfer.kind == "send", QuickTransfer.owner_id == admin.id)
        .order_by(QuickTransfer.id.desc())
        .first()
    )
    staged2 = db.query(Node).filter(Node.pending_send_id == qt2.id).all() if qt2 else []
    check("子文件进了暂存区", len(staged2) >= 2, f"{len(staged2)} 个节点")

    print("\n[M] 权限关闭时不下发工具")
    from app.ai_service import DEFAULT_CONFIG, tool_specs

    cfg = dict(DEFAULT_CONFIG)
    cfg.update(tools=True, deleteFiles=False, quickTransfer=False)
    names = [spec["function"]["name"] for spec in tool_specs(cfg)]
    check("无 delete_item / empty_trash", "delete_item" not in names and "empty_trash" not in names, "")
    check("无 send_to_user", "send_to_user" not in names, "")
    cfg.update(deleteFiles=True, quickTransfer=True)
    names = [spec["function"]["name"] for spec in tool_specs(cfg)]
    check("开了权限就下发", {"delete_item", "empty_trash", "send_to_user"} <= set(names), "")

    db.close()
    print(f"\n结果：{passed} 通过，{failed} 失败")
    import shutil

    if failed == 0:
        shutil.rmtree(TMP, ignore_errors=True)
    else:
        print("临时目录保留：", TMP)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
