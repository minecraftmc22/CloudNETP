"""Google / GitHub OAuth 的最小实现（只用标准库 urllib）。

只走 **Authorization Code** 流程，没有 SDK、没有第三方依赖：
1. `authorize_url()` 拼出授权页地址，浏览器跳过去；
2. 回调拿 `code`，`exchange_code()` 换 access_token；
3. `fetch_identity()` 用 access_token 取一份统一格式的身份。

统一身份格式（provider 差异全部在这里抹平）::

    {"subject": "稳定唯一 ID", "email": "a@b.c" | None, "emailVerified": bool,
     "name": "显示名", "login": "provider 内的用户名", "avatar": "远程头像 URL"}

`subject` 是**绑定主键**：Google 用 OIDC 的 `sub`，GitHub 用数字 `id`。
邮箱只是「顺便拿到的联系方式」，绝不拿它做主键——邮箱可以改、可以重复。

错误一律抛 `OAuthError`，消息是给用户看的中文；调用方把它映射成
`/?oauth_error=<code>`，不把上游原文透出去。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from . import oauth_config

log = logging.getLogger("uvicorn.error").getChild("cloudnetp.oauth")

TIMEOUT = 12


def _endpoint(provider: str, kind: str, default: str) -> str:
    """端点地址允许用环境变量覆盖（CLOUDNETP_OAUTH_GOOGLE_TOKEN_URL 这类）。

    正常部署永远走官方地址；这个口子主要给隔离环境的端到端测试用——
    在本机起一个假授权服务器，把 token / userinfo 指过去就能测完整流程。
    """
    return os.getenv(f"CLOUDNETP_OAUTH_{provider.upper()}_{kind}_URL", default).rstrip("/")


AUTHORIZE = {
    "google": _endpoint("google", "AUTHORIZE", "https://accounts.google.com/o/oauth2/v2/auth"),
    "github": _endpoint("github", "AUTHORIZE", "https://github.com/login/oauth/authorize"),
}
TOKEN = {
    "google": _endpoint("google", "TOKEN", "https://oauth2.googleapis.com/token"),
    "github": _endpoint("github", "TOKEN", "https://github.com/login/oauth/access_token"),
}
USERINFO = {
    "google": _endpoint("google", "USERINFO", "https://openidconnect.googleapis.com/v1/userinfo"),
    "github": _endpoint("github", "USERINFO", "https://api.github.com/user"),
}
USER_EMAILS = {
    "google": None,
    "github": _endpoint("github", "EMAILS", "https://api.github.com/user/emails"),
}
SCOPES = {
    # openid 才有 sub；email 才有邮箱声明
    "google": "openid email profile",
    # 读公开资料 + 读邮箱列表（/user 的 email 可能为空）
    "github": "read:user user:email",
}

# 回调里能出现的错误码 → 用户可读文案（前端按 code 查同一张表）
ERRORS = {
    "denied": "你取消了第三方授权",
    "state": "授权状态已失效，请重新点击登录",
    "exchange": "换取授权凭证失败，请稍后重试",
    "profile": "读取第三方账号信息失败，请稍后重试",
    "email_taken": "该邮箱已被其它账号绑定，请先用密码登录，再到「账号安全」里绑定",
    "disabled": "该登录方式暂未开放",
    "not_linked": "该第三方账号还没有绑定到你的账号",
    "already_linked": "该第三方账号已绑定到其它账号",
    "banned": "该账号已被封禁，无法登录",
    "need_2fa": "该账号开启了两步验证，请改用密码登录",
    "closed": "站点已关闭注册",
    "server": "服务端出了点问题，请稍后重试",
}


class OAuthError(Exception):
    """`code` 是 ERRORS 里的键，`detail` 只写日志、不发给浏览器。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code if code in ERRORS else "server"
        self.detail = detail


def _request(url: str, data: dict | None = None, headers: dict | None = None) -> dict | list:
    """发一个请求并解析 JSON。`data` 非空时用 POST + form 编码。

    返回值允许是数组：GitHub 的 `/user/emails` 就是 JSON 数组，
    之前一刀切只收 dict 会把邮箱列表整包丢掉（邮箱永远拿不到）。
    """
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method="POST" if body else "GET")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if body:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    # GitHub 强制要求 UA，缺了直接 403
    req.add_header("User-Agent", "CloudNETP-OAuth")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read(256 * 1024)
    except urllib.error.HTTPError as exc:
        # 上游把错误原因放在 body 里，日志记一份便于排查；对外只给通用文案
        try:
            snippet = exc.read(2048).decode("utf-8", "replace")
        except OSError:
            snippet = ""
        raise OAuthError("server", f"HTTP {exc.code}: {snippet}") from exc
    except OSError as exc:  # URLError / 超时 / RemoteDisconnected 都是 OSError
        raise OAuthError("server", f"网络错误：{exc}") from exc
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise OAuthError("server", "上游返回的不是 JSON") from exc
    if not isinstance(payload, (dict, list)):
        raise OAuthError("server", "上游返回结构异常")
    return payload


