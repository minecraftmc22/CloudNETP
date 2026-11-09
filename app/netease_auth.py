"""网易云扫码登录：管理员在后台点一下，服务端自己把 Cookie 拿回来存好。

几条硬约束：

* **Cookie 不进浏览器**：扫码成功后从 ``Set-Cookie`` 里取 ``MUSIC_U`` / ``__csrf``，
  加密写进 ``system_settings``，之后由服务端直接带给网易云。所有接口只回
  「是否已连接 + 昵称/头像/更新时间」，任何响应都不含 Cookie 原文。
* **登录态不进前端请求**：前端只跟本站的 ``/api/admin/netease/*`` 打交道，
  轮询的是我们自己的会话 id，不是网易云的 key；真正的 key 只存在服务端内存。
* **Cookie 优先级**：后台扫码保存的 > ``.env`` 的 ``NETEASE_COOKIE``。
  后台没登录时继续用 .env，老部署不用改。
* **重启不丢**：进程启动时把库里的 Cookie 读进内存；读取失败就先用 .env 兜底，
  下一次用到再重试。

二维码有效期 3 分钟（网易云自己的限制），过期需要重新获取。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.cookies import SimpleCookie

from . import secret_box, settings_store
from .config import settings
from .user_service import iso

log = logging.getLogger("uvicorn.error").getChild("cloudnetp.netease")

# 上游地址（测试里会改写这个常量指到本地假服务）
API_BASE = "https://music.163.com"

QR_TTL = 180  # 网易云二维码有效期（秒）
VERIFY_TTL = 60  # 账号信息复查间隔（秒），别每次打开后台都打一次网易云
_TIMEOUT = 6.0

_COOKIE_KEYS = ("MUSIC_U", "__csrf")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    # 和网页版一致的固定部分；登录后再拼上 MUSIC_U
    "Cookie": "os=pc; appver=2.10.13",
}

# 不走系统代理：本机代理对 TLS 上游会超时（与 lyrics_service 一致）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_lock = threading.Lock()
_sessions: dict[str, dict] = {}
_runtime_cookie = ""
_loaded = False
_verify_cache: dict[str, object] = {"cookie": "", "at": 0.0, "profile": None, "error": ""}

STORE_COOKIE = "netease_cookie"
STORE_COOKIE_AT = "netease_cookie_at"
STORE_PROFILE = "netease_profile"


class NeteaseError(RuntimeError):
    """可直接展示给管理员的中文错误。"""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _request(path: str, cookie: str = "") -> tuple[dict, object]:
    headers = dict(_HEADERS)
    if cookie:
        headers["Cookie"] = headers["Cookie"] + "; " + cookie
    req = urllib.request.Request(API_BASE + path, headers=headers)
    try:
        with _opener.open(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read() or b"{}"), resp.headers
    except urllib.error.HTTPError as exc:
        raise NeteaseError(f"网易云接口返回 HTTP {exc.code}", status=exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise NeteaseError("连不上网易云，检查服务器网络后重试") from exc


def _extract_cookie(headers) -> str:
    """从 Set-Cookie 里只挑登录需要的两项，其余（埋点、设备 id）不存。"""
    jar: dict[str, str] = {}
    for raw in headers.get_all("Set-Cookie") or []:
        try:
            parsed = SimpleCookie()
            parsed.load(raw)
        except Exception:  # pragma: no cover - 个别头不是标准 Cookie
            continue
        for name, morsel in parsed.items():
            if name in _COOKIE_KEYS:
                jar[name] = morsel.value
    return "; ".join(f"{name}={jar[name]}" for name in _COOKIE_KEYS if jar.get(name))


# --------------------------------------------------------------------------- #
# 扫码会话（只存在服务端内存，进程重启即失效，重新扫码即可）
# --------------------------------------------------------------------------- #
def _prune(now: float | None = None) -> None:
    now = time.time() if now is None else now
    for sid in [k for k, v in _sessions.items() if now - v["at"] > QR_TTL + 30]:
        _sessions.pop(sid, None)


def qr_create() -> dict:
    body, _ = _request("/api/login/qrcode/unikey?type=1")
    if body.get("code") != 200 or not body.get("unikey"):
        raise NeteaseError("获取二维码失败，稍后再试")
    key = str(body["unikey"])
    sid = secrets.token_urlsafe(18)
    with _lock:
        _prune()
        # 每个进程只留最近 10 个会话，避免有人刷接口把内存撑大
        while len(_sessions) >= 10:
            _sessions.pop(next(iter(_sessions)))
        _sessions[sid] = {"key": key, "at": time.time()}
    return {
        "session": sid,
        "qrUrl": f"{API_BASE}/login?codekey={key}",
        "expiresIn": QR_TTL,
    }


def _session(sid: str) -> dict:
    with _lock:
        sess = _sessions.get(sid)
    if sess is None:
        raise NeteaseError("二维码已失效，请重新获取", status=404)
    if time.time() - sess["at"] > QR_TTL:
        with _lock:
            _sessions.pop(sid, None)
        raise NeteaseError("二维码已过期，请重新获取", status=410)
    return sess


def qr_poll(sid: str) -> dict:
    """轮询扫码状态。

    返回 {code, nickname?}；code 与网易云一致：
    801 待扫码 / 802 已扫码待确认 / 803 成功（此时 cookie 只在本函数内部可见）。
    成功时额外带 ``cookie`` 字段，仅供本模块落库，路由层不会下发。
    """
    sess = _session(sid)
    body, headers = _request(
        "/api/login/qrcode/client/login?"
        + urllib.parse.urlencode({"key": sess["key"], "type": 1})
    )
    code = body.get("code")
    if code == 800:
        with _lock:
            _sessions.pop(sid, None)
        return {"code": 800, "message": "二维码已过期，请重新获取"}
    if code == 801:
        return {"code": 801}
    if code == 802:
        return {"code": 802, "nickname": (body.get("nickname") or "").strip()}
    if code == 803:
        cookie = _extract_cookie(headers)
        if not cookie:
            raise NeteaseError("登录成功但没拿到登录票据，请重试")
        with _lock:
            _sessions.pop(sid, None)
        return {"code": 803, "cookie": cookie, "nickname": (body.get("nickname") or "").strip()}
    raise NeteaseError(f"网易云返回了未知状态（code {code}）")


def qr_svg(sid: str) -> str:
    """把扫码地址画成 SVG（服务端生成，前端只当图片用）。"""
    sess = _session(sid)
    url = f"{API_BASE}/login?codekey={sess['key']}"
    return _render_svg(url)


def _render_svg(text: str, box: int = 8, border: int = 2) -> str:
    try:
        import qrcode
    except ImportError:  # pragma: no cover - 老环境没装新依赖时给个能看懂的提示
        raise NeteaseError(
            "缺少 qrcode 依赖：请先执行 pip install -r requirements.txt",
            status=503,
        ) from None

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box,
        border=border,
    )
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    modules = len(matrix)
    size = modules * box
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" shape-rendering="crispEdges">',
        # 白底：深色主题下二维码也要能扫
        f'<rect width="{size}" height="{size}" fill="#ffffff"/>',
    ]
    # 按行合并连续模块，少写几百个 rect
    for y, row in enumerate(matrix):
        x = 0
        while x < modules:
            if not row[x]:
                x += 1
                continue
            start = x
            while x < modules and row[x]:
                x += 1
            parts.append(
                f'<rect x="{start * box}" y="{y * box}" '
                f'width="{(x - start) * box}" height="{box}" fill="#111111"/>'
            )
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Cookie 存取（加密落库 + 内存缓存，取歌词的路径不碰数据库）
# --------------------------------------------------------------------------- #
def _set_runtime(cookie: str) -> None:
    global _runtime_cookie, _loaded
    with _lock:
        _runtime_cookie = cookie
        _loaded = True


def stored_cookie(db) -> str:
    """后台扫码保存的 Cookie（解密后）。"""
    raw = settings_store.get_value(db, STORE_COOKIE)
    return secret_box.decrypt(raw, "netease") if raw else ""


def stored_profile(db) -> dict:
    raw = settings_store.get_value(db, STORE_PROFILE)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save_cookie(db, cookie: str, profile: dict | None = None) -> None:
    cookie = (cookie or "").strip()
    if not cookie:
        raise NeteaseError("Cookie 为空，登录没有完成")
    settings_store.set_value(db, STORE_COOKIE, secret_box.encrypt(cookie, "netease"))
    settings_store.set_value(db, STORE_COOKIE_AT, iso(datetime.now(timezone.utc).replace(tzinfo=None)) or "")
    if profile:
        settings_store.set_value(db, STORE_PROFILE, json.dumps(profile, ensure_ascii=False))
    db.commit()
    _set_runtime(cookie)
    with _lock:
        _verify_cache.update({"cookie": "", "at": 0.0, "profile": None, "error": ""})
    log.info("网易云 Cookie 已保存（%s）", secret_box.mask_secret(cookie))


def clear_cookie(db) -> dict:
    """退出登录：清掉后台保存的 Cookie，并尽量让网易云那边也失效。"""
    cookie = stored_cookie(db)
    if cookie:
        try:
            _request("/api/logout", cookie)
        except NeteaseError as exc:  # 远端失败不影响本地清理
            log.warning("网易云退出接口失败（继续清理本地）：%s", exc.message)
    settings_store.set_value(db, STORE_COOKIE, "")
    settings_store.set_value(db, STORE_COOKIE_AT, "")
    settings_store.set_value(db, STORE_PROFILE, "")
    db.commit()
    _set_runtime("")
    with _lock:
        _verify_cache.update({"cookie": "", "at": 0.0, "profile": None, "error": ""})
    return {"cleared": bool(cookie)}


def hydrate() -> None:
    """启动时把库里的 Cookie 读进内存；失败就先用 .env 兜底，下次再试。"""
    global _loaded, _runtime_cookie
    with _lock:
        if _loaded:
            return
    try:
        from . import db_failover

        with db_failover.current_sessionmaker()() as db:
            cookie = stored_cookie(db)
    except Exception:  # pragma: no cover - 数据库还没就绪时不该阻断启动
        log.warning("读取网易云 Cookie 失败，本次先用 .env 配置", exc_info=True)
        return
    with _lock:
        _runtime_cookie = cookie
        _loaded = True


def current_cookie() -> str:
    """当前生效的 Cookie：后台登录的优先，其次 .env。"""
    hydrate()
    with _lock:
        cookie = _runtime_cookie
    return cookie or (settings.NETEASE_COOKIE or "").strip()


# --------------------------------------------------------------------------- #
# 账号信息与状态
# --------------------------------------------------------------------------- #
def fetch_profile(cookie: str) -> dict:
    body, _ = _request("/api/nuser/account/get", cookie)
    if body.get("code") != 200:
        raise NeteaseError("Cookie 已失效，请重新扫码登录")
    profile = body.get("profile") or {}
    return {
        "userId": profile.get("userId") or 0,
        "nickname": (profile.get("nickname") or "").strip(),
        "avatarUrl": profile.get("avatarUrl") or "",
        "vipType": profile.get("vipType") or 0,
    }


def status(db, verify: bool = True) -> dict:
    """给后台卡片用的状态：只有结论与昵称，永远不含 Cookie。"""
    admin_cookie = stored_cookie(db)
    env_cookie = (settings.NETEASE_COOKIE or "").strip()
    cookie = admin_cookie or env_cookie
    source = "admin" if admin_cookie else ("env" if env_cookie else "")
    profile = stored_profile(db) if admin_cookie else {}
    updated_at = settings_store.get_value(db, STORE_COOKIE_AT) or ""
    verified = False
    error = ""

    if cookie and verify:
        with _lock:
            cache = dict(_verify_cache)
        if cache["cookie"] == cookie and time.time() - float(cache["at"] or 0) < VERIFY_TTL:
            profile = cache["profile"] or profile
            error = str(cache["error"] or "")
            verified = not error
        else:
            try:
                profile = fetch_profile(cookie)
                verified = True
                if admin_cookie:
                    settings_store.set_value(
                        db, STORE_PROFILE, json.dumps(profile, ensure_ascii=False)
                    )
                    db.commit()
            except NeteaseError as exc:
                error = exc.message
            with _lock:
                _verify_cache.update(
                    {
                        "cookie": cookie,
                        "at": time.time(),
                        "profile": profile or None,
                        "error": error,
                    }
                )

    return {
        "connected": bool(cookie),
        "source": source,
        "nickname": (profile or {}).get("nickname", "") or "",
        "userId": (profile or {}).get("userId", 0) or 0,
        "avatarUrl": (profile or {}).get("avatarUrl", "") or "",
        "vipType": (profile or {}).get("vipType", 0) or 0,
        "updatedAt": updated_at,
        "verified": verified,
        "error": error,
        # .env 里也配了的话要告诉前端，否则「退出登录」后卡片会莫名又连上
        "envConfigured": bool(env_cookie),
    }
