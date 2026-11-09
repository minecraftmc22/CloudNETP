"""路径式入口的重定向冒烟（不起浏览器）。

前端是 hash 路由，但地址栏可能被写成 /admin 或 /admin#/admin（旧书签、外部链接）。
这里验证这些路径会 302 到 /#/<视图>，而不是 404；同时确认
/terms、/api/version、/static/* 这些真实路径没有被兜底规则吃掉。

跑法：
    ./.venv/Scripts/python.exe scripts/entry_route_smoke.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-entry-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'smoke.db').as_posix()}"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ["SECRET_KEY"] = "smoke-secret-key-for-entry"
os.environ["DB_FALLBACK_ENABLED"] = "0"
os.environ["DB_PROBE_INTERVAL"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}  ({detail})")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


_opener = urllib.request.build_opener(NoRedirect)


def get(base: str, path: str) -> tuple[int, dict, bytes]:
    """不跟随重定向，返回 (状态码, 响应头, body)。"""
    try:
        with _opener.open(base + path, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> None:
    import uvicorn

    from app.main import app as fastapi_app

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            fastapi_app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if server.started:
            break
        time.sleep(0.2)
    if not server.started:
        raise SystemExit("服务没起来")

    try:
        print("\nA. 路径式入口 → hash 路由")
        for view in ("admin", "files", "settings", "shares", "trash", "about", "feedback"):
            code, headers, _ = get(base, f"/{view}")
            location = headers.get("Location", "")
            check(
                f"/{view} 302 到 /#/{view}",
                code == 302 and location == f"/#/{view}",
                f"{code} {location}",
            )

        # Location 自带 fragment：浏览器地址栏写 /admin#/admin 时，fragment 不发给服务端，
        # 跳转后就是干净的 /#/admin（不会拼成 /#/admin#/admin）
        code, headers, _ = get(base, "/admin")
        check("Location 带 fragment，浏览器会落在 /#/admin", headers.get("Location") == "/#/admin", headers.get("Location"))
        check("是临时重定向（不会被浏览器长期缓存）", code == 302, code)

        print("\nB. 真实路径没有被兜底吃掉")
        code, headers, body = get(base, "/")
        check("根路径 200 且是 HTML", code == 200 and "text/html" in headers.get("Content-Type", ""), code)
        check("根页面有应用挂载点", b'id="shell"' in body and b'id="auth-root"' in body, body[:60])

        code, headers, _ = get(base, "/terms")
        check("/terms 仍是协议页", code == 200 and "text/html" in headers.get("Content-Type", ""), code)

        code, _, body = get(base, "/api/version")
        payload = json.loads(body or b"{}")
        check("/api/version 正常", code == 200 and payload.get("version"), f"{code} {payload}")

        code, _, _ = get(base, "/static/js/app.js")
        check("/static 静态资源正常", code == 200, code)

        print("\nC. 未知路径仍然是 404")
        code, _, body = get(base, "/nope")
        payload = json.loads(body or b"{}")
        check("未知单段路径 404", code == 404 and payload.get("code") == 404, f"{code} {payload}")
        code, _, _ = get(base, "/api/nope")
        check("未知接口 404", code == 404, code)
    finally:
        server.should_exit = True

    print(f"\n{'=' * 56}")
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for name in FAILED:
        print("  FAILED:", name)
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
