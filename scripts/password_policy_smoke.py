# -*- coding: utf-8 -*-
"""重置密码的密码规则冒烟。

两层：
  1. 纯函数：password_problem 的每条规则 + 管理员临时密码必须自身合规
  2. 真实接口：临时账号走「用户名 + 绑定邮箱」拿令牌，弱密码应 400，合规密码应 200，
     重置后新密码能登录、旧令牌立即失效；跑完删掉临时账号

用法：.venv/Scripts/python.exe scripts/password_policy_smoke.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from app import db_failover  # noqa: E402
from app.models import User  # noqa: E402
from app.routers.admin import _temp_password  # noqa: E402
from app.security import hash_password, password_problem  # noqa: E402

BASE = "http://127.0.0.1:8000"
passed = 0
failed = 0


def check(name: str, ok: bool, detail: object = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def post(path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST")
    req.add_header("Origin", BASE)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception:
            return exc.code, {}


print("== 规则函数 ==")
cases = [
    ("Abc12345", None),
    ("abcdefgh", "密码需要包含数字"),
    ("12345678", "密码需要包含小写字母"),
    ("abc 12345", "密码只能用键盘上的英文、数字和符号，不支持中文、空格和 emoji"),
    ("abc12345密码", "密码只能用键盘上的英文、数字和符号，不支持中文、空格和 emoji"),
    (" abc12345", "密码首尾不能是空格"),
    ("abc12345 ", "密码首尾不能是空格"),
    ("abc1234", "密码至少 8 位"),
    ("a1" + "x" * 127, "密码太长（上限 128 位）"),
]
for password, expect in cases:
    got = password_problem(password)
    check(f"{password[:14]!r} -> {expect or '合规'}", got == expect, got)

print("== 管理员临时密码 ==")
bad = [p for p in (_temp_password() for _ in range(300)) if password_problem(p)]
check("300 个临时密码全部满足规则", not bad, bad[:3])
check("临时密码长度 12", all(len(_temp_password()) == 12 for _ in range(20)))

print("== 真实接口 ==")
if not db_failover._started:
    db_failover.startup()
db = db_failover.current_sessionmaker()()
name = f"pws{int(time.time()) % 10000000}"
email = f"{name}@example.com"
user = User(username=name, password_hash=hash_password("oldpass123"), email=email, token_version=1)
db.add(user)
db.commit()
uid = user.id
db.close()

try:
    status, data = post("/api/auth/forgot/email", {"account": name, "email": email})
    token = data.get("token", "")
    check("拿到重置令牌", status == 200 and bool(token), data)

    weak = [
        ("abcdefgh", "密码需要包含数字"),
        ("12345678", "密码需要包含小写字母"),
        ("abc 12345", "密码只能用键盘上的英文、数字和符号，不支持中文、空格和 emoji"),
    ]
    for password, expect in weak:
        status, data = post("/api/auth/reset", {"token": token, "password": password})
        check(f"拒绝 {password!r}", status == 400 and data.get("detail") == expect, (status, data))

    status, data = post("/api/auth/reset", {"token": token, "password": "NewPass123"})
    check("合规密码重置成功", status == 200 and data.get("username") == name, (status, data))

    status, data = post("/api/auth/reset", {"token": token, "password": "Another123"})
    check("旧令牌立即失效", status == 400 and "无效" in str(data.get("detail")), (status, data))

    status, data = post("/api/auth/login", {"username": name, "password": "NewPass123"})
    check("新密码可以登录", status == 200 and bool(data.get("user")), (status, data))
finally:
    db = db_failover.current_sessionmaker()()
    row = db.get(User, uid)
    if row:
        db.delete(row)
        db.commit()
    db.close()
    print("  (临时账号已删除)")

print(f"\n通过 {passed} / {passed + failed}")
sys.exit(0 if failed == 0 else 1)