def authorize_url(provider: str, cfg: dict, state: str, base: str | None = None) -> str:
    """拼授权页地址。`prompt=select_account` 让 Google 每次都问用哪个号，
    避免「上次登过别人的号，这次直接被静默复用」。`base` 见 oauth_config.callback_url。"""
    if provider not in AUTHORIZE:
        raise OAuthError("disabled")
    params = {
        "client_id": cfg.get(f"{provider}ClientId") or "",
        "redirect_uri": oauth_config.callback_url(provider, base),
        "response_type": "code",
        "scope": SCOPES[provider],
        "state": state,
    }
    if provider == "google":
        params["prompt"] = "select_account"
        params["access_type"] = "online"
    return f"{AUTHORIZE[provider]}?{urllib.parse.urlencode(params)}"


def exchange_code(provider: str, cfg: dict, code: str, base: str | None = None) -> str:
    """用 code 换 access_token。`base` 必须与 authorize 时一致，否则上游报 redirect_uri 不匹配。"""
    if provider not in TOKEN:
        raise OAuthError("disabled")
    payload = _request(
        TOKEN[provider],
        {
            "client_id": cfg.get(f"{provider}ClientId") or "",
            "client_secret": cfg.get(f"{provider}ClientSecret") or "",
            "code": code,
            "redirect_uri": oauth_config.callback_url(provider, base),
            "grant_type": "authorization_code",
        },
    )
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise OAuthError("exchange", f"没有拿到 access_token：{payload.get('error')}")
    return token


def _google_identity(token: str) -> dict:
    info = _request(
        USERINFO["google"],
        headers={"Authorization": f"Bearer {token}"},
    )
    subject = str(info.get("sub") or "").strip()
    if not subject:
        raise OAuthError("profile", "Google 用户信息里没有 sub")
    email = str(info.get("email") or "").strip().lower()
    # OIDC 的 email_verified 明确表示「这个邮箱归他」，我们只信任这一种
    verified = bool(info.get("email_verified"))
    name = str(info.get("name") or "").strip()
    return {
        "subject": subject,
        "email": email or None,
        "emailVerified": verified,
        "name": name or (email.split("@")[0] if email else "Google 用户"),
        "login": email.split("@")[0] if email else subject,
        "avatar": str(info.get("picture") or "").strip(),
    }


def _github_identity(token: str) -> dict:
    auth = {"Authorization": f"Bearer {token}"}
    user = _request(USERINFO["github"], headers=auth)
    subject = str(user.get("id") or "").strip()
    if not subject:
        raise OAuthError("profile", "GitHub 用户信息里没有 id")
    email = str(user.get("email") or "").strip().lower()
    verified = bool(email)
    # /user 的 email 只在「公开邮箱」时才有值，而且无法判断是否已验证；
    # 想要可靠的已验证邮箱必须查 /user/emails（需要 user:email 权限）
    try:
        emails = _request(USER_EMAILS["github"], headers=auth)
    except OAuthError as exc:
        log.info("读取 GitHub 邮箱列表失败，改用 /user 的 email：%s", exc.detail)
        emails = []
    if isinstance(emails, list):
        primary = next(
            (e for e in emails if isinstance(e, dict) and e.get("primary")),
            None,
        )
        picked = primary or next(
            (e for e in emails if isinstance(e, dict) and e.get("verified")),
            None,
        )
        if picked and picked.get("email"):
            email = str(picked["email"]).strip().lower()
            verified = bool(picked.get("verified"))
        elif not email:
            verified = False
    name = str(user.get("name") or "").strip() or str(user.get("login") or "").strip()
    return {
        "subject": subject,
        "email": email or None,
        "emailVerified": verified,
        "name": name or "GitHub 用户",
        "login": str(user.get("login") or "").strip() or f"github{subject}",
        "avatar": str(user.get("avatar_url") or "").strip(),
    }


def fetch_identity(provider: str, token: str) -> dict:
    """按 provider 取统一身份。"""
    if provider == "google":
        return _google_identity(token)
    if provider == "github":
        return _github_identity(token)
    raise OAuthError("disabled")
