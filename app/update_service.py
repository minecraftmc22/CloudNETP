"""检查 GitHub main 分支有没有新版本。

只读取远端信息并缓存，**不会拉代码、不会重启服务**。运行环境限制：
- GitHub 直连不通时，可在 .env 配置 UPDATE_PROXY（例如 http://127.0.0.1:7897）。
- 未登录 GitHub API 有每小时 60 次的限额，这里默认缓存 10 分钟。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .config import BASE_DIR, settings

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


# --------------------------------------------------------------------------- #
# 真正执行更新（手动 / 自动共用）
# --------------------------------------------------------------------------- #
_apply_lock = threading.Lock()
_auto_started = False


def _git(root: Path, args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def _git_error(result: subprocess.CompletedProcess) -> str:
    text = (result.stderr or result.stdout or "").strip()
    return text[-600:] if text else f"git 退出码 {result.returncode}"


def _read_local_version(root: Path) -> str | None:
    try:
        text = (root / "app" / "config.py").read_text(encoding="utf-8")
    except OSError:
        return None
    matched = re.search(r'VERSION:\s*str\s*=\s*"([0-9]+(?:\.[0-9]+)*)"', text)
    return matched.group(1) if matched else None


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _schedule_restart() -> str:
    """返回 spawn / reload / skipped / failed。

    开发模式（RELOAD=true）由 uvicorn 的 WatchFiles 自动重载；
    生产模式拉起一个独立 helper，等端口释放后用同一个解释器重启 main.py。
    """
    if settings.RELOAD or os.environ.get("CLOUDNETP_RELOAD") == "1":
        return "reload"
    if os.environ.get("CLOUDNETP_RESTART_CMD"):
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            os.environ["CLOUDNETP_RESTART_CMD"],
            cwd=str(BASE_DIR),
            shell=True,
            creationflags=flags,
        )
        return "spawn"
    try:
        helper = BASE_DIR / "scripts" / "restart_helper.py"
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [sys.executable, str(helper), str(os.getpid())],
            cwd=str(BASE_DIR),
            creationflags=flags,
            close_fds=True,
        )
        return "spawn"
    except OSError:
        return "failed"


def _exit_after(delay: float = 2.0) -> None:
    threading.Timer(delay, lambda: os._exit(0)).start()


def apply_update(
    *,
    restart: bool = True,
    trigger: str = "manual",
    root: Path | None = None,
) -> dict:
    """git fetch + merge --ff-only origin/main；不 clean 不动手。"""
    base = Path(root or BASE_DIR)
    if not (base / ".git").exists():
        return {"ok": False, "updated": False, "error": "当前目录不是 git 仓库，无法自动更新"}

    with _apply_lock:
        try:
            branch = _git(base, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=20).stdout.strip()
            if branch != "main":
                return {
                    "ok": False,
                    "updated": False,
                    "error": f"当前分支是 {branch or '未知'}，只支持在 main 分支自动更新",
                }

            dirty = _git(base, ["status", "--porcelain", "--untracked-files=no"], timeout=20).stdout.strip()
            if dirty:
                files = [line.strip() for line in dirty.splitlines()[:8]]
                return {
                    "ok": False,
                    "updated": False,
                    "code": "dirty",
                    "error": "工作区有未提交改动，请先提交或还原后再更新",
                    "files": files,
                }

            fetch = _git(base, ["fetch", "--quiet", "origin", "main"], timeout=180)
            if fetch.returncode != 0:
                return {"ok": False, "updated": False, "error": f"拉取失败：{_git_error(fetch)}"}

            local = _git(base, ["rev-parse", "HEAD"], timeout=20).stdout.strip()
            remote = _git(base, ["rev-parse", "origin/main"], timeout=20).stdout.strip()
            if not remote:
                return {"ok": False, "updated": False, "error": "读不到 origin/main，请检查远端配置"}
            if local == remote:
                return {
                    "ok": True,
                    "updated": False,
                    "fromVersion": _read_local_version(base),
                    "toVersion": _read_local_version(base),
                    "commit": remote[:12],
                    "message": "已经是最新版本",
                }

            from_version = _read_local_version(base)
            req_before = _file_hash(base / "requirements.txt")
            merge = _git(base, ["merge", "--ff-only", "origin/main"], timeout=120)
            if merge.returncode != 0:
                return {"ok": False, "updated": False, "error": f"合并失败：{_git_error(merge)}"}

            to_version = _read_local_version(base)
            deps = "unchanged"
            if _file_hash(base / "requirements.txt") != req_before:
                pip = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                    cwd=str(base),
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                    env=dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1"),
                )
                if pip.returncode != 0:
                    return {
                        "ok": False,
                        "updated": True,
                        "fromVersion": from_version,
                        "toVersion": to_version,
                        "error": "代码已更新，但依赖安装失败，请手动执行 pip install -r requirements.txt",
                        "detail": (pip.stderr or pip.stdout or "")[-600:],
                    }
                deps = "installed"

            restart_state = _schedule_restart() if restart else "skipped"
            # 热重载模式退出 worker，由 uvicorn reloader 拉起新代码；生产模式由 helper 拉起
            if restart and restart_state in {"spawn", "reload"}:
                _exit_after(2.0)

            return {
                "ok": True,
                "updated": True,
                "trigger": trigger,
                "fromVersion": from_version,
                "toVersion": to_version,
                "commit": remote[:12],
                "deps": deps,
                "restart": restart_state,
                "message": "更新完成" + ("，服务正在重启" if restart_state in {"spawn", "reload"} else "，请手动重启服务"),
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "updated": False, "error": "git 操作超时，请检查网络后重试"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "updated": False, "error": f"更新失败：{exc}"}


def start_auto_watcher() -> None:
    """后台线程：每 6 小时读一次自动更新开关，有新版本就拉取并重启。"""
    global _auto_started
    if _auto_started:
        return
    _auto_started = True
    threading.Thread(target=_auto_loop, name="cloudnetp-auto-update", daemon=True).start()


def _auto_loop() -> None:
    time.sleep(300)  # 启动后先稳 5 分钟，避免开机就折腾
    while True:
        try:
            from . import db_failover, settings_store

            db = db_failover.current_sessionmaker()()
            try:
                enabled = settings_store.get_flag(db, "auto_update")
            finally:
                db.close()
            if enabled:
                # 直接 git fetch + 快进即可判断并更新，不依赖 GitHub API 令牌
                apply_update(restart=True, trigger="auto")
        except Exception:  # noqa: BLE001 - 后台任务不能把服务带崩
            pass
        time.sleep(6 * 60 * 60)
