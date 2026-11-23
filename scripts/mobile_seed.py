# -*- coding: utf-8 -*-
"""给隔离测试库造几条后台数据（举报 / 申诉 / 反馈 / 聊天举报），只为手机端排版看起来。

只允许跑在隔离库上：DATABASE_URL 不含 tmp_ / test 就拒绝执行。
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db_failover  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import (  # noqa: E402
    BanAppeal,
    ChatConversation,
    ChatMember,
    ChatMessage,
    ChatReport,
    Feedback,
    Node,
    Share,
    ShareReport,
    User,
    utcnow,
)

URL = settings.DATABASE_URL
if not any(k in URL.lower() for k in ("tmp", "test")):
    sys.exit(f"拒绝在非隔离库上写数据：{URL}")

if not db_failover._started:
    db_failover.startup()
db = db_failover.current_sessionmaker()()
try:
    admin = db.query(User).order_by(User.id.asc()).first()
    now = utcnow()
    # 幂等：重复跑不会撞 shares.token 唯一键，也不会把举报/反馈越堆越多
    if not db.query(ShareReport).filter(ShareReport.token == "t" * 20).first():
        db.add_all(
            [
                ShareReport(
                    share_id=0,
                    token="t" * 20,
                    blob_sha256=("a1b2c3d4" * 8) if i == 0 else None,
                    first_name="分片演示-2026-10-29-季度汇报终版-带备注很长很长.bin"
                    if i == 0
                    else "项目资料",
                    size=629145600 if i == 0 else 0,
                    is_folder=bool(i),
                    reason="这个文件好像涉及版权内容，请管理员核实一下，谢谢。" if i == 0 else "",
                    reporter_id=admin.id,
                    reporter_ip="100.93.42.52",
                    status="pending",
                    created_at=now - timedelta(hours=i + 1),
                )
                for i in range(2)
            ]
        )
    if not db.query(Feedback).first():
        db.add(
            Feedback(
                user_id=admin.id,
                kind="suggestion",
                title="希望手机端能直接管理用户",
                content="在手机上打开后台，用户行只剩一个名字，配额和操作按钮都看不到。",
                contact="sakura@example.com",
                status=0,
                created_at=now - timedelta(hours=3),
            )
        )
    if not db.query(BanAppeal).filter(BanAppeal.status == "pending").first():
        db.add(
            BanAppeal(
                user_id=admin.id,
                message="我不是故意的，那个文件是朋友传给我的，我并不知道里面是什么内容。请解封。",
                status="pending",
                ban_reason="分享违规内容",
                created_at=now - timedelta(hours=5),
            )
        )
    db.commit()
    # 回收站 / 我的分享：让列表里有内容，才能看出手机版行布局
    files = db.query(Node).filter(Node.deleted_at.is_(None), Node.type == "file").limit(3).all()
    if not db.query(Node).filter(Node.deleted_at.is_not(None)).first():
        for node in files[:1]:
            node.deleted_at = now - timedelta(days=1)
            node.restore_parent_id = node.parent_id
    if files and not db.query(Share).filter(Share.token == "s" * 20).first():
        db.add(
            Share(
                user_id=files[0].user_id,
                node_id=files[0].id,
                token="s" * 20,
                extract_code=None,
                created_at=now - timedelta(days=2),
            )
        )
    # 聊天举报：后台「聊天举报」页签要有待处理数据，才看得出窄屏行布局
    pair = db.query(User).order_by(User.id.asc()).limit(2).all()
    if len(pair) >= 2 and not db.query(ChatReport).filter(ChatReport.status == "pending").first():
        me, peer = pair[0], pair[1]
        dkey = f"{min(me.id, peer.id)}-{max(me.id, peer.id)}"
        conv = db.query(ChatConversation).filter(ChatConversation.direct_key == dkey).first()
        if not conv:
            conv = ChatConversation(kind="direct", title="", owner_id=me.id, direct_key=dkey)
            db.add(conv)
            db.flush()
            db.add(ChatMember(conversation_id=conv.id, user_id=me.id, role="member"))
            db.add(ChatMember(conversation_id=conv.id, user_id=peer.id, role="member"))
        text = "这是一条用于测试后台审核布局的聊天消息，内容写得比较长，用来看窄屏下省略号是否生效。"
        msg = ChatMessage(conversation_id=conv.id, sender_id=peer.id, kind="text", body=text)
        db.add(msg)
        db.flush()
        conv.last_message_id = msg.id
        conv.last_message_at = msg.created_at
        db.add(
            ChatReport(
                message_id=msg.id,
                conversation_id=conv.id,
                reporter_id=me.id,
                target_id=peer.id,
                reason="发布广告与无关内容，反复骚扰",
                snapshot=f"[text] {text}",
                status="pending",
            )
        )

        title = "测试群聊 · 项目协作"
        group = db.query(ChatConversation).filter(ChatConversation.title == title).first()
        if not group:
            group = ChatConversation(kind="group", title=title, owner_id=peer.id)
            db.add(group)
            db.flush()
            for u in (me, peer):
                db.add(
                    ChatMember(
                        conversation_id=group.id,
                        user_id=u.id,
                        role="owner" if u.id == peer.id else "member",
                    )
                )
        # 文件消息只存快照，不落物理文件（后台只展示摘要）
        fmsg = ChatMessage(
            conversation_id=group.id,
            sender_id=peer.id,
            kind="file",
            payload='{"nodes":[{"id":1,"name":"报价单最终版.xlsx","size":20480,'
            '"mime":"","folder":false,"icon":"sheet"}],"count":1,"size":20480}',
        )
        db.add(fmsg)
        db.flush()
        group.last_message_id = fmsg.id
        group.last_message_at = fmsg.created_at
        db.add(
            ChatReport(
                message_id=fmsg.id,
                conversation_id=group.id,
                reporter_id=me.id,
                target_id=peer.id,
                reason="疑似泄露公司内部文件",
                snapshot="[file] 报价单最终版.xlsx 1 个文件 · 20 KB",
                status="pending",
            )
        )
    db.commit()
    print("已写入测试数据")
finally:
    db.close()
