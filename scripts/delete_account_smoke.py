# -*- coding: utf-8 -*-
"""注销账号冒烟：身份校验、数据清理、唯一管理员保护、审计与登录态清理。

直接调路由函数（不起服务），把「删谁的、删了什么、什么人不能删」钉住。

    ./.venv/Scripts/python.exe scripts/delete_account_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-delacc-")).resolve()
DB = TMP / "delacc.db"

os.environ["DATABASE_URL"] = f"sqlite:///{DB.as_posix()}"
os.environ["STORAGE_DIR"] = str(TMP / "storage")
os.environ["DB_FALLBACK_ENABLED"] = "0"
os.environ["DB_PROBE_INTERVAL"] = "0"
os.environ["DB_MIRROR_INTERVAL"] = "0"
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, ensure_schema, engine  # noqa: E402
from app.models import (  # noqa: E402
    AiChatMessage,
    AiChatSession,
    AudioPref,
    AuditLog,
    Blob,
    Feedback,
    Node,
    QuickPickupItem,
    QuickTransfer,
    Share,
    ShareReport,
    User,
    UserSetting,
    utcnow,
)
from app.routers import auth as auth_router  # noqa: E402
from app.schemas import AccountDeleteIn  # noqa: E402
from app.security import hash_password, verify_password  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def req(cookie: str | None = None) -> Request:
    headers = []
    if cookie:
        headers.append((b"cookie", f"{settings.COOKIE_NAME}={cookie}".encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/me/delete",
            "headers": headers,
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
        }
    )


def make_user(db, name: str, *, email: str | None = None, role: int = 0, is_super: bool = False) -> User:
    user = User(
        username=name,
        password_hash=hash_password("Passw0rd!2024"),
        email=email,
        email_verified=False,
        role=role,
        is_super=is_super,
        quota_bytes=10 * 1024**3,
        token_version=1,
        created_at=utcnow(),
    )
    db.add(user)
    db.flush()
    return user


def count(db, model, where) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(where)) or 0)


ensure_schema(engine)

SHA = "b" * 64

with SessionLocal() as db:
    victim = make_user(db, "delvictim", email="victim@example.com")
    # 0.6.0 起注销要求「已验证邮箱 + 邮件服务就绪」才验验证码；钉行为需要已验证
    victim.email_verified = True
    other = make_user(db, "delother", role=1, is_super=True)
    admin = make_user(db, "deladmin", role=0)

    # 两个用户各有一个节点指向同一份 blob：删掉 victim 后 blob 仍在（ref_count 要降）
    db.add(Blob(sha256=SHA, size=1024, ref_count=2, created_at=utcnow()))

    folder = Node(user_id=victim.id, parent_id=None, name="文档", type="folder", created_at=utcnow())
    db.add(folder)
    db.flush()
    node = Node(
        user_id=victim.id,
        parent_id=folder.id,
        name="账单.pdf",
        type="file",
        size=1024,
        blob_sha256=SHA,
        created_at=utcnow(),
    )
    other_node = Node(
        user_id=other.id,
        parent_id=None,
        name="同一份.pdf",
        type="file",
        size=1024,
        blob_sha256=SHA,
        created_at=utcnow(),
    )
    db.add_all([node, other_node])
    db.flush()

    share = Share(
        token="tok-" + "x" * 20,
        user_id=victim.id,
        node_id=node.id,
        created_at=utcnow(),
    )
    db.add(share)
    db.flush()
    db.add(
        ShareReport(
            share_id=share.id,
            quick_id=None,
            token=share.token,
            first_name="账单.pdf",
            size=1024,
            is_folder=False,
            reason="测试",
            created_at=utcnow(),
        )
    )

    quick = QuickTransfer(
        kind="pickup",
        owner_id=victim.id,
        token="qt-" + "y" * 20,
        code="ABC123",
        name="账单.pdf",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(quick)
    db.flush()
    db.add(QuickPickupItem(quick_id=quick.id, node_id=node.id, created_at=utcnow()))

    session = AiChatSession(user_id=victim.id, title="聊点啥", created_at=utcnow(), updated_at=utcnow())
    db.add(session)
    db.flush()
    db.add(
        AiChatMessage(
            session_id=session.id,
            role="user",
            content="你好",
            images="[]",
            created_at=utcnow(),
        )
    )
    db.add(Feedback(user_id=victim.id, kind="idea", title="t", content="c", created_at=utcnow()))
    db.add(AudioPref(node_id=node.id, mode="auto"))
    db.add(UserSetting(user_id=victim.id))
    db.commit()

    victim_id, other_id, share_id, quick_id, session_id, node_id = (
        victim.id,
        other.id,
        share.id,
        quick.id,
        session.id,
        node.id,
    )

print("== 身份校验 ==")
# 0.6.0 起的流程是「密码 + 发到绑定邮箱的验证码」：邮件服务就绪且邮箱已验证时
# 必须带验证码。冒烟环境没有 SMTP，这里把 ready 与 verify 换成假实现来钉行为。
from app import email_code as email_code_mod  # noqa: E402

_real_ready = auth_router.mail_config.ready
_real_verify = email_code_mod.verify
auth_router.mail_config.ready = lambda: True
email_code_mod.verify = (
    lambda scene, email, code: (
        bool(code == "246810" and email == "victim@example.com"),
        "" if (code == "246810" and email == "victim@example.com") else "验证码不正确",
    )
)

try:
    with SessionLocal() as db:
        victim = db.get(User, victim_id)
        try:
            auth_router.delete_me(
                AccountDeleteIn(password="wrong-pass", email_code="246810"),
                req(),
                Response(),
                victim,
                db,
            )
            check("密码错被拒", False)
        except HTTPException as exc:
            check("密码错被拒", exc.status_code == 400, str(exc.detail))

        try:
            auth_router.delete_me(AccountDeleteIn(password="Passw0rd!2024"), req(), Response(), victim, db)
            check("绑了邮箱必须填验证码", False)
        except HTTPException as exc:
            check("绑了邮箱必须填验证码", exc.status_code == 400, str(exc.detail))

        try:
            auth_router.delete_me(
                AccountDeleteIn(password="Passw0rd!2024", email_code="000000"),
                req(),
                Response(),
                victim,
                db,
            )
            check("验证码错被拒", False)
        except HTTPException as exc:
            check("验证码错被拒", exc.status_code == 400, str(exc.detail))

        check("三次失败后账号还在", db.get(User, victim_id) is not None)

    print("== 正常注销 ==")
    with SessionLocal() as db:
        victim = db.get(User, victim_id)
        response = Response()
        out = auth_router.delete_me(
            AccountDeleteIn(password="Passw0rd!2024", email_code="246810"),
            req(),
            response,
            victim,
            db,
        )
        check("注销成功", out.get("deleted") is True, str(out))
        check("返回删了多少", out.get("nodes", 0) >= 2, str(out))
        cookie_header = response.headers.get("set-cookie", "")
        check(
            "清了登录 cookie",
            settings.COOKIE_NAME in cookie_header and "Max-Age=0" in cookie_header,
            cookie_header[:80],
        )
finally:
    auth_router.mail_config.ready = _real_ready
    email_code_mod.verify = _real_verify

with SessionLocal() as db:
    check("用户已删除", db.get(User, victim_id) is None)
    check("节点已删除", count(db, Node, Node.user_id == victim_id) == 0)
    check("分享已删除", count(db, Share, Share.user_id == victim_id) == 0)
    check("举报记录已删除", count(db, ShareReport, ShareReport.share_id == share_id) == 0)
    check("快传已删除", count(db, QuickTransfer, QuickTransfer.id == quick_id) == 0)
    check("取件项已删除", count(db, QuickPickupItem, QuickPickupItem.quick_id == quick_id) == 0)
    check("AI 会话已删除", count(db, AiChatSession, AiChatSession.user_id == victim_id) == 0)
    check("AI 消息已删除", count(db, AiChatMessage, AiChatMessage.session_id == session_id) == 0)
    check("歌词偏好已删除", count(db, AudioPref, AudioPref.node_id == node_id) == 0)
    check("意见反馈已删除", count(db, Feedback, Feedback.user_id == victim_id) == 0)
    check("设置已删除", count(db, UserSetting, UserSetting.user_id == victim_id) == 0)

    blob = db.get(Blob, SHA)
    check("共用 blob 还在（别人也在用）", blob is not None and int(blob.ref_count) == 1, str(blob.ref_count if blob else None))
    check("别人的节点没被动", count(db, Node, Node.user_id == other_id) == 1)

    log = db.scalars(
        select(AuditLog).where(AuditLog.action == "account_delete").order_by(AuditLog.id.desc())
    ).first()
    check("记了审计", log is not None)
    check("审计里写了用户名", log is not None and "delvictim" in (log.detail or ""), (log.detail or "")[:80])
    check("审计不挂在已删用户上", log is not None and log.user_id is None)

print("== 没绑邮箱：只验密码 ==")
with SessionLocal() as db:
    plain = make_user(db, "delplain")
    db.commit()
    plain_id = plain.id
with SessionLocal() as db:
    plain = db.get(User, plain_id)
    out = auth_router.delete_me(AccountDeleteIn(password="Passw0rd!2024"), req(), Response(), plain, db)
    check("无邮箱也能注销", out.get("deleted") is True)
    check("无邮箱账号已删除", db.get(User, plain_id) is None)

print("== 唯一管理员 / 最后一个超管 ==")
with SessionLocal() as db:
    solo = make_user(db, "delsolo", role=1, is_super=True)
    db.commit()
    solo_id = solo.id
    # 先把其它管理员降级，确保只剩他
    for row in db.scalars(select(User).where(User.role == 1)).all():
        if row.id != solo_id:
            row.role = 0
            row.is_super = False
    db.commit()

with SessionLocal() as db:
    solo = db.get(User, solo_id)
    try:
        auth_router.delete_me(AccountDeleteIn(password="Passw0rd!2024"), req(), Response(), solo, db)
        check("最后一个管理员不能注销", False)
    except HTTPException as exc:
        check("最后一个管理员不能注销", exc.status_code == 400, str(exc.detail))

with SessionLocal() as db:
    # 造两个管理员：一个超管 + 一个普通管理员，普通管理员可以注销自己
    sup = make_user(db, "delsup2", role=1, is_super=True)
    plain_admin = make_user(db, "deladm2", role=1)
    db.commit()
    plain_admin_id = plain_admin.id
with SessionLocal() as db:
    plain_admin = db.get(User, plain_admin_id)
    out = auth_router.delete_me(
        AccountDeleteIn(password="Passw0rd!2024"), req(), Response(), plain_admin, db
    )
    check("有别的管理员时可以注销", out.get("deleted") is True)

print("== 审计动作有中文名 ==")
from app import audit  # noqa: E402

check("account_delete 已登记", "account_delete" in audit.ACTIONS)
check(
    "标签是「注销账号」",
    audit.ACTIONS["account_delete"]["label"] == "注销账号",
    audit.ACTIONS["account_delete"]["label"],
)
check("密码校验函数可用", verify_password("Passw0rd!2024", hash_password("Passw0rd!2024")))

print()
print(f"通过 {PASS} / {PASS + FAIL}")
sys.exit(1 if FAIL else 0)
