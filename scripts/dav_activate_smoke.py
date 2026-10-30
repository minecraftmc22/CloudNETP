"""验证「老账号零重登启用 WebDAV」：登录 → 点激活 → Digest 直连。

场景：0.2.7 之前注册的账号没有 dav_ha1，Windows 客户端只走 Digest，
所以要么重新登录一次，要么在设置页输一次密码激活。这里验证后者。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8123"
ACCOUNT = "davact"
SECRET = "DavAct#2026"

passed = 0
failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def request(method: str, path: str, body: dict | None = None, cookie: str | None = None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), resp.headers
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}"), exc.headers
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode("utf-8", "replace")}, exc.headers


def ensure_account() -> None:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import User, utcnow
    from app.security import hash_password
    from app.user_service import ensure_settings

    with SessionLocal() as db:
        user = db.scalars(select(User).where(User.username == ACCOUNT)).first()
        if user is None:
            user = User(
                username=ACCOUNT,
                password_hash=hash_password(SECRET),
                role=0,
                quota_bytes=256 * 1024 * 1024,
                token_version=1,
                created_at=utcnow(),
                last_login_at=utcnow(),
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            ensure_settings(db, user.id)
            db.commit()
        else:
            user.password_hash = hash_password(SECRET)
            user.dav_ha1 = None  # 强制回到「未激活」状态
            user.banned = False
            db.commit()


def clear_ha1() -> None:
    """模拟「登录态还在、但数据库里没有 HA1」的老账号。"""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        user = db.scalars(select(User).where(User.username == ACCOUNT)).first()
        user.dav_ha1 = None
        db.commit()


def digest_probe() -> int:
    """模拟 Windows：只带 Digest，不发 Basic。"""
    url = BASE + "/dav/"
    probe = urllib.request.Request(url, method="PROPFIND")
    probe.add_header("Depth", "0")
    try:
        with urllib.request.urlopen(probe, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        challenges = (exc.headers.get_all("WWW-Authenticate") or [])
    digest = next((c for c in challenges if c.lower().startswith("digest")), "")
    if not digest:
        raise AssertionError("没有 Digest challenge")
    params: dict[str, str] = {}
    for match in re.finditer(r'(\w+)=("[^"]*"|[^,\s]+)', digest[7:]):
        params[match.group(1).lower()] = match.group(2).strip('"')
    uri = "/dav/"
    ha1 = hashlib.md5(f"{ACCOUNT}:{params['realm']}:{SECRET}".encode()).hexdigest()
    ha2 = hashlib.md5(f"PROPFIND:{uri}".encode()).hexdigest()
    nc, cnonce = "00000001", "deadbeef01"
    resp_hash = hashlib.md5(
        f"{ha1}:{params['nonce']}:{nc}:{cnonce}:auth:{ha2}".encode()
    ).hexdigest()
    auth = (
        f'Digest username="{ACCOUNT}", realm="{params["realm"]}", nonce="{params["nonce"]}", '
        f'uri="{uri}", qop=auth, nc={nc}, cnonce="{cnonce}", response="{resp_hash}", algorithm=MD5'
    )
    if params.get("opaque"):
        auth += f', opaque="{params["opaque"]}"'
    final = urllib.request.Request(url, method="PROPFIND")
    final.add_header("Depth", "0")
    final.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(final, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> int:
    ensure_account()

    print("== 1) 未激活时 Digest 应该失败 ==")
    status = digest_probe()
    check("未激活 Digest 401", status == 401, f"got {status}")

    print("== 2) 网页登录（不写 HA1 的老账号）==")
    status, payload, headers = request("POST", "/api/auth/login", {"username": ACCOUNT, "password": SECRET})
    check("登录 200", status == 200, f"got {status}")
    cookie = (headers.get("Set-Cookie") or "").split(";")[0]
    check("拿到会话 cookie", bool(cookie), cookie[:40])
    # 网页登录会自动补 HA1，这里清掉，模拟「早就登录过、但版本升级前没 HA1」的会话
    clear_ha1()
    status, payload, _ = request("GET", "/api/me", cookie=cookie)
    check("davReady 为 false", payload.get("user", {}).get("davReady") is False, str(payload)[:160])

    print("== 3) 调激活接口 ==")
    status, payload, _ = request("POST", "/api/me/dav/activate", {"password": "Wrong#Pass1"}, cookie)
    check("错误密码 400", status == 400, f"got {status}")

    status, _, _ = request("POST", "/api/me/dav/activate", {"password": SECRET}, cookie)
    check("激活成功 200", status == 200, f"got {status}")

    status, payload, _ = request("GET", "/api/me", cookie=cookie)
    check("davReady 变 true", payload.get("user", {}).get("davReady") is True, str(payload)[:120])

    print("== 4) 激活后 Digest 直连 ==")
    status = digest_probe()
    check("Digest 直连 207", status == 207, f"got {status}")

    print("== 5) 改密后旧 HA1 失效 ==")
    status, _, _ = request(
        "PUT", "/api/me/password",
        {"oldPassword": SECRET, "newPassword": SECRET + "x"}, cookie,
    )
    check("改密 200", status == 200, f"got {status}")
    status = digest_probe()
    check("旧密码 Digest 401", status == 401, f"got {status}")

    print(f"\n结果：{passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
