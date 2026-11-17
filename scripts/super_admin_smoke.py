# -*- coding: utf-8 -*-
"""超管权限冒烟：首个账号成超管、老库自动升级、成员角色只有超管能动。

直接调路由函数（不启服务），把「谁能改角色 / 谁能封禁 / 谁能重置密码」
这些规则一条条钉住——这里错一条就是提权漏洞。

    ./.venv/Scripts/python.exe scripts/super_admin_smoke.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-super-")).resolve()
DB = TMP / "super.db"


def snapshot_db(dst: Path) -> None:
    src_path = ROOT / "dev.db"
    if not src_path.is_file():
        src_path = ROOT / "storage" / "db" / "fallback.db"
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

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402

from app import captcha as cap  # noqa: E402
from app.db import (  # noqa: E402
    SessionLocal,
    _bootstrap_super_admin,
    _build_engine,
    engine,
    ensure_schema,
)
from app.deps import require_admin, require_super  # noqa: E402
from app.models import AuditLog, User, utcnow  # noqa: E402
from app.routers import admin as admin_router  # noqa: E402
from app.routers import auth as auth_router  # noqa: E402
from app.schemas import AdminUserPatchIn, RegisterIn  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.settings_store import set_value  # noqa: E402
from app.user_service import ensure_settings, user_payload  # noqa: E402

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label}  {extra}")


def req() -> Request:
    return Request(
        {
            "type": "http",
            "method": "PATCH",
            "path": "/api/admin/users/0",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
        }
    )


def patch(db, me: User, target: User, **fields) -> dict:
    """调后台的用户补丁接口，返回 {"status": 200|错误码, "detail": ...}。"""
    try:
        return {
            "status": 200,
            "data": admin_router.patch_user(
                target.id, AdminUserPatchIn(**fields), req(), db, me
            ),
        }
    except HTTPException as exc:
        return {"status": exc.status_code, "detail": exc.detail}


# --------------------------------------------------------------------------- #
# A. 老库升级：没有任何超管时，最早的管理员自动成为超管
# --------------------------------------------------------------------------- #
print("== A 老库升级 ==")
ensure_schema(engine)
with SessionLocal() as db:
    supers = db.scalars(select(User).where(User.is_super.is_(True))).all()
    first_admin = db.scalar(
        select(User).where(User.role == 1).order_by(User.id.asc()).limit(1)
    )
    check("升级后有超管", len(supers) >= 1, str(len(supers)))
    check(
        "超管就是最早的管理员",
        bool(supers and first_admin and supers[0].id == first_admin.id),
        f"{[u.id for u in supers]} vs {getattr(first_admin, 'id', None)}",
    )
    check("超管同时是管理员", all(int(u.role) == 1 for u in supers))

# 有人直接在数据库里把 is_super 打开（role 还是 0）：role 要被补齐，管理员接口也要认
with SessionLocal() as db:
    manual = User(
        username=f"db-super-{int(time.time())}",
        password_hash=hash_password("Passw0rd!2024"),
        role=0,
        is_super=True,
        quota_bytes=1024**3,
        created_at=utcnow(),
    )
    db.add(manual)
    db.commit()
    db.refresh(manual)
    manual_id = manual.id
_bootstrap_super_admin(engine)
with SessionLocal() as db:
    manual = db.get(User, manual_id)
    check("手改 is_super 后 role 被补齐", int(manual.role) == 1, str(manual.role))
    check("require_admin 认 is_super", require_admin(manual).id == manual_id)

# --------------------------------------------------------------------------- #
# B. 全新库里，第一个注册的账号就是超管
# --------------------------------------------------------------------------- #
print("== B 首个注册账号 ==")
fresh_engine = _build_engine(f"sqlite:///{(TMP / 'fresh.db').as_posix()}")
ensure_schema(fresh_engine)
Fresh = sessionmaker(bind=fresh_engine, expire_on_commit=False)


def code_for() -> tuple[str, str]:
    cid, _svg = cap.issue("127.0.0.1")
    return cid, cap._codes[cid].code


with Fresh() as db:
    set_value(db, "allow_register", "1")
    db.commit()
    cid, code = code_for()
    first = auth_router.register(
        RegisterIn(
            username="o-first",
            password="Passw0rd!2024",
            captchaId=cid,
            captcha=code,
            agree=True,
        ),
        req(),
        Response(),
        db,
    )
    check("首个账号 isSuper", first["user"]["isSuper"] is True, str(first["user"].get("isSuper")))
    check("首个账号 role=1", first["user"]["role"] == 1, str(first["user"].get("role")))
    check("返回 firstAdmin", first["firstAdmin"] is True)

    cid2, code2 = code_for()
    second = auth_router.register(
        RegisterIn(
            username="o-second",
            password="Passw0rd!2024",
            captchaId=cid2,
            captcha=code2,
            agree=True,
        ),
        req(),
        Response(),
        db,
    )
    check("第二个账号不是超管", second["user"]["isSuper"] is False)
    check("第二个账号是普通用户", second["user"]["role"] == 0)
    check("第二个账号 firstAdmin=false", second["firstAdmin"] is False)

# --------------------------------------------------------------------------- #
# C. 权限矩阵
# --------------------------------------------------------------------------- #
print("== C 权限矩阵 ==")
stamp = str(int(time.time()))
with SessionLocal() as db:
    def make(name: str, role: int, is_super: bool = False) -> User:
        user = User(
            username=f"{name}-{stamp}",
            password_hash=hash_password("Passw0rd!2024"),
            role=role,
            is_super=is_super,
            quota_bytes=1024**3,
            created_at=utcnow(),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        ensure_settings(db, user.id)
        return user

    sup = make("sroot", 1, True)
    adm = make("sadm", 1, False)
    adm2 = make("sadm2", 1, False)
    plain = make("splain", 0, False)

    # 普通管理员的越权尝试
    out = patch(db, adm, plain, role=1)
    check("管理员不能把别人设为管理员", out["status"] == 403, str(out))
    out = patch(db, adm, plain, isSuper=True)
    check("管理员不能把别人设为超管", out["status"] == 403, str(out))
    out = patch(db, adm, sup, isSuper=False)
    check("管理员不能取消超管", out["status"] == 403, str(out))
    out = patch(db, adm, sup, role=0)
    check("管理员不能降级超管", out["status"] == 403, str(out))
    out = patch(db, adm, sup, banned=True)
    check("管理员不能封禁超管", out["status"] == 400, str(out))

    try:
        admin_router.reset_password(sup.id, req(), db, adm)
        check("管理员不能重置超管密码", False)
    except HTTPException as exc:
        check("管理员不能重置超管密码", exc.status_code == 403, str(exc.status_code))
    try:
        admin_router.delete_user(sup.id, req(), db, adm)
        check("管理员不能删除超管", False)
    except HTTPException as exc:
        check("管理员不能删除超管", exc.status_code == 400, str(exc.detail))

    # 管理员对普通用户的日常操作不受影响
    out = patch(db, adm, plain, banned=True)
    check("管理员仍能封禁普通用户", out["status"] == 200 and out["data"]["user"]["banned"] is True)
    out = patch(db, adm, plain, banned=False)
    check("管理员仍能解封", out["status"] == 200)
    out = patch(db, adm, plain, quotaBytes=2 * 1024**3)
    check("管理员仍能改配额", out["status"] == 200)

    # 管理员之间互相不能动（同级接管同样是提权）
    out = patch(db, adm, adm2, banned=True)
    check("管理员不能封禁另一个管理员", out["status"] == 403, str(out))

    # 被封禁的管理员也不能由同级悄悄解封；超管可以，且解封后才允许设超管
    out = patch(db, sup, adm2, banned=True)
    check("超管能封禁管理员", out["status"] == 200 and out["data"]["user"]["banned"] is True, str(out))
    out = patch(db, adm, adm2, banned=False)
    check("管理员不能解封另一个管理员", out["status"] == 403, str(out))
    out = patch(db, sup, adm2, isSuper=True)
    check("封禁中的账号不能设超管", out["status"] == 400, str(out))
    out = patch(db, sup, adm2, banned=False)
    check("超管能解封管理员", out["status"] == 200 and out["data"]["user"]["banned"] is False, str(out))
    try:
        admin_router.reset_password(adm2.id, req(), db, adm)
        check("管理员不能重置另一个管理员的密码", False)
    except HTTPException as exc:
        check("管理员不能重置另一个管理员的密码", exc.status_code == 403, str(exc.status_code))
    try:
        admin_router.delete_user(adm2.id, req(), db, adm)
        check("管理员不能删除另一个管理员", False)
    except HTTPException as exc:
        check("管理员不能删除另一个管理员", exc.status_code == 403, str(exc.status_code))

    try:
        res = admin_router.reset_password(adm2.id, req(), db, sup)
        check("超管能重置管理员密码", bool(res.get("password")))
    except HTTPException as exc:
        check("超管能重置管理员密码", False, str(exc.detail))

    # 超管的正常操作
    out = patch(db, sup, plain, role=1)
    check("超管能把人设为管理员", out["status"] == 200 and out["data"]["user"]["role"] == 1)
    out = patch(db, sup, plain, role=0)
    check("超管能取消管理员", out["status"] == 200 and out["data"]["user"]["role"] == 0)
    out = patch(db, sup, plain, isSuper=True)
    check(
        "超管能设超管（同时升为管理员）",
        out["status"] == 200
        and out["data"]["user"]["isSuper"] is True
        and out["data"]["user"]["role"] == 1,
        str(out.get("data", {}).get("user")),
    )
    out = patch(db, sup, plain, role=0)
    check("不能直接把超管降成普通用户", out["status"] == 400, str(out))
    out = patch(db, sup, plain, isSuper=False)
    check(
        "取消超管后仍是管理员",
        out["status"] == 200
        and out["data"]["user"]["isSuper"] is False
        and out["data"]["user"]["role"] == 1,
        str(out.get("data", {}).get("user")),
    )

    out = patch(db, sup, sup, isSuper=False)
    check("超管不能取消自己", out["status"] == 400, str(out))
    out = patch(db, sup, sup, role=0)
    check("超管不能改自己角色", out["status"] == 400, str(out))

    # 超管之间可以互相管理，但不能把人删光
    out = patch(db, adm2, plain, isSuper=True)
    check("普通管理员设超管仍被拒", out["status"] == 403)
    out = patch(db, sup, adm2, isSuper=True)
    check("超管能再提一个超管", out["status"] == 200 and out["data"]["user"]["isSuper"] is True)
    out = patch(db, sup, adm2, isSuper=False)
    check("超管能取消另一个超管", out["status"] == 200)

    # 依赖注入层的双保险
    check("require_super 放行超管", require_super(sup).id == sup.id)
    try:
        require_super(adm)
        check("require_super 拦普通管理员", False)
    except HTTPException as exc:
        check("require_super 拦普通管理员", exc.status_code == 403)
    check("require_admin 仍放行普通管理员", require_admin(adm).id == adm.id)

    # 返回给前端的数据要带 isSuper，否则菜单判断不了
    check("user_payload 带 isSuper", user_payload(db, sup).get("isSuper") is True)
    rows = admin_router.list_users(db, sup, query=stamp, state="all", page=1, page_size=50)["items"]
    mine = {row["username"]: row for row in rows}
    check(
        "列表行带 isSuper",
        mine.get(sup.username, {}).get("isSuper") is True
        and mine.get(adm.username, {}).get("isSuper") is False,
        str({k: v.get("isSuper") for k, v in mine.items()}),
    )

    # 审计
    import json

    logs = db.scalars(
        select(AuditLog).where(AuditLog.action == "admin_user_super")
    ).all()
    check("角色变更有审计", len(logs) >= 1, str(len(logs)))

    def detail_of(entry: AuditLog) -> dict:
        try:
            return json.loads(entry.detail or "{}")
        except (TypeError, ValueError):
            return {}

    check(
        "审计里记了目标与结果",
        any(detail_of(entry).get("target") == plain.username for entry in logs),
        str([detail_of(entry) for entry in logs][:3]),
    )

    db.rollback()

print()
print(f"通过 {PASS} / {PASS + FAIL}")
import shutil  # noqa: E402

if FAIL == 0:
    shutil.rmtree(TMP, ignore_errors=True)
else:
    print("临时目录保留：", TMP)
sys.exit(1 if FAIL else 0)
