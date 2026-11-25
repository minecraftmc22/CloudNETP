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


class _ForcedProxy(urllib.request.ProxyHandler):
    """显式配置的代理必须走。

    urllib 的 `proxy_open` 里有 `proxy_bypass(req.host)`：环境变量
    `NO_PROXY` 带 127.0.0.1（本机调试常配）时，指向 127.0.0.1 的目标会连
    显式代理都绕掉。配置了代理就要走代理，把 host 摘掉再交父类处理，
    绕过那一条判断；父类其余逻辑（解析凭据、set_proxy）不用 host。
    """

    def proxy_open(self, req, proxy, req_type):
        real_host, req.host = req.host, ""
        try:
            return super().proxy_open(req, proxy, req_type)
        finally:
            req.host = real_host


def _request(
    url: str,
    data: dict | None = None,
    headers: dict | None = None,
    proxy: str | None = None,
) -> dict | list:
    """发一个请求并解析 JSON。`data` 非空时用 POST + form 编码。

    返回值允许是数组：GitHub 的 `/user/emails` 就是 JSON 数组，
    之前一刀切只收 dict 会把邮箱列表整包丢掉（邮箱永远拿不到）。

    `proxy`：服务器自己出不了网（国内直连 Google 很常见）时走配置的代理；
    不传则按 urllib 默认行为（进程环境变量里有 HTTPS_PROXY 时自然生效）。
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
    if proxy:
        opener = urllib.request.build_opener(_ForcedProxy({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener()
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
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


def exchange_code(
    provider: str, cfg: dict, code: str, base: str | None = None, proxy: str | None = None
) -> str:
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
        proxy=proxy,
    )
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise OAuthError("exchange", f"没有拿到 access_token：{payload.get('error')}")
    return token


def _google_identity(token: str, proxy: str | None = None) -> dict:
    info = _request(
        USERINFO["google"],
        headers={"Authorization": f"Bearer {token}"},
        proxy=proxy,
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


def _github_identity(token: str, proxy: str | None = None) -> dict:
    auth = {"Authorization": f"Bearer {token}"}
    user = _request(USERINFO["github"], headers=auth, proxy=proxy)
    subject = str(user.get("id") or "").strip()
    if not subject:
        raise OAuthError("profile", "GitHub 用户信息里没有 id")
    email = str(user.get("email") or "").strip().lower()
    verified = bool(email)
    # /user 的 email 只在「公开邮箱」时才有值，而且无法判断是否已验证；
    # 想要可靠的已验证邮箱必须查 /user/emails（需要 user:email 权限）
    try:
        emails = _request(USER_EMAILS["github"], headers=auth, proxy=proxy)
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


def fetch_identity(provider: str, token: str, proxy: str | None = None) -> dict:
    """按 provider 取统一身份。"""
    if provider == "google":
        return _google_identity(token, proxy)
    if provider == "github":
        return _github_identity(token, proxy)
    raise OAuthError("disabled")


# --------------------------------------------------------------------------- #
# 后台「测试连接」诊断
# --------------------------------------------------------------------------- #
def diagnose(provider: str, cfg: dict, proxy: str | None = None, base: str | None = None) -> dict:
    """向 token 端点发一次探测请求，把真实失败原因带给管理员。

    登录流程对外只给通用文案（oauth_error=server），但「server」背后的原因
    可能是代理不通、可能是 Secret 填错、也可能是回调地址没对上——排障时
    必须区分开。做法：用假 code 换一次 token，按上游的反应给出结论：

    - HTTP 400 + invalid_grant / bad_verification_code：链路通、凭据被识别，
      只是 code 是假的 → **连通正常**
    - HTTP 400 + invalid_client：Client ID / Secret 不对
    - HTTP 400 + redirect_uri_mismatch：回调地址没对上
    - OSError：代理不通 / 超时 / DNS 失败（附 Docker 127.0.0.1 提示）
    """
    if provider not in TOKEN:
        return {"ok": False, "stage": "config", "message": "未知的登录方式"}
    if not (cfg.get(f"{provider}ClientId") and cfg.get(f"{provider}ClientSecret")):
        return {"ok": False, "stage": "config", "message": "Client ID / Secret 还没配置全"}

    proxy = (proxy or "").strip()
    if proxy:
        scheme = urllib.parse.urlsplit(proxy).scheme.lower()
        if scheme not in {"http", "https"}:
            return {
                "ok": False,
                "stage": "proxy",
                "message": f"暂只支持 http(s) 代理，收到的是「{scheme or '无协议'}」；socks5 请先换算成 http 代理",
            }

    body = urllib.parse.urlencode(
        {
            "client_id": cfg.get(f"{provider}ClientId") or "",
            "client_secret": cfg.get(f"{provider}ClientSecret") or "",
            "code": "cloudnetp-probe-invalid-code",
            "redirect_uri": oauth_config.callback_url(provider, base),
            "grant_type": "authorization_code",
        }
    ).encode()
    req = urllib.request.Request(TOKEN[provider], data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "CloudNETP-OAuth")
    if proxy:
        opener = urllib.request.build_opener(_ForcedProxy({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener()

    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read(16 * 1024).decode("utf-8", "replace")
        # GitHub 对坏 code 有时回 200 + error 字段而不是 HTTP 4xx
        error = ""
        try:
            error = str(json.loads(raw).get("error") or "")
        except json.JSONDecodeError:
            pass
        if error:
            return {
                "ok": True,
                "stage": "done",
                "message": "连通正常（服务器可达、凭据格式被接受）",
                "status": resp.status,
                "detail": f"{error}: {raw[:300]}",
            }
        return {"ok": True, "stage": "done", "message": "连通正常", "status": resp.status, "detail": raw[:300]}
    except urllib.error.HTTPError as exc:
        try:
            snippet = exc.read(2048).decode("utf-8", "replace")
        except OSError:
            snippet = ""
        error = ""
        try:
            error = str(json.loads(snippet).get("error") or "")
        except json.JSONDecodeError:
            pass
        if error in {"invalid_grant", "bad_verification_code", "invalid_request", "invalid_code"}:
            # code 是编的，上游肯好好回业务错误 = 链路与凭据都没问题
            return {
                "ok": True,
                "stage": "done",
                "message": "连通正常（服务器可达、凭据被上游识别）",
                "status": exc.code,
                "detail": f"{error}: {snippet[:300]}",
            }
        if error == "invalid_client":
            return {
                "ok": False,
                "stage": "credentials",
                "message": "Client ID / Secret 不正确（上游回了 invalid_client）",
                "status": exc.code,
                "detail": snippet[:300],
            }
        if error == "redirect_uri_mismatch":
            return {
                "ok": False,
                "stage": "redirect",
                "message": "回调地址与上游后台登记的不一致（redirect_uri_mismatch）——对照卡片里的回调地址逐字符核对",
                "status": exc.code,
                "detail": snippet[:300],
            }
        return {
            "ok": False,
            "stage": "http",
            "message": f"上游返回 HTTP {exc.code}",
            "status": exc.code,
            "detail": snippet[:300],
        }
    except OSError as exc:  # URLError / 超时 / RemoteDisconnected
        result = {"ok": False, "stage": "network", "message": f"网络错误：{exc}"}
        if proxy and ("127.0.0.1" in proxy or "localhost" in proxy):
            result["hint"] = (
                "代理指向 127.0.0.1 / localhost：若服务跑在 Docker 里，容器内的 127.0.0.1 是容器自己，"
                "请改用宿主机 IP 或 host.docker.internal；代理本体也要允许来自服务器的连接"
            )
        return result
