"""发信：一条通道接口，两套实现（BillionMail 发件 API / SMTP）。

为什么不用第三方库：项目只装 fastapi / SQLAlchemy / PyMySQL / cryptography /
Pillow / mutagen / qrcode 这几个包，出网请求一律走标准库，省掉一个依赖就少一处
供应链风险。BillionMail 用 `urllib.request`，SMTP 用 `smtplib`。

两条通道怎么选：看配置里的 ``provider``，参数是否齐由 `mail_config.ready()` 判定。

- ``billionmail``：``template`` 模式下主题、正文、发件人都绑在 BillionMail 后台
  的「API」里，模板用 ``{{.API.<变量名>}}`` 插值，这边只传 ``attribs``；
  ``direct`` 模式才由调用方提供 subject / html。
- ``smtp``：465 隐式 SSL（``SMTP_SSL``）、587 明文起手再 STARTTLS、25 明文。

失败一律抛 `MailError`，``message`` 是可以直接显示给管理员看的中文。
"""

from __future__ import annotations

import json
import logging
import re
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr

from . import mail_config

log = logging.getLogger("uvicorn.error").getChild("cloudnetp.mail_service")

HTTP_TIMEOUT = 15
SMTP_TIMEOUT = 15
MAX_RESPONSE = 65536

# 与歌词库/看板娘一致：直连上游，不走系统代理（本机代理对 TLS 上游常超时）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class MailError(RuntimeError):
    """发信失败。``message`` 可直接展示。"""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def send(
    to: str,
    subject: str,
    html: str,
    text: str | None = None,
    *,
    config: dict | None = None,
    variables: dict | None = None,
) -> dict:
    """发一封信。

    成功返回 ``{"ok": True, "provider": str, "elapsedMs": int}``；
    失败抛 `MailError`。

    ``variables`` 只给 BillionMail 模板模式用（``{"code": "123456"}``），
    直传模式与 SMTP 模式忽略它（正文里已经写好了）。
    """
    cfg = config or mail_config.resolved()
    if not mail_config.ready(cfg):
        raise MailError("邮件服务还没配置完整，请先在「邮箱服务」里补齐发件参数")
    if not (to or "").strip():
        raise MailError("缺少收件地址")

    started = time.perf_counter()
    provider = cfg.get("provider")
    if provider == "billionmail":
        _send_billionmail(cfg, to.strip(), subject, html, variables or {})
    elif provider == "smtp":
        _send_smtp(cfg, to.strip(), subject, html, text)
    else:
        raise MailError(f"未知的发件通道：{provider}")

    elapsed = int((time.perf_counter() - started) * 1000)
    log.info("[mail] 已发送 %s via %s（%sms）", _mask_addr(to), provider, elapsed)
    return {"ok": True, "provider": provider, "elapsedMs": elapsed}


# --------------------------------------------------------------------------- #
# 可测性钩子：测试只替换这两个函数就能拦掉所有出网
# --------------------------------------------------------------------------- #
def _open_url(req: urllib.request.Request, timeout: int):
    return _opener.open(req, timeout=timeout)


def _smtp_client(cfg: dict):
    """按安全模式连上 SMTP。抽成函数是为了测试时可以整个替换掉。"""
    host = (cfg.get("smtpHost") or "").strip()
    if not host:
        raise MailError("请先填写 SMTP 主机")
    try:
        port = int(cfg.get("smtpPort") or 465)
    except (TypeError, ValueError):
        port = 465
    security = cfg.get("smtpSecurity") or "ssl"
    context = ssl.create_default_context()

    if security == "ssl":  # 465：先握手再说话
        return smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT, context=context)

    client = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
    if security == "starttls":  # 587：明文起手，STARTTLS 后要再 ehlo 一次
        client.ehlo()
        client.starttls(context=context)
        client.ehlo()
    return client


# --------------------------------------------------------------------------- #
# BillionMail
# --------------------------------------------------------------------------- #
def _billionmail_url(cfg: dict) -> str:
    mode = cfg.get("bmMode") or "template"
    path = (cfg.get("bmTemplatePath") if mode == "template" else cfg.get("bmDirectPath")) or ""
    path = path.strip()
    if "://" in path:  # 允许直接写完整 URL
        return path
    base = (cfg.get("bmBase") or "").strip().rstrip("/")
    if not base:
        raise MailError("请先填写 BillionMail 地址")
    return f"{base}/{path.lstrip('/')}"


