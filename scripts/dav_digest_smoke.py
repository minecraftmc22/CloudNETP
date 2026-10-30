"""WebDAV Digest 链路验证。

背景：Windows 的 WebClient 默认 BasicAuthLevel=1，明文 HTTP 上不会发送 Basic
凭据，收到 401 + Basic challenge 就直接报 0x80070043（找不到网络名）。
服务端因此同时下发 Digest（排前面）+ Basic（兜底），本脚本验证 Digest 链路。

跑法：先启动服务，然后 ./.venv/Scripts/python.exe -m scripts.dav_digest_smoke
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8123"
DAV = BASE + "/dav"
ACCOUNT = "davwin"
SECRET = "DavWin#2026"

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


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def headers_of(response) -> dict[str, str]:
    # Starlette 下发的头名是全小写，统一小写键、同名用换行拼起来
    out: dict[str, str] = {}
    for key, value in response.headers.items():
        low = key.lower()
        out[low] = f"{out[low]}\n{value}" if low in out else value
    return out


def raw_call(method: str, path: str, auth: str | None = None, extra: dict | None = None):
    url = DAV + urllib.parse.quote(path, safe="/")
    request = urllib.request.Request(url, method=method)
    if auth:
        request.add_header("Authorization", auth)
    for key, value in (extra or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(), headers_of(response)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), headers_of(exc)


def basic_auth(user: str, secret: str) -> str:
    import base64

    token = base64.b64encode(f"{user}:{secret}".encode()).decode()
    return f"Basic {token}"


_NC = [0]


def build_digest(method: str, path: str, secret: str = SECRET) -> str:
    """每次调用递增 nc（真实客户端也这么做，qop=auth 要求 nc 严格递增）。"""
    """先探一次拿 challenge，再自己算摘要（模拟客户端的真实流程）。"""
    status, _, headers = raw_call(method, path)
    challenges = (headers.get("www-authenticate") or "").split("\n")
    challenge = next((c for c in challenges if c.lower().startswith("digest")), "")
    if not challenge:
        raise AssertionError(f"没有 Digest challenge，status={status}")
    params: dict[str, str] = {}
    for match in re.finditer(r'(\w+)=("[^"]*"|[^,\s]+)', challenge[7:]):
        params[match.group(1).lower()] = match.group(2).strip('"')
    uri = "/dav" + urllib.parse.quote(path, safe="/")
    ha1 = md5(f"{ACCOUNT}:{params['realm']}:{secret}")
    ha2 = md5(f"{method.upper()}:{uri}")
    _NC[0] += 1
    nc = f"{_NC[0]:08x}"
    cnonce = os.urandom(6).hex()
    response = md5(f"{ha1}:{params['nonce']}:{nc}:{cnonce}:auth:{ha2}")
    header = (
        f'Digest username="{ACCOUNT}", realm="{params["realm"]}", '
        f'nonce="{params["nonce"]}", uri="{uri}", qop=auth, nc={nc}, '
        f'cnonce="{cnonce}", response="{response}", algorithm=MD5'
    )
    if params.get("opaque"):
        header += f', opaque="{params["opaque"]}"'
    return header


def main() -> int:
    print("== 1) 401 同时给两个 challenge ==")
    status, _, headers = raw_call("PROPFIND", "/", extra={"Depth": "0"})
    joined = headers.get("www-authenticate") or ""
    check("未认证返回 401", status == 401, f"got {status}")
    check("含 Digest 挑战", "digest" in joined.lower(), joined[:80])
    check("含 Basic 兜底", "basic" in joined.lower(), joined[:80])
    check("Digest 声明 qop=auth", 'qop="auth"' in joined.lower() or "qop=auth" in joined.lower(), joined[:140])
    check("Digest 带 algo MD5", "algorithm=md5" in joined.lower().replace(" ", ""), joined[:120])

    print("== 2) Basic 登录顺带播种 HA1 ==")
    status, _, _ = raw_call("PROPFIND", "/", auth=basic_auth(ACCOUNT, SECRET), extra={"Depth": "0"})
    check("Basic 认证通过", status == 207, f"got {status}")

    print("== 3) Digest 全链路 ==")
    auth = build_digest("PROPFIND", "/")
    status, body, _ = raw_call("PROPFIND", "/", auth=auth, extra={"Depth": "0"})
    check("Digest 认证通过", status == 207, f"got {status}")
    check("返回 multistatus", b"multistatus" in body, str(body[:80]))

    status, _, _ = raw_call(
        "PROPFIND", "/", auth=build_digest("PROPFIND", "/", secret="Wrong#Pass1"),
        extra={"Depth": "0"},
    )
    check("Digest 错密码 401", status == 401, f"got {status}")

    status, _, _ = raw_call("PROPFIND", "/", auth=auth, extra={"Depth": "0"})
    check("Digest nc 重放被拒", status == 401, f"got {status}")

    print("== 4) Digest 覆盖写操作 ==")
    auth = build_digest("MKCOL", "/digest-probe")
    status, _, _ = raw_call("MKCOL", "/digest-probe", auth=auth)
    check("MKCOL 201", status in (201, 405), f"got {status}")

    auth = build_digest("PUT", "/digest-probe/hello.txt")
    request = urllib.request.Request(
        DAV + "/digest-probe/hello.txt", data=b"hello digest", method="PUT"
    )
    request.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    check("PUT 成功", status in (201, 204), f"got {status}")

    auth = build_digest("GET", "/digest-probe/hello.txt")
    status, body, _ = raw_call("GET", "/digest-probe/hello.txt", auth=auth)
    check("GET 回读内容", status == 200 and body == b"hello digest", f"got {status} {body[:30]}")

    auth = build_digest("DELETE", "/digest-probe/hello.txt")
    status, _, _ = raw_call("DELETE", "/digest-probe/hello.txt", auth=auth)
    check("DELETE 204", status == 204, f"got {status}")

    auth = build_digest("DELETE", "/digest-probe")
    status, _, _ = raw_call("DELETE", "/digest-probe", auth=auth)
    check("删除目录 204", status == 204, f"got {status}")

    print(f"\n结果：{passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
