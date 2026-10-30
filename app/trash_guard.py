"""回收站关闭操作的二次确认码。

服务器生成随机码，前端只负责展示与回填，核验完全在服务器侧完成：
- 一次性：验证成功立即作废
- 有效期：默认 5 分钟
- 尝试上限：默认 5 次，超限作废，防止暴力猜
- 申请限流：60 秒内最多 5 次，避免被当成验证码工厂
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

# 去掉容易看错的 0/O/1/I/L
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 6
CODE_TTL_SECONDS = 300
MAX_ATTEMPTS = 5
ISSUE_WINDOW_SECONDS = 60
ISSUE_LIMIT = 5

_lock = threading.Lock()
_codes: dict[int, "Challenge"] = {}
_issue_log: dict[int, list[float]] = {}


@dataclass
class Challenge:
    code: str
    expires_at: float
    attempts: int = field(default=0)


class TooManyRequests(Exception):
    """申请验证码过于频繁。"""


def _purge_expired(now: float) -> None:
    for user_id, item in list(_codes.items()):
        if item.expires_at <= now:
            _codes.pop(user_id, None)


def issue(user_id: int) -> Challenge:
    """为指定用户签发一个新验证码（旧的作废）。"""
    now = time.time()
    with _lock:
        _purge_expired(now)

        recent = [t for t in _issue_log.get(user_id, []) if now - t < ISSUE_WINDOW_SECONDS]
        if len(recent) >= ISSUE_LIMIT:
            raise TooManyRequests
        recent.append(now)
        _issue_log[user_id] = recent

        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        item = Challenge(code=code, expires_at=now + CODE_TTL_SECONDS)
        _codes[user_id] = item
        return item


def verify(user_id: int, code: str) -> tuple[bool, str]:
    """核验验证码。返回 (是否通过, 失败原因)。通过后立即作废。"""
    now = time.time()
    with _lock:
        item = _codes.get(user_id)
        if item is None:
            return False, "验证码已失效，请重新获取"
        if item.expires_at <= now:
            _codes.pop(user_id, None)
            return False, "验证码已过期，请重新获取"

        item.attempts += 1
        if item.attempts > MAX_ATTEMPTS:
            _codes.pop(user_id, None)
            return False, "尝试次数过多，请重新获取验证码"

        if not secrets.compare_digest(item.code, (code or "").strip().upper()):
            left = MAX_ATTEMPTS - item.attempts
            return False, f"验证码不正确，还可尝试 {max(left, 0)} 次"

        _codes.pop(user_id, None)
        return True, ""


def revoke(user_id: int) -> None:
    with _lock:
        _codes.pop(user_id, None)
