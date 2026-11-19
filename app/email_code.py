"""邮箱验证码：发码、发信与核验全部在服务端。

发信走 `mail_service`（BillionMail 发件 API 或 SMTP），通道与凭据在
「管理后台 → 邮箱服务」里配；没配好时接口直接回「未配置」，不会白烧配额。

规则（按需求实现）：
1. 同一目标（邮箱/场景）两次发送间隔不得小于 30 秒；
2. 同一设备累计发满 10 次后，冷却 1 小时；
3. 每发一次新码，前一个码立即作废；
4. 核验只在服务端做，前端拿不到也无法伪造；
5. 码 10 分钟过期，最多试 5 次，成功即销毁；
6. 发信失败要把刚写入的码与设备计数都撤回，别让用户被冷却卡住。
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

from . import mail_service
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
    """发码被拒绝。retry_after 是建议前端倒计时的秒数，status 给路由层定 HTTP 码。"""

    def __init__(self, message: str, retry_after: int = 0, status: int = 429) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after
        self.status = status


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


_SCENE_SUBJECT = {
    "register": "注册验证码",
    "bind": "绑定邮箱验证码",
    "reset": "重置密码验证码",
}
_SCENE_ACTION = {
    "register": "注册账号",
    "bind": "绑定邮箱",
    "reset": "重置密码",
}


def _code_text(action: str, code: str) -> str:
    minutes = CODE_TTL_SECONDS // 60
    return (
        f"你正在{action}，验证码：{code}\n"
        f"{minutes} 分钟内有效，只能用一次。\n"
        "如果这不是你本人操作，忽略这封邮件即可，你的账号不会有变化。\n"
        f"—— {settings.APP_NAME}"
    )


def _code_html(action: str, code: str) -> str:
    """验证码邮件正文。

    邮件客户端不认 CSS 变量与外链样式，全部内联；按 600px 卡片排版，
    深色模式下也会保持可读（只用了浅底 + 深字）。
    """
    minutes = CODE_TTL_SECONDS // 60
    site = settings.APP_NAME
    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f6f7fb;font-family:-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,'PingFang SC','Microsoft YaHei',sans-serif;">
  <div style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:16px;padding:32px 28px;color:#1f2430;">
    <div style="font-size:15px;font-weight:600;color:#ff7aa8;margin-bottom:18px;">{site}</div>
    <div style="font-size:16px;line-height:1.7;">你正在{action}，验证码是：</div>
    <div style="margin:18px 0;padding:16px;text-align:center;background:#fff0f5;border-radius:12px;font-size:30px;font-weight:700;letter-spacing:8px;color:#e34f86;">{code}</div>
    <div style="font-size:13px;line-height:1.8;color:#6b7280;">
      {minutes} 分钟内有效，只能用一次。<br>
      如果这不是你本人操作，忽略这封邮件即可，你的账号不会有变化。
    </div>
  </div>
  <div style="max-width:600px;margin:14px auto 0;font-size:12px;color:#9aa1ad;text-align:center;">
    这封邮件由 {site} 自动发送，请勿直接回复。
  </div>
</body></html>"""


def billionmail_template(code_var: str = "code") -> str:
    """给 BillionMail 后台「API」模板用的 HTML 正文。

    与真实验证码邮件同一套版式，只是把验证码换成 ``{{.API.<变量名>}}`` 占位符。
    变量名必须跟卡片里的「验证码变量名」一致，否则发信时插不进去。
    """
    var = (code_var or "code").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,39}", var):
        raise ValueError("变量名只能包含字母、数字和下划线，且不能以数字开头")
    site = settings.APP_NAME
    header = (
        "<!--\n"
        f"  {site} 验证码邮件模板（BillionMail 模板 API 专用）\n"
        "  1. BillionMail 后台 → API → 打开你的 API → 模板内容，把本文件整段粘进去\n"
        f"  2. {{{{.API.{var}}}}} 会被本站传来的验证码替换；卡片里改过「验证码变量名」就重新下载一份\n"
        f"  3. 主题在 BillionMail 那边单独填，建议：【{site}】验证码\n"
        "-->\n"
    )
    return header + _code_html("完成一项验证操作", "{{.API." + var + "}}")


def deliver(to: str, scene: str, code: str) -> None:
    """真正发信。

    走 `mail_service`：通道（BillionMail / SMTP）由后台配置决定。
    成功返回 None，失败抛 `mail_service.MailError`，由 `issue()` 回滚并转成
    `SendError`（502），调用方拿到的是可读的中文原因。
    """
    action = _SCENE_ACTION.get(scene, "验证操作")
    subject = f"【{settings.APP_NAME}】{_SCENE_SUBJECT.get(scene, '验证码')}"
    mail_service.send(
        to,
        subject,
        _code_html(action, code),
        _code_text(action, code),
        variables={"code": code},
    )


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

    # 网络调用放在锁外：SMTP/HTTP 最长 15 秒，不能把别的发码请求堵在门外
    try:
        deliver(target, scene, code)
    except mail_service.MailError as exc:
        log.warning("[mail] 发信失败，已撤销本次验证码：%s", exc.message)
        _rollback(key, dev, code, now)
        raise SendError(f"验证码邮件发送失败：{exc.message}", 0, status=502) from None
    return RESEND_COOLDOWN


def _rollback(key: str, dev: str, code: str, sent_at: float) -> None:
    """发信失败：把刚写入的验证码与设备计数撤掉，让用户可以立刻重试。

    网络调用在锁外，这期间同一设备可能已经给别的目标签发了新码，
    所以只能按「本条码」精确删除，绝不能整表清空，也不能把设备计数整个丢掉
    （那是限流依据）。
    """
    with _lock:
        current = _codes.get(key)
        if current is not None and current.code == code:
            _codes.pop(key, None)
        stamps = _device_log.get(dev)
        if stamps:
            try:
                stamps.remove(sent_at)
            except ValueError:
                pass
            if not stamps:
                _device_log.pop(dev, None)


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
