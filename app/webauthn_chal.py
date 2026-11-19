"""WebAuthn 的 challenge 缓存（进程内存，一次一用）。

为什么放内存：challenge 只活几十秒、且必须「取一次就失效」，落库既没必要也会
每注册一个通行密钥就写一行垃圾。和 `email_code` 一样按「单实例部署」设计：
多 worker 下 A 进程发 challenge、B 进程验会失败。

注册（reg）与登录断言（auth）用不同命名空间，互相顶不掉。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

CHALLENGE_TTL = 300
MAX_ENTRIES = 1000

_lock = threading.Lock()


@dataclass
class _Challenge:
    value: bytes
    expires_at: float


_items: dict[str, _Challenge] = {}


def _purge(now: float) -> None:
    for key, item in list(_items.items()):
        if item.expires_at <= now:
            _items.pop(key, None)
    if len(_items) > MAX_ENTRIES:
        ordered = sorted(_items.items(), key=lambda kv: kv[1].expires_at)
        for key, _ in ordered[: len(_items) - MAX_ENTRIES]:
            _items.pop(key, None)


def put(kind: str, user_id: int, challenge: bytes) -> str:
    """存下 challenge，返回给前端的 nonce（放进 options 响应里带回）。"""
    nonce = secrets.token_urlsafe(12)
    now = time.time()
    with _lock:
        _purge(now)
        _items[f"{kind}|{user_id}|{nonce}"] = _Challenge(challenge, now + CHALLENGE_TTL)
    return nonce


def pop(kind: str, user_id: int, nonce: str) -> bytes | None:
    """取出并销毁。取不到（过期/伪造/已用）返回 None。"""
    key = f"{kind}|{user_id}|{(nonce or '').strip()}"
    with _lock:
        item = _items.pop(key, None)
    if item is None:
        return None
    return item.value if item.expires_at > time.time() else None


def reset() -> None:
    """清空（测试用）。"""
    with _lock:
        _items.clear()
