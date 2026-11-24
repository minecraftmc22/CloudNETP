"""第三方登录（Google / GitHub OAuth）的端到端校验。

用法（先在隔离环境起 uvicorn，URL 指到本脚本内置的假授权服务器）::

    DATABASE_URL=... STORAGE_DIR=... PORT=8135 \
    CLOUDNETP_OAUTH_GOOGLE_TOKEN_URL=http://127.0.0.1:9911/token \
    CLOUDNETP_OAUTH_GOOGLE_USERINFO_URL=http://127.0.0.1:9911/userinfo \
    CLOUDNETP_OAUTH_GITHUB_TOKEN_URL=http://127.0.0.1:9911/token \
    CLOUDNETP_OAUTH_GITHUB_USERINFO_URL=http://127.0.0.1:9911/gh/user \
    CLOUDNETP_OAUTH_GITHUB_EMAILS_URL=http://127.0.0.1:9911/gh/emails \
    python -m uvicorn app.main:app --port 8135

    python -m scripts.oauth_smoke 8135

假授权服务器只实现 token / userinfo / emails 三个端点；authorize 页不模拟
（start 只断言 302 的 Location 形状），回调直接拿 start 的 state 手工拼。
会在库里留一个管理员 probe 账号（隔离库，用完随库丢弃）。
"""

from __future__ import annotations

import http.cookiejar
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.db import SessionLocal
from app.models import User
from app.security import hash_password

BASE = f"http://127.0.0.1:{int(sys.argv[1]) if len(sys.argv) > 1 else 8135}"
ADMIN = "oauthadmin"
PASSWORD = "OAuth#2026x"
SECRET = "probe-secret-9f3e"

passed = 0
failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [ok]   {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label} {detail}")


# --------------------------------------------------------------------------- #
# 假授权服务器（Google / GitHub 的最小替身）
# --------------------------------------------------------------------------- #
MOCK: dict = {
    "g_subject": "g-sub-1001",
    "g_email": "guser@example.com",
    "g_verified": True,
    "gh_subject": "200123",
    "gh_login": "ghprobe",
    "gh_email": "ghuser@example.com",
    "gh_verified": True,
}


class MockProvider(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # 安静
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path == "/token":
            self._json({"access_token": "tok-mock", "token_type": "bearer"})
        else:
            self._json({"error": "not_found"}, 404)

    def do_GET(self) -> None:
        if self.path == "/userinfo":
            self._json(
                {
                    "sub": MOCK["g_subject"],
                    "email": MOCK["g_email"],
                    "email_verified": MOCK["g_verified"],
                    "name": "Google Probe",
                    "picture": "",
                }
            )
        elif self.path == "/gh/user":
            self._json(
                {
                    "id": int(MOCK["gh_subject"]),
                    "login": MOCK["gh_login"],
                    "name": "GitHub Probe",
                    "email": None,
                    "avatar_url": "",
                }
            )
        elif self.path == "/gh/emails":
            self._json(
                [{"email": MOCK["gh_email"], "primary": True, "verified": MOCK["gh_verified"]}]
            )
        else:
            self._json({"error": "not_found"}, 404)


def start_mock() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 9911), MockProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --------------------------------------------------------------------------- #
# HTTP 客户端：不跟随 302，手工管 cookie
# --------------------------------------------------------------------------- #
class Client:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}

    def call(self, path: str, method: str = "GET", payload=None, headers: dict | None = None):
        data = None
        hdrs = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode()
            hdrs["Content-Type"] = "application/json"
        if self.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(BASE + path, data=data, headers=hdrs, method=method)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            res = opener.open(req, timeout=15)
            status, hdr = res.status, res.headers
            body = res.read()
        except urllib.error.HTTPError as exc:
            status, hdr, body = exc.code, exc.headers, exc.read()
        for raw in hdr.get_all("Set-Cookie") or []:
            first = raw.split(";", 1)[0]
            name, _, value = first.partition("=")
            if value and "expired" not in raw.lower():
                self.cookies[name.strip()] = value
            else:
                self.cookies.pop(name.strip(), None)
        ctype = hdr.get("content-type", "")
        parsed = json.loads(body) if "json" in ctype and body else body
        location = hdr.get("Location", "")
        return status, parsed, location

    def get(self, path: str):
        return self.call(path)

    def post(self, path: str, payload=None):
        return self.call(path, "POST", payload=payload)

    def put(self, path: str, payload=None):
        return self.call(path, "PUT", payload=payload)

    def delete(self, path: str):
        return self.call(path, "DELETE")

    def login(self, username: str, password: str):
        return self.post("/api/auth/login", {"username": username, "password": password})


