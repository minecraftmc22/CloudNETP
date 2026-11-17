# -*- coding: utf-8 -*-
"""封禁与申诉冒烟：封禁详情、在线 403、登录票据、申诉提交、后台审核。

直接调路由函数（不起服务），把「谁被封 / 什么时候解 / 谁能审」钉住。

    ./.venv/Scripts/python.exe scripts/ban_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-ban-")).resolve()
DB = TMP / "ban.db"

os.environ["DATABASE_URL"] = f"sqlite:///{DB.as_posix()}"
os.environ["STORAGE_DIR"] = str(TMP / "storage")
os.environ["DB_FALLBACK_ENABLED"] = "0"
os.environ["DB_PROBE_INTERVAL"] = "0"
os.environ["DB_MIRROR_INTERVAL"] = "0"
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402

from app import audit, ban_service  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, engine, ensure_schema  # noqa: E402
from app.deps import get_current_user  # noqa: E402
from app.models import AuditLog, BanAppeal, User, utcnow  # noqa: E402
from app.routers import admin as admin_router  # noqa: E402
from app.routers import auth as auth_router  # noqa: E402
from app.routers import ban as ban_router  # noqa: E402
from app.schemas import (  # noqa: E402
    AdminAppealReviewIn,
    AdminUserPatchIn,
    BanAppealIn,
    LoginIn,
)
from app.security import create_token, hash_password, verify_ban_ticket  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def req(cookie: str | None = None, method: str = "POST", path: str = "/") -> Request:
    headers = []
    if cookie:
        headers.append((b"cookie", f"{settings.COOKIE_NAME}={cookie}".encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": headers,
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
        }
    )


def cookie_of(response: Response) -> str:
    raw = response.headers.get("set-cookie", "")
    for part in raw.split(";"):
        if "=" in part and settings.COOKIE_NAME in part:
            return part.split("=", 1)[1]
    return ""


def patch(db, me: User, target: User, **fields) -> dict:
    try:
        return {
            "status": 200,
            "data": admin_router.patch_user(
                target.id, AdminUserPatchIn(**fields), req(), db, me
            ),
        }
    except HTTPException as exc:
        return {"status": exc.status_code, "detail": exc.detail}


def new_user(db, name: str, role: int = 0, super_: bool = False) -> User:
    user = User(
        username=name,
        password_hash=hash_password("Passw0rd!2024"),
        role=role,
        is_super=super_,
        quota_bytes=1024**3,
        created_at=utcnow(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def main() -> int:
    ensure_schema(engine)
    with SessionLocal() as db:
        sup = new_user(db, "bansup", role=1, super_=True)
        adm = new_user(db, "banadm", role=1)
        adm2 = new_user(db, "banadm2", role=1)
        alice = new_user(db, "aliceban")
        alice_token = create_token(alice.id, alice.token_version)

        print("== 1. 封禁写入详情 ==")
        out = patch(
            db,
            sup,
            alice,
            banned=True,
            ban_reason="测试原因：上传违规文件",
            ban_days=7,
        )
        check("封禁成功", out["status"] == 200, str(out))
        db.refresh(alice)
        check("banned=True", bool(alice.banned))
        check("原因落库", alice.ban_reason == "测试原因：上传违规文件", str(alice.ban_reason))
        check("有封禁时间", alice.banned_at is not None)
        check(
            "到期时间是 7 天后",
            alice.ban_until is not None
            and abs((alice.ban_until - (utcnow() + timedelta(days=7))).total_seconds()) < 120,
            str(alice.ban_until),
        )
        check("记了操作人", alice.banned_by == sup.id, str(alice.banned_by))
        check("没有踢会话（token_version 不变）", alice.token_version == 1, str(alice.token_version))

        # 已封禁再说一次「封禁」：接口只在状态变化时写，但重复封禁要能刷新原因/时长
        out = patch(db, sup, alice, banned=True, ban_reason="改判：30 天", ban_days=30)
        db.refresh(alice)
        check("重复封禁可改判", out["status"] == 200 and alice.ban_reason == "改判：30 天", str(alice.ban_reason))

        print("== 2. 在线请求得到结构化 403 ==")
        try:
            get_current_user(req(alice_token, "GET", "/api/me"), db)
            check("banned 会话被拦", False)
        except HTTPException as exc:
            detail = exc.detail or {}
            check("状态码 403", exc.status_code == 403, str(exc.status_code))
            check("detail 带 code=banned", detail.get("code") == "banned", str(detail.keys()))
            check("detail 带原因", detail.get("ban", {}).get("reason") == "改判：30 天", str(detail.get("ban")))
            check("detail 带票据", bool(detail.get("ticket")), str(detail.get("ticket"))[:16])

        print("== 3. 登录被拒并下发票据 ==")
        res = auth_router.login(
            LoginIn(username="aliceban", password="Passw0rd!2024"), req(), Response(), db
        )
        check("登录返回 403", getattr(res, "status_code", 0) == 403, str(getattr(res, "status_code", None)))
        body = json.loads(res.body.decode())
        detail = body.get("detail") or {}
        check("登录 detail 是结构化的", detail.get("code") == "banned", str(detail.keys()))
        check("登录 detail 带封禁信息", detail.get("ban", {}).get("permanent") is False, str(detail.get("ban")))
        ticket = detail.get("ticket", "")
        check("登录下发票据", bool(ticket))
        check("票据写进 cookie", ban_router.BAN_COOKIE in res.headers.get("set-cookie", ""), res.headers.get("set-cookie", ""))
        found = verify_ban_ticket(ticket)
        check("票据可校验", found and found[0] == alice.id, str(found))
        check("过期票据无效", verify_ban_ticket("1.2.3.abc") is None)

        print("== 4. 申诉页读信息 / 提交 ==")
        info = ban_router.ban_info(req(), db, ticket=ticket)
        check("能读到封禁信息", info["ban"]["active"] is True and info["ban"]["reason"] == "改判：30 天", str(info))
        check("还没有申诉记录", info["appeal"] is None, str(info.get("appeal")))

        out = ban_router.submit_appeal(BanAppealIn(message="我不是故意的，请再给一次机会", ticket=ticket), req(), db)
        check("提交成功", out.get("ok") is True and out.get("appeal"), str(out))
        appeal_id = out["appeal"]["id"]
        check("状态是待审核", out["appeal"]["status"] == "pending", out["appeal"]["status"])
        check("快照了封禁原因", out["appeal"]["banReason"] == "改判：30 天", out["appeal"]["banReason"])

        try:
            ban_router.submit_appeal(BanAppealIn(message="再交一次", ticket=ticket), req(), db)
            check("重复提交被拒", False)
        except HTTPException as exc:
            check("重复提交被拒", exc.status_code == 409, str(exc.status_code))

        try:
            ban_router.submit_appeal(BanAppealIn(message="空票据"), req(), db)
            check("没有票据/会话时 401", False)
        except HTTPException as exc:
            check("没有票据/会话时 401", exc.status_code == 401, str(exc.status_code))

        print("== 5. 后台审核 ==")
        lst = ban_router.list_appeals(db, sup, status_filter="pending", page=1, page_size=20)
        check("管理员能看到待审申诉", lst["total"] == 1, str(lst["total"]))
        row = lst["items"][0]
        check("列表带用户与封禁快照", row["username"] == "aliceban" and row["banReason"], str(row.keys()))
        check("列表带当前封禁状态", row["banned"] is True and row["currentBan"]["reason"], str(row.get("currentBan")))

        try:
            ban_router.review_appeal(
                appeal_id, AdminAppealReviewIn(approve=True, reply=""), req(), db, adm2
            )
            check("普通管理员能审普通用户", True)
        except HTTPException as exc:
            check("普通管理员能审普通用户", False, str(exc.detail))
        db.refresh(alice)
        check("通过后解封", alice.banned is False and alice.ban_reason is None, str(alice.banned))
        row = db.get(BanAppeal, appeal_id)
        check("申诉标记为已通过", row.status == "approved", row.status)
        check("审核人/时间已记", row.handled_by == adm2.id and row.handled_at is not None)

        try:
            ban_router.review_appeal(
                appeal_id, AdminAppealReviewIn(approve=False, reply=""), req(), db, sup
            )
            check("已处理过的申诉不能再审", False)
        except HTTPException as exc:
            check("已处理过的申诉不能再审", exc.status_code == 400, str(exc.status_code))

        # 解封后票据还能回来看结果
        after = ban_router.ban_info(req(), db, ticket=ticket)
        check("解封后仍能查看申诉结果", after["ban"]["active"] is False and after["appeal"]["status"] == "approved", str(after["ban"]))

        print("== 6. 驳回与再次申诉 ==")
        patch(db, sup, alice, banned=True, ban_reason="二次违规", ban_days=0)
        db.refresh(alice)
        check("永久封禁时长留空", alice.ban_until is None)
        out = ban_router.submit_appeal(BanAppealIn(message="我还是想解释一下", ticket=ticket), req(), db)
        second_id = out["appeal"]["id"]
        res = ban_router.review_appeal(
            second_id, AdminAppealReviewIn(approve=False, reply="证据不足"), req(), db, sup
        )
        db.refresh(alice)
        check("驳回后仍然封禁", alice.banned is True and res["status"] == "rejected", str(res))
        out = ban_router.submit_appeal(BanAppealIn(message="补充了新的说明材料", ticket=ticket), req(), db)
        check("驳回后可以再申诉", out.get("ok") is True and out["appeal"]["status"] == "pending")

        print("== 7. 管理员申诉归超管 ==")
        patch(db, sup, adm, banned=True, ban_reason="管理员违规", ban_days=3)
        db.refresh(adm)
        adm_ticket = ""
        try:
            get_current_user(req(create_token(adm.id, adm.token_version), "GET", "/api/me"), db)
        except HTTPException as exc:
            adm_ticket = (exc.detail or {}).get("ticket", "")
        check("管理员被封也有票据", bool(adm_ticket))
        out = ban_router.submit_appeal(BanAppealIn(message="请复核", ticket=adm_ticket), req(), db)
        adm_appeal_id = out["appeal"]["id"]
        try:
            ban_router.review_appeal(
                adm_appeal_id, AdminAppealReviewIn(approve=True, reply=""), req(), db, adm2
            )
            check("普通管理员不能审管理员的申诉", False)
        except HTTPException as exc:
            check("普通管理员不能审管理员的申诉", exc.status_code == 403, str(exc.status_code))
        ban_router.review_appeal(
            adm_appeal_id, AdminAppealReviewIn(approve=True, reply="已复核"), req(), db, sup
        )
        db.refresh(adm)
        check("超管能通过管理员的申诉", adm.banned is False)

        print("== 8. 到期自动解封 ==")
        patch(db, sup, alice, banned=True, ban_reason="临时", ban_days=1)
        db.refresh(alice)
        alice.ban_until = utcnow() - timedelta(minutes=1)
        db.commit()
        check("到期判定", ban_service.is_expired(alice) is True)
        before = alice.token_version
        user = get_current_user(req(create_token(alice.id, before), "GET", "/api/me"), db)
        check("到期后直接放行", user.id == alice.id)
        db.refresh(alice)
        check("到期后清空封禁字段", alice.banned is False and alice.ban_reason is None)
        logs = db.query(AuditLog).filter(AuditLog.action == "ban_expired").count()
        check("自动解封有审计", logs >= 1, str(logs))

        print("== 9. 审计动作字典 ==")
        check("ai_tool 不再显示「其它操作」", audit.describe("ai_tool")["label"] == "看板娘调用工具", audit.describe("ai_tool")["label"])
        unknown = [a for a in audit.ACTIONS if audit.describe(a)["label"] == "其它操作"]
        check("所有登记动作都有中文名", not unknown, str(unknown))
        check("申诉动作已登记", audit.describe("admin_appeal_approve")["group"] == "管理")

        print("== 10. 解封后能正常登录 ==")
        res = auth_router.login(
            LoginIn(username="aliceban", password="Passw0rd!2024"), req(), Response(), db
        )
        check("解封后登录成功", isinstance(res, dict) and res.get("user", {}).get("username") == "aliceban", str(type(res)))

    print()
    print(f"通过 {PASS} / {PASS + FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
