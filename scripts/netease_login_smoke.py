# -*- coding: utf-8 -*-
"""网易云扫码登录（后台上）离线冒烟。

起一个假网易云，验证整套「服务端拿 Cookie」的链路：
  1. 二维码申请 → 会话/地址；二维码能画成 SVG
  2. 轮询 801 → 802 → 803；803 时只挑 MUSIC_U / __csrf，其余 Set-Cookie 丢掉
  3. Cookie 加密落库（库里不是明文），内存里立刻生效（取歌词不用查库）
  4. status() 只回昵称/头像/时间，**响应里不含 Cookie 原文或名字**
  5. 退出登录：清库 + 调远端 logout；没有后台 Cookie 时回退到 .env
  6. 启动 hydrate 能从库里恢复；会话过期/失效的错误码

跑法：
    ./.venv/Scripts/python.exe scripts/netease_login_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-ng-login-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'smoke.db').as_posix()}"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ["SECRET_KEY"] = "smoke-secret-key-for-netease-login"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import netease_auth as ng  # noqa: E402
from app.config import settings  # noqa: E402
from app.settings_store import get_value  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def _err_code(fn, *args) -> int:
    """调用预期失败的函数，返回 NeteaseError.status（不是 NeteaseError 则返回 -1）。"""
    try:
        fn(*args)
    except ng.NeteaseError as exc:
        return exc.status
    except Exception:
        return -1
    return 0


def check(name: str, ok: bool, detail: object = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}  ({detail})")


# --------------------------------------------------------------------------- #
# 假网易云
# --------------------------------------------------------------------------- #
SECRET = "MUSIC_U=smoke-token-123456; Path=/; Domain=.music.163.com; HttpOnly"
CSRF = "__csrf=csrf-abc; Path=/; Domain=.music.163.com"
NOISE = "NMTID=noise-should-be-dropped; Path=/"


class Fake(BaseHTTPRequestHandler):
    polls = 0
    logout_hits: list[str] = []
    account_headers: list[dict] = []

    def log_message(self, *_args):
        pass

    def _send(self, payload: dict, cookies: list[str] | None = None):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for raw in cookies or []:
            self.send_header("Set-Cookie", raw)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(path.query)
        cookie = self.headers.get("Cookie") or ""

        if path.path == "/api/login/qrcode/unikey":
            return self._send({"code": 200, "unikey": "uni-777"})

        if path.path == "/api/login/qrcode/client/login":
            if qs.get("key", [""])[0] != "uni-777":
                return self._send({"code": 800, "message": "二维码不存在或已过期"})
            Fake.polls += 1
            if Fake.polls == 1:
                return self._send({"code": 801})
            if Fake.polls == 2:
                return self._send({"code": 802, "nickname": "半仙"})
            return self._send({"code": 803, "nickname": "半仙"}, [SECRET, CSRF, NOISE])

        if path.path == "/api/nuser/account/get":
            Fake.account_headers.append(dict(self.headers))
            if "MUSIC_U=smoke-token-123456" in cookie:
                return self._send(
                    {
                        "code": 200,
                        "account": {"id": 42},
                        "profile": {
                            "userId": 42,
                            "nickname": "半仙",
                            "avatarUrl": "http://p1.music.126.net/a.jpg",
                            "vipType": 11,
                        },
                    }
                )
            return self._send({"code": 301})

        if path.path == "/api/logout":
            Fake.logout_hits.append(cookie)
            return self._send({"code": 200})

        return self._send({"code": 404})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
ng.API_BASE = f"http://127.0.0.1:{srv.server_address[1]}"

# --------------------------------------------------------------------------- #
# 临时库
# --------------------------------------------------------------------------- #
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import db_failover  # noqa: E402
from app.models import Base  # noqa: E402

engine = create_engine(f"sqlite:///{(_TMP / 'smoke.db').as_posix()}")
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, expire_on_commit=False)
db = Session()

# hydrate 走的是容灾模块的 sessionmaker，测试里指到临时库
_orig_sessionmaker = db_failover.current_sessionmaker
db_failover.current_sessionmaker = lambda: Session  # type: ignore[assignment]

_orig_cookie = settings.NETEASE_COOKIE
settings.NETEASE_COOKIE = ""

try:
    print("\nA. 二维码")
    info = ng.qr_create()
    check("拿到会话 id", bool(info.get("session")), info)
    check("二维码地址带 key", "codekey=uni-777" in info.get("qrUrl", ""), info.get("qrUrl"))
    check("有效期 180s", info.get("expiresIn") == 180, info.get("expiresIn"))
    svg = ng.qr_svg(info["session"])
    check("SVG 是白底深码", svg.startswith("<svg") and "#ffffff" in svg and "#111111" in svg, svg[:60])
    check("SVG 有尺寸", "viewBox=" in svg and 'width="' in svg, svg[:80])

    print("\nB. 轮询与落库")
    sid = info["session"]
    check("第一次是待扫码", ng.qr_poll(sid)["code"] == 801)
    second = ng.qr_poll(sid)
    check("第二次已扫码并带昵称", second["code"] == 802 and second["nickname"] == "半仙", second)
    third = ng.qr_poll(sid)
    check("第三次登录成功", third["code"] == 803, third.get("code"))
    cookie = third.get("cookie", "")
    check("只留 MUSIC_U 与 __csrf", cookie == "MUSIC_U=smoke-token-123456; __csrf=csrf-abc", cookie)
    check("丢弃无关 Cookie", "NMTID" not in cookie, cookie)
    check("803 之后会话失效", _err_code(ng.qr_poll, sid) == 404)

    ng.save_cookie(db, cookie, {"nickname": "半仙", "userId": 42})
    stored = get_value(db, ng.STORE_COOKIE)
    check("库里是密文", stored.startswith("enc:"), stored[:12])
    check("库里没有明文 token", "smoke-token-123456" not in stored, stored[:24])
    check("取歌词用内存里的 Cookie", ng.current_cookie() == cookie, ng.current_cookie())

    print("\nC. 状态（不能泄露 Cookie）")
    st = ng.status(db)
    payload = json.dumps(st, ensure_ascii=False)
    check("已连接", st["connected"] is True, st)
    check("来源是后台", st["source"] == "admin", st["source"])
    check("昵称来自网易云", st["nickname"] == "半仙", st["nickname"])
    check("头像/VIP 带上", st["avatarUrl"].endswith("a.jpg") and st["vipType"] == 11, st)
    check("更新时间有值", bool(st["updatedAt"]), st["updatedAt"])
    check("响应里没有 Cookie 原文", "smoke-token-123456" not in payload, payload)
    check("响应里没有 Cookie 名字", "MUSIC_U" not in payload and "__csrf" not in payload, payload)
    check("请求上游时带上了 Cookie", any(
        "MUSIC_U=smoke-token-123456" in (h.get("Cookie") or "") for h in Fake.account_headers
    ), Fake.account_headers[:1])

    print("\nC2. 歌词取词用到这个 Cookie")
    from app import lyrics_service as ls

    captured = {}

    def fake_lyrics(song_id, cookie=""):
        captured["cookie"] = cookie
        return {"lines": [{"start": 0, "text": "x"}], "granularity": "line"}

    orig_lyrics = ls.netease_lyrics
    ls.netease_lyrics = fake_lyrics  # type: ignore[assignment]
    ls._lyrics_cache.clear()
    try:
        ls.fetch_lyrics(1, "netease")
    finally:
        ls.netease_lyrics = orig_lyrics  # type: ignore[assignment]
    check("fetch_lyrics 用的是后台 Cookie", captured.get("cookie") == cookie, captured.get("cookie"))

    print("\nD. 启动恢复")
    ng._runtime_cookie = ""  # 模拟进程重启
    ng._loaded = False
    ng.hydrate()
    check("hydrate 从库里读回", ng.current_cookie() == cookie, ng.current_cookie())

    print("\nE. 退出登录")
    result = ng.clear_cookie(db)
    check("清掉了后台 Cookie", result["cleared"] is True, result)
    check("远端 logout 收到了 Cookie", any("MUSIC_U=smoke-token-123456" in c for c in Fake.logout_hits), Fake.logout_hits)
    check("内存也清空", ng.current_cookie() == "", ng.current_cookie())
    st2 = ng.status(db, verify=False)
    check("状态变未连接", st2["connected"] is False, st2)
    check("来源为空", st2["source"] == "", st2["source"])

    print("\nF. .env 兜底")
    settings.NETEASE_COOKIE = "MUSIC_U=from-env"
    check("没有后台 Cookie 时用 .env", ng.current_cookie() == "MUSIC_U=from-env", ng.current_cookie())
    st3 = ng.status(db)
    check("状态标成 env", st3["source"] == "env" and st3["connected"] is True, st3)
    check("env 也标出来了", st3["envConfigured"] is True, st3)

    print("\nG. 错误路径")
    check("过期会话拿不到二维码", _err_code(ng.qr_svg, "nope") == 404)

finally:
    db_failover.current_sessionmaker = _orig_sessionmaker  # type: ignore[assignment]
    settings.NETEASE_COOKIE = _orig_cookie
    db.close()
    srv.shutdown()

print(f"\n结果：{PASSED} 项通过，{len(FAILED)} 项失败")
if FAILED:
    print("失败项：" + "、".join(FAILED))
    sys.exit(1)
print("全部通过")
