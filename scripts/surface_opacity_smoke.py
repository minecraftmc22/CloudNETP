# -*- coding: utf-8 -*-
"""模块不透明度冒烟：保存 / 读取 / 夹取范围 / 白名单 / 清空还原。

用法：python scripts/surface_opacity_smoke.py [port]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from app import db_failover  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import User, UserSetting  # noqa: E402
from app.security import create_token  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
BASE = f"http://127.0.0.1:{PORT}"

ok_count = 0
fail_count = 0


def check(label, cond, extra=""):
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  OK   {label} {extra}")
    else:
        fail_count += 1
        print(f"  FAIL {label} {extra}")


def http(method, path, body=None, cookie=""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Origin", BASE)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {}


def main():
    db_failover.startup()
    db = db_failover.current_sessionmaker()()
    admin = db.query(User).filter(User.role == 1).order_by(User.id).first()
    cookie = f"{settings.COOKIE_NAME}={create_token(admin.id, admin.token_version)}"
    row = db.get(UserSetting, admin.id)
    backup = dict(row.surface_opacity or {}) if row else {}
    db.close()

    print("== 初始 ==")
    st, me = http("GET", "/api/me", cookie=cookie)
    check("GET /api/me 200", st == 200, st)
    check("下发 surfaceOpacity 字段", "surfaceOpacity" in me.get("user", {}).get("settings", {}), "")

    print("== 保存四个模块 ==")
    payload = {"nav": 0, "titlebar": 100, "panel": 35, "dialog": 85}
    st, d = http("PATCH", "/api/me/settings", {"surfaceOpacity": payload}, cookie=cookie)
    check("PATCH 200", st == 200, st)
    got = d.get("settings", {}).get("surfaceOpacity", {})
    check("四项都存住", got == payload, got)

    st, me = http("GET", "/api/me", cookie=cookie)
    got2 = me.get("user", {}).get("settings", {}).get("surfaceOpacity", {})
    check("重新读取一致", got2 == payload, got2)

    print("== 越界夹取 ==")
    st, d = http(
        "PATCH",
        "/api/me/settings",
        {"surfaceOpacity": {"panel": 250, "dialog": -20, "nav": 33.7}},
        cookie=cookie,
    )
    got = d.get("settings", {}).get("surfaceOpacity", {})
    check("超上限夹到 100", got.get("panel") == 100, got.get("panel"))
    check("低于 0 夹到 0", got.get("dialog") == 0, got.get("dialog"))
    check("小数四舍五入", got.get("nav") == 34, got.get("nav"))

    print("== 白名单 ==")
    st, d = http(
        "PATCH",
        "/api/me/settings",
        {"surfaceOpacity": {"nav": 40, "evil": 90, "titlebar": "abc"}},
        cookie=cookie,
    )
    got = d.get("settings", {}).get("surfaceOpacity", {})
    check("未知键被丢弃", "evil" not in got, got)
    check("非数字被丢弃", "titlebar" not in got, got)
    check("合法键保留", got.get("nav") == 40, got)

    print("== 清空 ==")
    st, d = http("PATCH", "/api/me/settings", {"surfaceOpacity": {}}, cookie=cookie)
    got = d.get("settings", {}).get("surfaceOpacity", {})
    check("{} 表示恢复默认", got == {}, got)

    print("== 未登录 ==")
    st, _ = http("PATCH", "/api/me/settings", {"surfaceOpacity": {"nav": 10}})
    check("未登录 401", st == 401, st)

    print("== 还原 ==")
    db = db_failover.current_sessionmaker()()
    row = db.get(UserSetting, admin.id)
    if row:
        row.surface_opacity = backup
        db.commit()
    db.close()
    print("  恢复为", backup)

    print(f"\n通过 {ok_count} 项，失败 {fail_count} 项")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
