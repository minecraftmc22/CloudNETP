# -*- coding: utf-8 -*-
"""给隔离测试库造几条后台数据（举报 / 申诉 / 反馈），只为手机端排版看起来。

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
    for i in range(2):
        db.add(
            ShareReport(
                share_id=0,
                token="t" * 20,
                blob_sha256=("a1b2c3d4" * 8) if i == 0 else None,
                first_name="分片演示-2026-10-29-季度汇报终版-带备注很长很长.bin" if i == 0 else "项目资料",
                size=629145600 if i == 0 else 0,
                is_folder=bool(i),
                reason="这个文件好像涉及版权内容，请管理员核实一下，谢谢。" if i == 0 else "",
                reporter_id=admin.id,
                reporter_ip="100.93.42.52",
                status="pending",
                created_at=now - timedelta(hours=i + 1),
            )
        )
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
    for i, node in enumerate(files[:1]):
        node.deleted_at = now - timedelta(days=1)
        node.restore_parent_id = node.parent_id
    if files:
        db.add(
            Share(
                user_id=files[0].user_id,
                node_id=files[0].id,
                token="s" * 20,
                extract_code=None,
                created_at=now - timedelta(days=2),
            )
        )
    db.commit()
    print("已写入测试数据")
finally:
    db.close()
