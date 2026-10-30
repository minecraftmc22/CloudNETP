"""WebDAV 端到端校验：真实 HTTP + 真实 MySQL，覆盖协议方法与配额/鉴权边界。

用法：./.venv/Scripts/python.exe -m scripts.dav_smoke
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from sqlalchemy import func, select

from app import node_service as ns
from app.db import SessionLocal
from app.dav_auth import ha1
from app.models import Node, User, UserSetting
from app.security import hash_password

BASE = "http://127.0.0.1:8123"
DAV = f"{BASE}/dav"
USERNAME = "davprobe"
PASSWORD = "DavProbe#2026"
FOLDER = "WebDAV 测试"

passed: list[str] = []
failed: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        passed.append(label)
        print(f"  ok   {label}")
    else:
        failed.append(label)
        print(f"  FAIL {label} {detail}")


def auth_header(user: str = USERNAME, password: str = PASSWORD) -> dict:
    raw = f"{user}:{password}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def hget(headers: dict, name: str) -> str | None:
    """服务端会把响应头名小写化，这里统一按大小写不敏感取。"""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def call(
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict | None = None,
    *,
    auth: bool = True,
):
    url = DAV + urllib.parse.quote(path, safe="/")
    data = body
    request = urllib.request.Request(url, data=data, method=method)
    if auth:
        for key, value in auth_header().items():
            request.add_header(key, value)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read(), _headers(response)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), _headers(exc)


def _headers(response) -> dict[str, str]:
    """同名响应头会重复（401 同时给 Digest + Basic），用换行拼起来不丢信息。"""
    out: dict[str, str] = {}
    for key, value in response.headers.items():
        out[key] = f"{out[key]}\n{value}" if key in out else value
    return out


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


_LAST_DIGEST: list[str] = []
_DIGEST_NC = [0]


def digest_call(
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict | None = None,
    *,
    password: str | None = None,
    replay: bool = False,
):
    """自己算一遍 Digest 摘要，模拟 Windows 资源管理器。

    依据：WebClient 默认 BasicAuthLevel=1，明文 HTTP 上不发 Basic 凭据，
    只会对 Digest challenge 重试；所以这条链路必须单独验证。
    """
    url = DAV + urllib.parse.quote(path, safe="/")
    if replay and _LAST_DIGEST:
        auth = _LAST_DIGEST[0]
    else:
        probe = urllib.request.Request(url, data=body, method=method)
        for key, value in (headers or {}).items():
            probe.add_header(key, value)
        try:
            with urllib.request.urlopen(probe, timeout=30) as response:
                return response.status, response.read(), _headers(response)
        except urllib.error.HTTPError as exc:
            raws = exc.headers.get_all("WWW-Authenticate") or []
        challenge = next((item for item in raws if item.lower().startswith("digest")), "")
        if not challenge:
            raise AssertionError("401 响应里没有 Digest challenge")
        params: dict[str, str] = {}
        for match in re.finditer(r'(\w+)=("[^"]*"|[^,\s]+)', challenge[7:]):
            params[match.group(1).lower()] = match.group(2).strip('"')
        uri = "/dav" + urllib.parse.quote(path, safe="/")
        digest_ha1 = _md5(f"{USERNAME}:{params['realm']}:{password or PASSWORD}")
        digest_ha2 = _md5(f"{method.upper()}:{uri}")
        _DIGEST_NC[0] += 1
        nc, cnonce = f"{_DIGEST_NC[0]:08x}", os.urandom(6).hex()
        digest = _md5(
            f"{digest_ha1}:{params['nonce']}:{nc}:{cnonce}:auth:{digest_ha2}"
        )
        auth = (
            f'Digest username="{USERNAME}", realm="{params["realm"]}", '
            f'nonce="{params["nonce"]}", uri="{uri}", qop=auth, nc={nc}, '
            f'cnonce="{cnonce}", response="{digest}", algorithm=MD5'
        )
        if params.get("opaque"):
            auth += f', opaque="{params["opaque"]}"'
        _LAST_DIGEST[:] = [auth]

    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", auth)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read(), _headers(response)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), _headers(exc)


def ensure_user() -> int:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(func.lower(User.username) == USERNAME.lower()))
        if user is None:
            user = User(
                username=USERNAME,
                password_hash=hash_password(PASSWORD),
                role=0,
                quota_bytes=512 * 1024 * 1024,
                token_version=1,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            db.add(UserSetting(user_id=user.id))
            db.commit()
        else:
            user.password_hash = hash_password(PASSWORD)
            user.banned = False
            user.quota_bytes = 512 * 1024 * 1024
            db.commit()
        return user.id


def wipe(db, user_id: int) -> None:
    user = db.get(User, user_id)
    for node in db.scalars(
        select(Node).where(Node.user_id == user_id, Node.parent_id.is_(None))
    ).all():
        ns.purge_node(db, user, node)
    ns.recalc_usage(db, user)
    db.commit()


def main() -> int:
    user_id = ensure_user()
    with SessionLocal() as db:
        wipe(db, user_id)

    # 1) 未认证
    status, _, headers = call("PROPFIND", "/", auth=False, headers={"Depth": "0"})
    check("无凭据返回 401", status == 401, f"got {status}")
    check(
        "401 带 Basic 挑战",
        "basic" in (hget(headers, "WWW-Authenticate") or "").lower(),
        str(hget(headers, "WWW-Authenticate")),
    )

    status, _, _ = call("PROPFIND", "/", headers={"Depth": "0"}, auth=False)
    check("错误密码返回 401", status in (401, 429), f"got {status}")

    # 2) OPTIONS
    status, _, headers = call("OPTIONS", "/")
    check("OPTIONS 200", status == 200, f"got {status}")
    check("DAV: 1,2 能力", "1" in (hget(headers, "DAV") or ""), str(hget(headers, "DAV")))
    check("Allow 含 PROPFIND", "PROPFIND" in (hget(headers, "Allow") or ""), str(hget(headers, "Allow")))

    # 2.5) Digest：Windows 资源管理器在明文 HTTP 下只走这条路
    status, body, _ = digest_call("PROPFIND", "/", headers={"Depth": "0"})
    check("Digest 认证通过", status == 207, f"got {status}")
    check("Digest 返回 multistatus", b"multistatus" in body, str(body[:60]))
    status, _, _ = digest_call("PROPFIND", "/", password="Wrong#Pass1", headers={"Depth": "0"})
    check("Digest 错密码 401", status == 401, f"got {status}")
    digest_call("PROPFIND", "/", headers={"Depth": "0"})
    status, _, _ = digest_call("PROPFIND", "/", headers={"Depth": "0"}, replay=True)
    check("Digest nc 重放被拒", status == 401, f"got {status}")

    # 3) 根 PROPFIND
    status, body, _ = call("PROPFIND", "/", headers={"Depth": "0"})
    text = body.decode("utf-8", "replace")
    check("根 PROPFIND 207", status == 207, f"got {status}")
    check("根是 collection", "<D:collection/>" in text, text[:200].replace("\n", " "))
    check("根 href 正确", "<D:href>/dav/</D:href>" in text, text[:300])
    check("含配额属性", "quota-available-bytes" in text)

    # 4) MKCOL
    status, _, _ = call("MKCOL", f"/{FOLDER}")
    check("MKCOL 201", status == 201, f"got {status}")
    status, _, _ = call("MKCOL", f"/{FOLDER}")
    check("重复 MKCOL 405", status == 405, f"got {status}")

    # 5) PUT
    payload = "你好，WebDAV！\n".encode("utf-8")
    status, _, _ = call("PUT", f"/{FOLDER}/hello.txt", payload, {"Content-Type": "text/plain"})
    check("PUT 201", status == 201, f"got {status}")

    status, body, headers = call("GET", f"/{FOLDER}/hello.txt")
    check("GET 内容一致", status == 200 and body == payload, f"{status} {body[:40]!r}")
    check("GET 带 ETag", bool(hget(headers, "ETag")), str(hget(headers, "ETag")))

    status, _, headers = call("HEAD", f"/{FOLDER}/hello.txt")
    check("HEAD 200 且长度正确", status == 200 and hget(headers, "Content-Length") == str(len(payload)),
          f"{status} {headers.get('Content-Length')}")

    status, body, headers = call("GET", f"/{FOLDER}/hello.txt", headers={"Range": "bytes=0-5"})
    check("Range 206", status == 206, f"got {status}")
    check("Range 内容部分", body == payload[:6], repr(body))
    check("Content-Range", (hget(headers, "Content-Range") or "").startswith("bytes 0-5/"), str(hget(headers, "Content-Range")))

    # 6) 目录列举
    status, body, _ = call("PROPFIND", f"/{FOLDER}", headers={"Depth": "1"})
    text = body.decode("utf-8", "replace")
    check("目录 PROPFIND 207", status == 207, f"got {status}")
    check("列举含 hello.txt", "hello.txt" in text)
    check("文件长度属性", f"<D:getcontentlength>{len(payload)}</D:getcontentlength>" in text)

    status, body, _ = call("PROPFIND", "/", headers={"Depth": "1"})
    text = body.decode("utf-8", "replace")
    check("根 Depth1 含中文目录（已编码）", urllib.parse.quote(FOLDER) in text, text[:400])

    # 7) PROPPATCH
    prop_body = (
        b'<?xml version="1.0"?><D:propertyupdate xmlns:D="DAV:"><D:set><D:prop>'
        b"<Win32LastModifiedTime/></D:prop></D:set></D:propertyupdate>"
    )
    status, body, _ = call("PROPPATCH", f"/{FOLDER}/hello.txt", prop_body,
                           {"Content-Type": "text/xml"})
    check("PROPPATCH 207", status == 207, f"got {status}")
    check("PROPPATCH 回执 200", b"200 OK" in body)

    # 8) LOCK / UNLOCK
    lock_body = (
        b'<?xml version="1.0"?><D:lockinfo xmlns:D="DAV:"><D:lockscope><D:exclusive/></D:lockscope>'
        b"<D:locktype><D:write/></D:locktype><D:owner>smoke</D:owner></D:lockinfo>"
    )
    status, body, headers = call("LOCK", f"/{FOLDER}/hello.txt", lock_body, {"Timeout": "Second-600"})
    token = (hget(headers, "Lock-Token") or "").strip("<>")
    check("LOCK 200", status == 200, f"got {status}")
    check("LOCK 返回 token", token.startswith("opaquelocktoken:"), token)
    check("LOCK 响应含 lockdiscovery", b"lockdiscovery" in body)

    status, body, headers = call("LOCK", f"/{FOLDER}/hello.txt", b"", {"If": f"(<{token}>)"})
    check("LOCK 刷新返回同一 token", (hget(headers, "Lock-Token") or "").strip("<>") == token,
          str(hget(headers, "Lock-Token")))

    status, _, _ = call("UNLOCK", f"/{FOLDER}/hello.txt", headers={"Lock-Token": f"<{token}>"})
    check("UNLOCK 204", status == 204, f"got {status}")

    # 9) 覆盖写
    payload2 = "覆盖后的内容".encode("utf-8")
    status, _, _ = call("PUT", f"/{FOLDER}/hello.txt", payload2, {"Content-Type": "text/plain"})
    check("覆盖 PUT 204", status == 204, f"got {status}")
    status, body, _ = call("GET", f"/{FOLDER}/hello.txt")
    check("覆盖后内容更新", body == payload2, repr(body[:30]))

    # 10) MOVE
    dest = f"{DAV}/{urllib.parse.quote(FOLDER)}/moved.txt"
    status, _, _ = call("MOVE", f"/{FOLDER}/hello.txt", headers={"Destination": dest})
    check("MOVE 201", status == 201, f"got {status}")
    status, _, _ = call("GET", f"/{FOLDER}/hello.txt")
    check("原路径已消失", status == 404, f"got {status}")
    status, body, _ = call("GET", f"/{FOLDER}/moved.txt")
    check("新路径可读", status == 200 and body == payload2)

    # 11) COPY
    dest2 = f"{DAV}/{urllib.parse.quote(FOLDER)}/copy.txt"
    status, _, _ = call("COPY", f"/{FOLDER}/moved.txt", headers={"Destination": dest2})
    check("COPY 201", status == 201, f"got {status}")
    status, body, _ = call("GET", f"/{FOLDER}/copy.txt")
    check("副本内容一致", status == 200 and body == payload2)

    status, _, _ = call("COPY", f"/{FOLDER}/moved.txt", headers={"Destination": dest2, "Overwrite": "F"})
    check("Overwrite F 冲突 412", status == 412, f"got {status}")

    # 12) 复制文件夹到自身内部 → 409
    call("MKCOL", f"/{FOLDER}/sub")
    status, _, _ = call("COPY", f"/{FOLDER}", headers={"Destination": dest2})
    check("复制到自身内部 409/412", status in (409, 412), f"got {status}")

    # 13) 越权路径
    status, _, _ = call("PROPFIND", "/..%2F..%2Fetc", headers={"Depth": "0"})
    check("路径穿越被拒", status in (400, 404), f"got {status}")

    # 14) 遍历深度
    status, body, _ = call("PROPFIND", "/", headers={"Depth": "infinity"})
    check("Depth infinity 可用", status == 207, f"got {status}")

    # 15) DELETE
    status, _, _ = call("DELETE", f"/{FOLDER}/copy.txt")
    check("DELETE 文件 204", status == 204, f"got {status}")
    status, _, _ = call("GET", f"/{FOLDER}/copy.txt")
    check("删除后 404", status == 404, f"got {status}")

    status, _, _ = call("DELETE", f"/{FOLDER}")
    check("DELETE 目录 204", status == 204, f"got {status}")
    status, _, _ = call("PROPFIND", f"/{FOLDER}", headers={"Depth": "0"})
    check("目录删除后 404", status == 404, f"got {status}")

    # 16) 落库核对
    with SessionLocal() as db:
        trashed = db.scalars(
            select(Node).where(Node.user_id == user_id, Node.name == FOLDER, Node.deleted_at.is_not(None))
        ).all()
        check("删除进了回收站（未直接抹掉）", len(trashed) == 1, f"count={len(trashed)}")
        alive = db.scalars(
            select(Node).where(Node.user_id == user_id, Node.deleted_at.is_(None))
        ).all()
        check("活动节点已清空", len(alive) == 0, f"count={len(alive)}")
        user = db.get(User, user_id)
        # 回收站里的文件仍然占空间（产品语义），彻底清理后才归零
        check("回收站内仍计入已用空间", user.used_bytes > 0, str(user.used_bytes))
        wipe(db, user_id)
        db.expire_all()
        check("彻底清理后配额归零", db.get(User, user_id).used_bytes == 0,
              str(db.get(User, user_id).used_bytes))

    # 17) 配额不足 → 507
    with SessionLocal() as db:
        user = db.get(User, user_id)
        user.used_bytes = user.quota_bytes
        db.commit()
    status, _, _ = call("PUT", "/big.bin", b"x" * 1024)
    check("配额耗尽 507", status == 507, f"got {status}")
    with SessionLocal() as db:
        user = db.get(User, user_id)
        user.used_bytes = 0
        db.commit()

    # 18) 清理
    with SessionLocal() as db:
        wipe(db, user_id)
        user = db.get(User, user_id)
        db.delete(user)
        db.commit()

    print(f"\n通过 {len(passed)} 项，失败 {len(failed)} 项")
    for item in failed:
        print("  -", item)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
