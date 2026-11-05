"""检查 GitHub main 分支有没有新版本。

只读取远端信息并缓存，**不会拉代码、不会重启服务**。运行环境限制：
- GitHub 直连不通时，可在 .env 配置 UPDATE_PROXY（例如 http://127.0.0.1:7897）。
- 未登录 GitHub API 有每小时 60 次的限额，这里默认缓存 10 分钟。
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .config import settings

CACHE_TTL = 600  # 成功结果缓存 10 分钟
FAIL_TTL = 60  # 失败结果只缓存 1 分钟，网络恢复后能尽快重试
FORCE_COOLDOWN = 30  # 手动强制刷新最快 30 秒一次

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "ttl": 0, "payload": None}
_last_force = 0.0
_token_cache: str | None = None


def _github_token() -> str:
    """只使用显式配置的 UPDATE_TOKEN；不自动向 git 凭据管理器索取（无终端时会弹窗/卡住）。"""
    global _token_cache
    if _token_cache is None:
        _token_cache = settings.UPDATE_TOKEN or ""
    return _token_cache


def _headers() -> dict:
    headers = {
        "User-Agent": "CloudNETP-update-check",
        "Accept": "application/vnd.github+json",
    }
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _openers() -> list[urllib.request.OpenerDirector]:
    """优先走配置的代理，失败再试直连；没配代理就只直连。"""
    proxy = settings.UPDATE_PROXY
    handlers = []
    if proxy:
        handlers.append(
            urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
        )
    handlers.append(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    return handlers


def _fetch_json(url: str, timeout: int = 10) -> dict:
    last_error: Exception | None = None
    for opener in _openers():
        req = urllib.request.Request(
            url,
            headers=_headers(),
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 404:
                raise
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一转成检查失败
            last_error = exc
    raise last_error or RuntimeError("无法连接 GitHub")


def _version_parts(text: str | None) -> tuple[int, ...]:
    if not text:
        return ()
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 401:
            return "GitHub 令牌无效或已过期，请更新 UPDATE_TOKEN"
        if exc.code == 403:
            return "GitHub 接口限流（每小时 60 次）或令牌权限不足，稍后再试"
        if exc.code == 404:
            return "仓库不可访问：私有仓库请在 .env 配置 UPDATE_TOKEN"
        return f"GitHub 返回 {exc.code}"
    if isinstance(exc, TimeoutError):
        return "连接 GitHub 超时"
    reason = getattr(exc, "reason", exc)
    return f"无法连接 GitHub：{reason}"


def _check_remote() -> dict:
    repo = settings.UPDATE_REPO
    commit = _fetch_json(f"https://api.github.com/repos/{repo}/commits/main")
    contents = _fetch_json(
        f"https://api.github.com/repos/{repo}/contents/app/config.py?ref=main"
    )

    latest = None
    content = contents.get("content")
    if content:
        try:
            text = base64.b64decode(content).decode("utf-8", errors="ignore")
            matched = re.search(r'VERSION:\s*str\s*=\s*"([0-9]+(?:\.[0-9]+)*)"', text)
            if matched:
                latest = matched.group(1)
        except (ValueError, TypeError):
            latest = None

    commit_info = commit.get("commit", {}) if isinstance(commit, dict) else {}
    author = commit_info.get("author", {}) if isinstance(commit_info, dict) else {}
    message = (commit_info.get("message") or "").strip().splitlines()
    payload = {
        "ok": True,
        "current": settings.VERSION,
        "latest": latest,
        "hasUpdate": bool(latest)
        and _version_parts(latest) > _version_parts(settings.VERSION),
        "commit": {
            "sha": commit.get("sha", "")[:12] if isinstance(commit, dict) else "",
            "message": message[0][:200] if message else "",
            "date": author.get("date"),
            "url": commit.get("html_url") if isinstance(commit, dict) else None,
        },
        "checkedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "cached": False,
        "error": None if latest else "读取版本号失败，只能看到最新提交",
    }
    return payload


def check(force: bool = False) -> dict:
    """返回检查结果；force 仅管理员可用（由路由层限制）。"""
    global _last_force
    now = time.time()
    with _lock:
        cached = _cache["payload"]
        fresh = cached and now - _cache["at"] < _cache["ttl"]
        if fresh and not force:
            result = dict(cached)
            result["cached"] = True
            return result
        if force and now - _last_force < FORCE_COOLDOWN and cached:
            result = dict(cached)
            result["cached"] = True
            result["throttled"] = True
            return result
        if force:
            _last_force = now

    try:
        payload = _check_remote()
        ttl = CACHE_TTL
    except Exception as exc:  # noqa: BLE001
        payload = {
            "ok": False,
            "current": settings.VERSION,
            "latest": None,
            "hasUpdate": False,
            "commit": None,
            "checkedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "cached": False,
            "error": _friendly_error(exc),
        }
        ttl = FAIL_TTL

    with _lock:
        _cache.update({"at": now, "ttl": ttl, "payload": payload})
    return payload