def query_of(url: str) -> dict:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #
def ensure_admin() -> None:
    with SessionLocal() as db:
        old = db.query(User).filter(User.username == ADMIN).first()
        if old:
            db.delete(old)
        # 清掉上一轮可能留下的第三方绑定号，保证断言从零开始
        for name in ("guser", "ghuser"):
            row = db.query(User).filter(User.username == name).first()
            if row:
                db.delete(row)
        db.add(
            User(
                username=ADMIN,
                password_hash=hash_password(PASSWORD),
                email="oauthadmin@example.com",
                email_verified=True,
                role=1,
                is_super=True,
                token_version=1,
            )
        )
        db.commit()


def run() -> None:
    admin = Client()
    user = Client()

    # -- 未配置时 ------------------------------------------------------------
    st, meta, _ = user.get("/api/meta")
    check("meta 可读", st == 200 and meta.get("version") == "0.6.5", f"{st}")
    check("未配置时 oauth 为空", meta.get("oauth") == [], str(meta.get("oauth")))

    st, _, loc = user.get("/api/auth/oauth/google/start")
    check(
        "未配置 start → disabled",
        st == 302 and loc.endswith("/?oauth_error=disabled"),
        f"{st} {loc}",
    )

    # -- 后台配置 ------------------------------------------------------------
    st, body, _ = admin.login(ADMIN, PASSWORD)
    check("管理员登录", st == 200, f"{st} {body if st != 200 else ''}")

    st, body, _ = admin.get("/api/admin/oauth")
    check("后台读 oauth 快照", st == 200 and "config" in body, f"{st}")
    snap = body["config"]
    check(
        "快照含回调地址",
        snap["providers"]["google"]["callbackUrl"].endswith("/api/auth/oauth/google/callback"),
        str(snap["providers"]["google"]),
    )

    st, body, _ = admin.put(
        "/api/admin/oauth",
        {
            "googleEnabled": True,
            "googleClientId": "gid-123",
            "googleClientSecret": SECRET,
            "githubEnabled": True,
            "githubClientId": "hid-456",
            "githubClientSecret": "gh-secret",
        },
    )
    check("后台保存配置", st == 200, f"{st} {body}")
    snap = body["config"]
    check("google/github 已 ready", snap["ready"] == ["google", "github"], str(snap["ready"]))
    check("密钥只回掩码", snap["providers"]["google"]["hasSecret"] and SECRET not in json.dumps(snap),
          str(snap["providers"]["google"]))

    st, meta, _ = user.get("/api/meta")
    check("meta 暴露 ready 列表", meta.get("oauth") == ["google", "github"], str(meta.get("oauth")))

    # -- Google 登录：首次建号 ------------------------------------------------
    st, _, loc = user.get("/api/auth/oauth/google/start?next=/files")
    check("start 302 到授权页", st == 302 and "state=" in loc and "client_id=gid-123" in loc, f"{st} {loc}")
    state = query_of(loc).get("state", "")

    st, _, loc = user.get(f"/api/auth/oauth/google/callback?code=good&state={state}")
    check("回调 302 回 next", st == 302 and loc.endswith("/files"), f"{st} {loc}")
    check("回调下发会话 cookie", "cloudnetp_session" in user.cookies, str(user.cookies.keys()))

    st, body, _ = user.get("/api/me")
    me = body.get("user") or body
    check(
        "自动建号并登录",
        st == 200 and me.get("username") == "guser" and me.get("email") == MOCK["g_email"],
        f"{st} {me.get('username')} {me.get('email')}",
    )
    g_uid = me.get("id")

    # 同一 subject 再登一次：还是同一个账号（state 是无状态签名，不做一次性）
    user2 = Client()
    st, _, loc = user2.get(f"/api/auth/oauth/google/callback?code=good&state={state}")
    st, body, _ = user2.get("/api/me")
    me2 = (body.get("user") or body)
    check("同一 subject 二次登录同账号", st == 200 and me2.get("id") == g_uid, f"{st} {me2.get('id')} vs {g_uid}")

    # -- GitHub 登录：邮箱来自 /user/emails -----------------------------------
    gh = Client()
    st, _, loc = gh.get("/api/auth/oauth/github/start")
    st, _, loc = gh.get(f"/api/auth/oauth/github/callback?code=good&state={query_of(loc)['state']}")
    if st != 302 or "oauth_error" in loc:
        check("GitHub 回调成功", False, f"{st} {loc}")
    st, body, _ = gh.get("/api/me")
    me = body.get("user") or body
    check(
        "GitHub 登录取到已验证邮箱",
        st == 200 and me.get("username") == "ghprobe" and me.get("email") == MOCK["gh_email"],
        f"{st} {me.get('username')} {me.get('email')}",
    )

    # -- 邮箱冲突：不自动合并 -------------------------------------------------
    MOCK["g_subject"] = "g-sub-2002"  # 新的第三方身份，邮箱撞现有账号
    MOCK["g_email"] = "guser@example.com"
    clash = Client()
    st, _, loc = clash.get("/api/auth/oauth/google/start")
    st, _, loc = clash.get(f"/api/auth/oauth/google/callback?code=good&state={query_of(loc)['state']}")
    check(
        "邮箱被占用 → email_taken",
        st == 302 and "oauth_error=email_taken" in loc,
        f"{st} {loc}",
    )
    MOCK["g_subject"], MOCK["g_email"] = "g-sub-1001", "guser@example.com"  # 还原

    # -- state 校验与取消 -----------------------------------------------------
    st, _, loc = user.get("/api/auth/oauth/google/callback?code=good&state=tampered")
    check("伪造 state → state 错误", "oauth_error=state" in loc, loc)
    st, _, loc = user.get(
        f"/api/auth/oauth/google/callback?error=access_denied&state={state}"
    )
    check("用户取消 → denied", "oauth_error=denied" in loc, loc)

    # -- 绑定 / 解绑 ----------------------------------------------------------
    MOCK["g_subject"] = "g-sub-3003"  # 管理员绑定一个没人占用的 subject
    st, body, _ = admin.get("/api/me/oauth")
    check(
        "me/oauth 初始未绑定",
        st == 200 and not body["providers"]["google"]["linked"] and body["providers"]["google"]["ready"],
        str(body),
    )

    st, _, loc = admin.get("/api/auth/oauth/google/start?mode=bind")
    check("绑定 start 也 302", st == 302 and "state=" in loc, f"{st} {loc}")
    st, _, loc = admin.get(f"/api/auth/oauth/google/callback?code=good&state={query_of(loc)['state']}")
    check("绑定回调回设置页", st == 302 and loc.endswith("/#/settings"), f"{st} {loc}")

    st, body, _ = admin.get("/api/me/oauth")
    check("绑定后 linked=true", body["providers"]["google"]["linked"] is True, str(body))

    # 别人的 subject 再绑到管理员号上 → already_linked
    MOCK["g_subject"] = "g-sub-1001"  # 已被 guser 占用
    st, _, loc = admin.get("/api/auth/oauth/google/start?mode=bind")
    st, _, loc = admin.get(f"/api/auth/oauth/google/callback?code=good&state={query_of(loc)['state']}")
    check("subject 被占 → already_linked", "oauth_error=already_linked" in loc, loc)

    # 解绑（管理员有已验证邮箱，允许解到空）
    MOCK["g_subject"] = "g-sub-3003"
    st, body, _ = admin.delete("/api/auth/oauth/google")
    check("解绑接口可用", st == 200 and body.get("ok"), f"{st} {body}")

    st, body, _ = admin.get("/api/me/oauth")
    check("解绑后 linked=false", body["providers"]["google"]["linked"] is False, str(body))

    # -- 未登录绑定被拒 -------------------------------------------------------
    anon = Client()
    st, _, loc = anon.get("/api/auth/oauth/google/start?mode=bind")
    check("未登录请求绑定 → not_linked", "oauth_error=not_linked" in loc, loc)

    # -- 后台清除 -------------------------------------------------------------
    st, body, _ = admin.delete("/api/admin/oauth")
    check("后台清除配置", st == 200 and body["config"]["ready"] == [], f"{st} {body}")
    st, meta, _ = user.get("/api/meta")
    check("清除后 meta.oauth 为空", meta.get("oauth") == [], str(meta.get("oauth")))

    # -- 审计 -----------------------------------------------------------------
    st, logs, _ = admin.get("/api/admin/logs?page=1&pageSize=100")
    if st == 200:
        rows = logs.get("items") or logs.get("list") or logs.get("logs") or []
        actions = {row.get("action") for row in rows if isinstance(row, dict)}
        oauth_actions = {a for a in actions if a and ("oauth" in a)}
        check(
            "审计含 oauth 动作",
            bool(oauth_actions),
            str(sorted(oauth_actions)[:8]) if oauth_actions else str(sorted(a for a in actions if a)[:8]),
        )
    else:
        check("审计接口可读", False, f"{st}")


def main() -> None:
    server = start_mock()
    try:
        ensure_admin()
        run()
    finally:
        server.shutdown()
    print(f"\n通过 {passed} / {passed + failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
