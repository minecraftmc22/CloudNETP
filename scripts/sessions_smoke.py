# -*- coding: utf-8 -*-
"""登录设备管理（login_sessions）的接口回归。

钉住这几件事：
- 登录会落一条设备记录，列表里能看到解析出的设备名与 IP，且带「当前设备」标记
- 两次登录 = 两条记录；下线旧设备后，那台设备下一次请求立刻 401
- 不允许用「下线单台」接口下线当前设备（想退当前设备走退出登录）
- 「下线其他设备」后除当前外全部 401
- 退出登录会把当前会话标记下线
- 升级前签发的旧式 token（无 s 声明）继续有效，不能被单台下线误伤
- 管理员重置密码（token_version bump + revoke_all）后设备列表只剩新登录

用法（需要一个已启动的隔离实例）：
    python scripts/sessions_smoke.py [port]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from sqlalchemy import select  # noqa: E402

from app import db_failover  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import User  # noqa: E402
from app.security import create_token, hash_password  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8131
BASE = f"http://127.0.0.1:{PORT}"
RESULTS: list[tuple[bool, str]] = []

if not any(k in settings.DATABASE_URL.lower() for k in ("tmp", "test")):
    sys.exit(f"拒绝在非隔离库上跑：{settings.DATABASE_URL}")

UA_DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0"
UA_PHONE = "Mozilla/5.0 (Linux; Android 14) Chrome/124.0.0.0 Mobile"
UA_TABLET = "Mozilla/5.0 (iPad; CPU OS 17) Safari/605.1.15"


def check(ok: bool, msg: str) -> None:
    RESULTS.append((bool(ok), msg))


def call(
    path: str,
    token: str | None = None,
    method: str = "GET",
    body=None,
    ua: str = UA_DESKTOP,
):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, method=method, data=data)
    req.add_header("Content-Type", "application/json")
    if ua:
        req.add_header("User-Agent", ua)
    if token:
        req.add_header("Cookie", f"{settings.COOKIE_NAME}={token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read() or b"{}"), res.headers
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read() or b"{}"), err.headers
        except Exception:
            return err.code, {}, err.headers


def login_token(username: str, password: str, ua: str) -> str:
    """走真实登录接口拿 Cookie，顺便让服务端落设备记录。"""
    code, data, headers = call(
        "/api/auth/login", method="POST", body={"username": username, "password": password}, ua=ua
    )
    assert code == 200, f"登录失败 {code} {data}"
    raw = (headers.get("Set-Cookie") or "").strip()
    assert raw, "登录响应没有 Set-Cookie"
    return raw.split(";", 1)[0].split("=", 1)[1]


def main() -> None:
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    try:
        row = db.scalar(select(User).where(User.username == "sess_user"))
        if row is None:
            row = User(
                username="sess_user",
                password_hash=hash_password("SmokeTest123"),
                role=0,
                quota_bytes=4 * 1024**3,
                used_bytes=0,
                display_name="sess_user",
            )
            db.add(row)
            db.commit()
            db.refresh(row)
        uid = row.id
    finally:
        db.close()

    # 清掉之前跑剩的会话，保证断言从零开始
    from app import session_service

    purge = db_failover.current_sessionmaker()()
    try:
        from app.models import LoginSession

        purge.query(LoginSession).filter(LoginSession.user_id == uid).delete()
        purge.commit()
    finally:
        purge.close()

    # ---- 1) 两台设备各登录一次 ----
    t1 = login_token("sess_user", "SmokeTest123", UA_DESKTOP)
    code, data, _ = call("/api/me/sessions", t1)
    check(code == 200, f"设备列表可读（{code}）")
    items = data.get("items") or []
    check(len(items) == 1, f"登录后有一条设备记录（{len(items)}）")
    check(items and items[0]["current"] is True, "当前登录带「当前设备」标记")
    check("Windows" in (items[0]["device"] if items else ""), f"UA 解析出 Windows（{items[0]['device'] if items else '—'}）")
    check("Chrome" in (items[0]["device"] if items else ""), "UA 解析出 Chrome")
    check(bool(items[0]["ip"] if items else False), "记录了登录 IP")

    t2 = login_token("sess_user", "SmokeTest123", UA_PHONE)
    code, data, _ = call("/api/me/sessions", t2)
    items = data.get("items") or []
    check(len(items) == 2, f"第二次登录后两条记录（{len(items)}）")
    check(items and items[0]["current"] is True, "最新的登录排在最前且是当前设备")
    check("Android" in (items[0]["device"] if items else ""), f"第二台解析为 Android（{items[0]['device'] if items else '—'}）")
    old_sid = items[1]["id"] if len(items) > 1 else ""

    # ---- 2) 下线旧设备：那台的下一次请求立刻 401 ----
    code, data, _ = call(f"/api/me/sessions/{old_sid}", t2, "DELETE")
    check(code == 200, f"下线旧设备（{code}）")
    code, _, _ = call("/api/me", t1)
    check(code == 401, f"被下线的设备下一次请求 401（{code}）")
    code, _, _ = call("/api/me", t2)
    check(code == 200, f"当前设备不受影响（{code}）")

    # ---- 3) 不能用单台下线接口下线当前设备 ----
    code, data, _ = call("/api/me/sessions", t2)
    cur_sid = (data.get("items") or [{}])[0].get("id", "")
    code, data, _ = call(f"/api/me/sessions/{cur_sid}", t2, "DELETE")
    check(code == 400, f"下线当前设备被拒（{code}）")

    # ---- 4) 第三台登录 + 下线其他设备 ----
    t3 = login_token("sess_user", "SmokeTest123", UA_TABLET)
    code, data, _ = call("/api/me/sessions/revoke-others", t3, "POST", {})
    check(code == 200, f"下线其他设备（{code}）")
    check(data.get("revoked", 0) >= 1, f"报告了下线数量（{data.get('revoked')}）")
    code, _, _ = call("/api/me", t2)
    check(code == 401, f"其它设备被下线（{code}）")
    code, _, _ = call("/api/me", t3)
    check(code == 200, f"当前设备还在（{code}）")

    # ---- 5) 退出登录会把当前会话标记下线 ----
    t4 = login_token("sess_user", "SmokeTest123", UA_DESKTOP)
    code, _, _ = call("/api/auth/logout", t4, "POST", {})
    check(code == 200, f"退出登录（{code}）")
    code, _, _ = call("/api/me", t4)
    check(code == 401, f"退出后再用旧 Cookie 401（{code}）")

    # ---- 6) 旧式 token（无 s 声明）继续有效 ----
    legacy = create_token(uid, 1)
    code, _, _ = call("/api/me", legacy)
    check(code == 200, f"旧式 token 仍可用（{code}）")
    code, data, _ = call("/api/me/sessions/revoke-others", legacy, "POST", {})
    check(code == 200, f"旧式 token 也能下线其它设备（{code}）")

    # ---- 7) 管理员重置密码：所有设备被标记下线，列表里不再出现幽灵设备 ----
    # （/me/password 需要图形验证码，进程外脚本过不了；管理员重置走同一条
    #   token_version += 1 + revoke_all 链路，行为等价）
    t5 = login_token("sess_user", "SmokeTest123", UA_DESKTOP)
    t6 = login_token("sess_user", "SmokeTest123", UA_PHONE)
    admin = db_failover.current_sessionmaker()()
    try:
        row = admin.scalar(select(User).where(User.role == 1).order_by(User.id.asc()))
        admin_token = create_token(row.id, row.token_version)
    finally:
        admin.close()
    code, data, _ = call(f"/api/admin/users/{uid}/password", admin_token, "POST", {})
    check(code == 200, f"管理员重置密码（{code}）")
    temp_pwd = data.get("password") or ""
    check(bool(temp_pwd), "返回了临时密码")
    code, _, _ = call("/api/me", t6)
    check(code == 401, f"重置后其它设备 401（{code}）")
    code, _, _ = call("/api/me", t5)
    check(code == 401, f"重置后原设备也 401（{code}）")
    t7 = login_token("sess_user", temp_pwd, UA_TABLET)
    code, data, _ = call("/api/me/sessions", t7)
    items = data.get("items") or []
    check(len(items) == 1 and items[0]["current"], f"设备列表只剩当前这台（{len(items)}）")
    # ---- 收尾 ----
    passed = sum(1 for ok, _ in RESULTS if ok)
    print(f"\n通过 {passed}/{len(RESULTS)}")
    for ok, msg in RESULTS:
        if not ok:
            print(f"  NG {msg}")
    if passed != len(RESULTS):
        sys.exit(1)


if __name__ == "__main__":
    main()
