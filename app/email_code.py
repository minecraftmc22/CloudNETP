"""邮箱验证码：发码与核验全部在服务端。

邮件服务还没搭建，所以 `deliver()` 现在只会写日志、不发信；但**判定逻辑一步不少**，
接口按最终形态开放，之后填上 SMTP 参数即可直接启用，前端零改动。

规则（按需求实现）：
1. 同一目标（邮箱/场景）两次发送间隔不得小于 30 秒；
2. 同一设备累计发满 10 次后，冷却 1 小时；
3. 每发一次新码，前一个码立即作废；
4. 核验只在服务端做，前端拿不到也无法伪造；
5. 码 10 分钟过期，最多试 5 次，成功即销毁。
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

from .config import settings

CODE_LENGTH = 6
CODE_TTL_SECONDS = 600
MAX_ATTEMPTS = 5

RESEND_COOLDOWN = 30           # 两次发送之间的最小间隔
DEVICE_SEND_LIMIT = 10         # 设备累计发满这个数就开始长冷却
DEVICE_COOLDOWN = 3600         # 长冷却时长
DEVICE_WINDOW = 24 * 3600      # 统计窗口

MAX_ENTRIES = 2000

_lock = threading.Lock()
_codes: dict[str, "Challenge"] = {}
_device_log: dict[str, list[float]] = {}

log = logging.getLogger("uvicorn.error")


class SendError(Exception):
    """发码被拒绝。retry_after 是建议前端倒计时的秒数。"""

    def __init__(self, message: str, retry_after: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


@dataclass
class Challenge:
    code: str
    expires_at: float
    sent_at: float
    attempts: int = field(default=0)


def _key(scene: str, target: str) -> str:
    return f"{scene}|{target.strip().lower()}"


def _purge(now: float) -> None:
    for key, item in list(_codes.items()):
        if item.expires_at <= now:
            _codes.pop(key, None)
    if len(_codes) > MAX_ENTRIES:
        ordered = sorted(_codes.items(), key=lambda kv: kv[1].expires_at)
        for key, _ in ordered[: len(_codes) - MAX_ENTRIES]:
            _codes.pop(key, None)


def deliver(to: str, scene: str, code: str) -> bool:
    """真正发信的位置。

    现在没有邮件服务，只写日志并返回 False；等 SMTP 参数填好，
    在这里接 smtplib 即可，调用方无需改动。
    """
    if not settings.mail_ready:
        log.info("[mail] 未配置邮件服务，跳过发送：%s / %s / %s", scene, to, code[:2] + "****")
        return False
    log.info("[mail] 待接入 SMTP，暂不发送：%s", to)
    return False


def issue(scene: str, target: str, device: str) -> int:
    """签发新验证码，返回本次冷却秒数（用于前端倒计时）。

    失败抛 SendError，带 retry_after。
    """
    now = time.time()
    key = _key(scene, target)
    dev = (device or "unknown")[:64]

    with _lock:
        _purge(now)

        # 规则 3：新码直接覆盖旧的，旧的立即失效
        old = _codes.get(key)
        if old is not None and now - old.sent_at < RESEND_COOLDOWN:
            wait = int(RESEND_COOLDOWN - (now - old.sent_at)) + 1
            raise SendError(f"请等待 {wait} 秒后再发送", wait)

        # 规则 2：同一设备发太多了，冷却 1 小时
        sends = [t for t in _device_log.get(dev, []) if now - t < DEVICE_WINDOW]
        if len(sends) >= DEVICE_SEND_LIMIT:
            last = sends[-1]
            remain = int(DEVICE_COOLDOWN - (now - last))
            if remain > 0:
                raise SendError(
                    f"该设备发送过于频繁，请 {max(1, remain // 60)} 小时后再试", remain
                )
            # 冷却结束后重新计数
            sends = []

        code = "".join(secrets.choice("0123456789") for _ in range(CODE_LENGTH))
        _codes[key] = Challenge(code=code, expires_at=now + CODE_TTL_SECONDS, sent_at=now)
        sends.append(now)
        _device_log[dev] = sends

    deliver(target, scene, code)
    return RESEND_COOLDOWN


def verify(scene: str, target: str, code: str) -> tuple[bool, str]:
    """核验验证码。通过后立即作废。"""
    now = time.time()
    key = _key(scene, target)
    with _lock:
        item = _codes.get(key)
        if item is None:
            return False, "验证码已失效，请重新获取"
        if item.expires_at <= now:
            _codes.pop(key, None)
            return False, "验证码已过期，请重新获取"

        item.attempts += 1
        if item.attempts > MAX_ATTEMPTS:
            _codes.pop(key, None)
            return False, "尝试次数过多，请重新获取"

        if not secrets.compare_digest(item.code, (code or "").strip()):
            left = MAX_ATTEMPTS - item.attempts
            return False, f"验证码不正确，还可尝试 {max(left, 0)} 次"

        _codes.pop(key, None)
        return True, ""


def pending_targets() -> int:
    """调试用：当前待核验的验证码数量。"""
    with _lock:
        return len(_codes)