def _billionmail_payload(
    cfg: dict, to: str, subject: str, html: str, variables: dict
) -> dict:
    addresser = (cfg.get("bmAddresser") or "").strip() or (cfg.get("fromAddr") or "").strip()
    if (cfg.get("bmMode") or "template") == "template":
        # 主题/正文/发件人由 BillionMail 后台的 API 模板决定，这里只传变量
        attribs = {str(k): str(v) for k, v in variables.items()}
        code_var = (cfg.get("bmCodeVar") or "code").strip() or "code"
        if "code" in attribs:
            attribs[code_var] = attribs.pop("code")
        payload: dict = {"recipient": to, "attribs": attribs}
    else:
        payload = {"from": addresser, "to": to, "subject": subject, "html": html}
    if addresser:
        payload["addresser"] = addresser  # 两种模式都带上，服务端会忽略多余字段
    return payload


def _error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(2048).decode("utf-8", "replace").strip()
    except Exception:  # pragma: no cover - 读不出 body 就用原因短语
        raw = ""
    if not raw:
        return str(exc.reason or "")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:200]
    if isinstance(data, dict):
        return str(data.get("msg") or data.get("message") or raw[:200])
    return raw[:200]


def _send_billionmail(
    cfg: dict, to: str, subject: str, html: str, variables: dict
) -> None:
    url = _billionmail_url(cfg)
    if not (cfg.get("bmKey") or "").strip():
        raise MailError("请先填写 BillionMail 的 API Key")
    body = json.dumps(
        _billionmail_payload(cfg, to, subject, html, variables), ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Accept", "application/json")
    # 两派鉴权头都带上：多余的头服务端会忽略，省得再让管理员选一次
    req.add_header("X-API-Key", cfg["bmKey"])
    req.add_header("Authorization", f"Bearer {cfg['bmKey']}")

    try:
        resp = _open_url(req, HTTP_TIMEOUT)
    except urllib.error.HTTPError as exc:
        raise MailError(f"BillionMail 拒绝请求（HTTP {exc.code}）：{_error_body(exc)}") from None
    except urllib.error.URLError as exc:
        raise MailError(f"连不上 BillionMail：{getattr(exc, 'reason', exc)}") from None
    except (TimeoutError, OSError) as exc:
        raise MailError(f"BillionMail 响应超时或连接中断：{exc}") from None

    try:
        raw = resp.read(MAX_RESPONSE).decode("utf-8", "replace")
    finally:
        try:
            resp.close()
        except Exception:  # pragma: no cover
            pass

    data: dict = {}
    if raw.strip().startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    if data.get("success") is False:
        raise MailError(f"BillionMail 返回失败：{data.get('msg') or raw[:200]}")
    code = data.get("code")
    if code not in (None, 0, "0", 200, "200"):
        raise MailError(f"BillionMail 返回错误码 {code}：{data.get('msg') or raw[:200]}")


# --------------------------------------------------------------------------- #
# SMTP
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<\s*(br|/p|/div|/tr)\s*/?\s*>", re.I)


def html_to_text(html: str) -> str:
    """粗略把 HTML 转成纯文本，只用于生成「纯文本兜底」那一段。"""
    text = _BR_RE.sub("\n", html or "")
    text = _TAG_RE.sub("", text)
    text = text.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _build_message(
    cfg: dict, to: str, subject: str, html: str, text: str | None
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject  # EmailMessage 的 policy 会自动做 RFC2047 编码
    msg["From"] = formataddr(
        ((cfg.get("fromName") or "").strip(), (cfg.get("fromAddr") or "").strip())
    )
    msg["To"] = to
    plain = (text or "").strip() or html_to_text(html) or subject
    msg.set_content(plain)  # 纯文本必须是第一段
    if (html or "").strip():
        msg.add_alternative(html, subtype="html")
    return msg


def _send_smtp(
    cfg: dict, to: str, subject: str, html: str, text: str | None
) -> None:
    try:
        client = _smtp_client(cfg)
    except MailError:
        raise
    except smtplib.SMTPAuthenticationError:
        raise MailError("SMTP 认证失败：请检查账号与授权码") from None
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise MailError(
            f"连不上 SMTP 服务器（{cfg.get('smtpHost')}:{cfg.get('smtpPort')}）：{exc}"
        ) from None

    try:
        if cfg.get("smtpUser"):
            try:
                client.login(cfg["smtpUser"], cfg.get("smtpPass") or "")
            except smtplib.SMTPAuthenticationError:
                raise MailError("SMTP 认证失败：多数邮箱要填「授权码」而不是登录密码") from None
        client.send_message(_build_message(cfg, to, subject, html, text))
    except MailError:
        raise
    except smtplib.SMTPRecipientsRefused:
        raise MailError("收件地址被服务器拒绝") from None
    except smtplib.SMTPSenderRefused:
        raise MailError("发件地址被服务器拒绝：检查发件地址是否属于该账号") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"SMTP 发送失败：{exc}") from None
    finally:
        try:
            client.quit()
        except Exception:  # pragma: no cover - 已经断了就无所谓
            pass


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _mask_addr(addr: str) -> str:
    name, _, domain = (addr or "").partition("@")
    if not domain:
        return "***"
    return f"{name[:2]}***@{domain}"
