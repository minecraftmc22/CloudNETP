# -*- coding: utf-8 -*-
"""头像读取权限的回归：普通用户必须能看到别人的头像。

钉住这几件事：
- 登录用户能读任意头像（好友列表、聊天、群成员都要显示对方的头像）
- 未登录一律 401，别把头像变成公开图床
- 文件名不合规 / 文件不存在都是 404，且与调用者身份无关（防路径穿越靠正则）
- 换头像后旧 URL 失效（旧文件被删）、新 URL 可用
- 删除头像后取不到
- 管理员读别人头像仍然正常（修复前只有他能读）

用法（需要一个已启动的隔离实例）：
    python scripts/avatar_smoke.py [port]
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from PIL import Image  # noqa: E402
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


def check(ok: bool, msg: str) -> None:
    RESULTS.append((bool(ok), msg))


def cookie(token: str | None) -> dict[str, str]:
    return {} if not token else {"Cookie": f"{settings.COOKIE_NAME}={token}"}


def call(path: str, token: str | None = None, method: str = "GET", body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, method=method, data=data)
    req.add_header("Content-Type", "application/json")
    for k, v in cookie(token).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read() or b"{}")
        except Exception:
            return err.code, {}


def fetch(path: str, token: str | None = None) -> tuple[int, str, bytes]:
    req = urllib.request.Request(BASE + path, method="GET")
    for k, v in cookie(token).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, res.headers.get("Content-Type", ""), res.read()
    except urllib.error.HTTPError as err:
        return err.code, "", err.read()


def put_image(path: str, token: str, raw: bytes, ctype: str = "image/png"):
    req = urllib.request.Request(BASE + path, method="PUT", data=raw)
    req.add_header("Content-Type", ctype)
    for k, v in cookie(token).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=40) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read() or b"{}")
        except Exception:
            return err.code, {}


def png_bytes(w: int = 320, h: int = 320, color=(230, 120, 160)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def main() -> None:
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    try:
        admin = db.scalar(select(User).where(User.role == 1).order_by(User.id.asc()))
        assert admin, "隔离库里没有管理员账号"

        def ensure_user(name: str) -> User:
            row = db.scalar(select(User).where(User.username == name))
            if row is None:
                row = User(
                    username=name,
                    password_hash=hash_password("SmokeTest123"),
                    role=0,
                    quota_bytes=4 * 1024**3,
                    used_bytes=0,
                    display_name=name,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
            return row

        alice = ensure_user("avatar_alice")
        bob = ensure_user("avatar_bob")
        ta = create_token(alice.id, alice.token_version)
        tb = create_token(bob.id, bob.token_version)
        tn = create_token(admin.id, admin.token_version)
    finally:
        db.close()

    # ---- 1) 上传与自己的头像 ----
    code, data = put_image("/api/me/avatar", ta, png_bytes())
    check(code == 200, f"Alice 上传头像（{code}）")
    url_a = (data.get("user") or {}).get("avatarUrl") or ""
    check(url_a.startswith("/api/avatar/"), f"返回头像地址（{url_a}）")

    code, ctype, raw = fetch(url_a, ta)
    check(code == 200 and ctype.startswith("image/"), f"Alice 读自己的头像（{code} {ctype}）")
    check(len(raw) > 100, "拿到的确实是一张图")

    # ---- 2) 修复点：普通用户能读别人的头像 ----
    code, ctype, raw = fetch(url_a, tb)
    check(code == 200, f"普通用户 Bob 能读 Alice 的头像（{code}）")
    check(ctype.startswith("image/") and len(raw) > 100, "Bob 拿到的是真实图片")

    code, data = call("/api/me", tb)
    check(code == 200, "Bob 自己的资料正常")
    check(not (data.get("user") or {}).get("avatarUrl"), "Bob 没设头像时自己的 avatarUrl 为空")

    # ---- 3) 管理员照旧 ----
    code, ctype, _ = fetch(url_a, tn)
    check(code == 200, f"管理员读别人的头像（{code}）")

    # ---- 4) 未登录不能当图床 ----
    code, _, _ = fetch(url_a, None)
    check(code == 401, f"未登读取头像被拒（{code}）")

    # ---- 5) 非法名与不存在的文件名 ----
    for bad in ("../config.py", "12-zzzzzzzzzzzz.webp", "0-aaaaaaaaaaaa.webp", "1-aaaaaaaaaaaa.svg"):
        code, _, _ = fetch(f"/api/avatar/{bad}", tb)
        check(code in (400, 404), f"非法文件名被拒（{bad} → {code}）")
    code, _, _ = fetch("/api/avatar/999999-aaaaaaaaaaaa.webp", tb)
    check(code == 404, f"不存在的头像 404（{code}）")

    # ---- 6) 换头像：旧 URL 失效 ----
    code, data = put_image("/api/me/avatar", ta, png_bytes(color=(60, 160, 220)))
    check(code == 200, f"Alice 换头像（{code}）")
    url_a2 = (data.get("user") or {}).get("avatarUrl") or ""
    check(url_a2 and url_a2 != url_a, "换头像后地址变了（随机段）")
    code, _, _ = fetch(url_a, tb)
    check(code == 404, f"旧头像地址已失效（{code}）")
    code, _, _ = fetch(url_a2, tb)
    check(code == 200, f"新头像普通用户也能读（{code}）")

    # ---- 7) 删除头像 ----
    code, data = call("/api/me/avatar", ta, "DELETE")
    check(code == 200, f"Alice 删除头像（{code}）")
    code, _, _ = fetch(url_a2, tb)
    check(code == 404, f"删掉后取不到（{code}）")

    # ---- 8) 坏图被拦 ----
    code, data = put_image("/api/me/avatar", tb, b"not-an-image")
    check(code == 400, f"上传非图片被拒（{code}）")

    passed = sum(1 for ok, _ in RESULTS if ok)
    for ok, msg in RESULTS:
        print(("  OK  " if ok else "  NG  ") + msg)
    print(f"\n头像权限：{passed}/{len(RESULTS)}")
    if passed != len(RESULTS):
        sys.exit(1)


if __name__ == "__main__":
    main()
