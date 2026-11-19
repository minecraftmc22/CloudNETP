# -*- coding: utf-8 -*-
"""「管理后台 → 邮箱服务」的离线联调脚本。

分两段跑，全程不碰真实库、不发真实邮件：

  1. 进程内验证发信实现：BillionMail 两种调用形态、SMTP 三种加密方式、
     EmailMessage 的中文主题与 From 头；
  2. 复制 dev.db 起独立实例 + 一个假的 BillionMail HTTP 服务，跑管理接口、
     测试发信、验证码真发、发信失败回滚、清空覆盖。

    ./.venv/Scripts/python.exe scripts/mail_admin_smoke.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import smtplib
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from email import message_from_bytes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
BM_KEY = "bm-smoke-key-0123456789"

passed = 0
failed = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {label} {extra}")
    else:
        failed += 1
        print(f"  FAIL {label} {extra}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------- #
# 第一段：进程内验证发信实现
# --------------------------------------------------------------------------- #
class FakeSMTP:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.ehlo_calls = 0
        self.starttls_called = False
        self.login_with: tuple | None = None
        self.message = None

    def ehlo(self) -> None:
        self.ehlo_calls += 1

    def starttls(self, context=None) -> None:  # noqa: ANN001 - 与标准库签名一致
        self.starttls_called = True

    def login(self, user: str, password: str) -> None:
        self.login_with = (user, password)

    def send_message(self, message) -> None:  # noqa: ANN001
        self.message = message

    def quit(self) -> None:
        pass


class FakeSmtplib:
    """只实现 mail_service 用到的那几个名字。"""

    SMTPException = smtplib.SMTPException
    SMTPAuthenticationError = smtplib.SMTPAuthenticationError
    SMTPRecipientsRefused = smtplib.SMTPRecipientsRefused
    SMTPSenderRefused = smtplib.SMTPSenderRefused

    def __init__(self) -> None:
        self.made: list[FakeSMTP] = []

    def SMTP(self, host, port, timeout=None):  # noqa: N802 - 与标准库同名
        client = FakeSMTP("SMTP")
        client.host, client.port = host, port
        self.made.append(client)
        return client

    def SMTP_SSL(self, host, port, timeout=None, context=None):  # noqa: N802
        client = FakeSMTP("SMTP_SSL")
        client.host, client.port = host, port
        self.made.append(client)
        return client


def offline_checks() -> None:
    from app import mail_service

    print("\n[A] 进程内：BillionMail 两种调用形态")
    captured: dict = {}

    class Resp:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def read(self, _n: int) -> bytes:
            return self.body

        def close(self) -> None:
            pass

    def fake_open(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return Resp(STATE["reply"])

    original_open = mail_service._open_url
    mail_service._open_url = fake_open
    STATE["reply"] = b'{"success": true, "code": 0, "msg": "ok"}'

    base_cfg = {
        "enabled": True,
        "provider": "billionmail",
        "fromName": "云盘",
        "fromAddr": "no-reply@example.com",
        "bmBase": "https://mail.example.com",
        "bmTemplatePath": "/api/batch_mail/api/send",
        "bmDirectPath": "/api/send",
        "bmMode": "template",
        "bmKey": BM_KEY,
        "bmAddresser": "",
        "bmCodeVar": "code",
        "smtpHost": "",
        "smtpPort": 465,
        "smtpUser": "",
        "smtpPass": "",
        "smtpSecurity": "ssl",
    }

    result = mail_service.send(
        "user@example.com", "主题", "<p>hi</p>", "hi", config=dict(base_cfg), variables={"code": "123456"}
    )
    check("模板模式发送成功", result.get("ok") is True, str(result))
    check("走的是模板路径", captured["url"].endswith("/api/batch_mail/api/send"), captured["url"])
    check("body 带 recipient", captured["body"].get("recipient") == "user@example.com")
    check("body 的 attribs 带验证码", captured["body"].get("attribs") == {"code": "123456"}, str(captured["body"]))
    check("X-API-Key 头正确", captured["headers"].get("x-api-key") == BM_KEY)
    check("Bearer 头也带了", captured["headers"].get("authorization") == f"Bearer {BM_KEY}")
    check("模板模式不发 subject", "subject" not in captured["body"])

    cfg = dict(base_cfg, bmMode="direct")
    mail_service.send("user@example.com", "直传主题", "<p>hi</p>", "hi", config=cfg)
    check("直传路径正确", captured["url"].endswith("/api/send"), captured["url"])
    body = captured["body"]
    check("直传 body 有 subject", body.get("subject") == "直传主题", str(body.get("subject")))
    check("直传 body 有 html", body.get("html") == "<p>hi</p>")
    check("直传 body 有 from", body.get("from") == "no-reply@example.com")

    cfg = dict(base_cfg, bmCodeVar="vcode")
    mail_service.send("user@example.com", "t", "<p>h</p>", "h", config=cfg, variables={"code": "999111"})
    check("变量名可改", captured["body"].get("attribs") == {"vcode": "999111"}, str(captured["body"].get("attribs")))

    STATE["reply"] = b'{"success": false, "code": 1, "msg": "sender rejected"}'
    try:
        mail_service.send("user@example.com", "t", "<p>h</p>", "h", config=dict(base_cfg))
        check("success=false 要报错", False)
    except mail_service.MailError as exc:
        check("success=false 会抛出并带上原因", "sender rejected" in exc.message, exc.message)

    STATE["reply"] = b'{"code": 500, "msg": "internal"}'
    try:
        mail_service.send("user@example.com", "t", "<p>h</p>", "h", config=dict(base_cfg))
        check("code=500 要报错", False)
    except mail_service.MailError as exc:
        check("非 0 code 会抛出", "500" in exc.message, exc.message)

    mail_service._open_url = original_open

    print("\n[B] 进程内：SMTP 三种加密方式与邮件头")
    fake = FakeSmtplib()
    original_smtplib = mail_service.smtplib
    mail_service.smtplib = fake
    try:
        for security, kind in (("ssl", "SMTP_SSL"), ("starttls", "SMTP"), ("none", "SMTP")):
            client = mail_service._smtp_client(
                {"smtpHost": "smtp.example.com", "smtpPort": 465, "smtpSecurity": security}
            )
            check(f"{security} 用 {kind}", client.kind == kind, client.kind)
            if security == "starttls":
                check("STARTTLS 前后各 ehlo 一次", client.ehlo_calls == 2 and client.starttls_called, str(client.ehlo_calls))
            if security == "none":
                check("明文模式不 starttls", not client.starttls_called)

        cfg = dict(
            base_cfg,
            provider="smtp",
            smtpHost="smtp.example.com",
            smtpPort=465,
            smtpUser="u@example.com",
            smtpPass="secret",
            smtpSecurity="ssl",
        )
        mail_service.send("user@example.com", "【云盘】注册验证码", "<p>hi</p>", "纯文本正文", config=cfg)
        client = fake.made[-1]
        check("SMTP 登录带上了账号", client.login_with == ("u@example.com", "secret"), str(client.login_with))
        raw = client.message.as_bytes()
        parsed = message_from_bytes(raw)
        subject = str(parsed["Subject"])
        check("中文主题被正确编码", "注册验证码" in decoded(subject), subject)
        check("From 头带发件人名称", "云盘" in decoded(str(parsed["From"])), str(parsed["From"]))
        check("纯文本段落存在", "纯文本正文" in client.message.get_body(("plain",)).get_content(), "")
    finally:
        mail_service.smtplib = original_smtplib


def decoded(header: str) -> str:
    from email.header import decode_header, make_header

    try:
        return str(make_header(decode_header(header)))
    except Exception:  # pragma: no cover
        return header


# --------------------------------------------------------------------------- #
# 假 BillionMail
# --------------------------------------------------------------------------- #
STATE: dict = {"mode": "ok", "requests": [], "reply": b'{"success": true, "code": 0}'}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:  # pragma: no cover
            body = {"raw": raw.decode("utf-8", "replace")}
        STATE["requests"].append(
            {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
        )
        mode = STATE["mode"]
        if mode == "error500":
            self.send_response(500)
            payload = {"msg": "boom"}
        elif mode == "fail":
            self.send_response(200)
            payload = {"success": False, "code": 1, "msg": "sender rejected"}
        else:
            self.send_response(200)
            payload = {"success": True, "code": 0, "msg": "Email sent successfully"}
        data = json.dumps(payload).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # 别把请求日志刷到终端
        pass


def start_fake_bm() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# --------------------------------------------------------------------------- #
# 起应用实例
# --------------------------------------------------------------------------- #
def snapshot_db(dst: Path) -> None:
    src_path = ROOT / "storage" / "db" / "fallback.db"
    if not src_path.is_file():
        src_path = ROOT / "dev.db"
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        src.close()
        out.close()
    # 真实库里的邮箱配置会盖掉脚本要测的那一套，先删掉
    conn = sqlite3.connect(str(dst))
    try:
        conn.execute("DELETE FROM system_settings WHERE k = 'mail_config'")
        conn.commit()
    finally:
        conn.close()


def call(base: str, method: str, path: str, cookie: str | None = None, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", f"cloudnetp_session={cookie}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"detail": raw}
        return exc.code, payload


def raw_get(base: str, path: str, cookie: str | None = None):
    """下载类接口：返回原始文本，不按 JSON 解析。"""
    req = urllib.request.Request(f"{base}{path}", method="GET")
    if cookie:
        req.add_header("Cookie", f"cloudnetp_session={cookie}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.headers, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode("utf-8", "replace")


def wait_up(base: str, proc: subprocess.Popen, log_path: Path) -> bool:
    for _ in range(80):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"{base}/api/version", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    print(log_path.read_text(encoding="utf-8", errors="replace")[-1500:])
    return False


def main() -> int:
    offline_checks()

    from app.security import create_token

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-mail-")).resolve()
    db_copy = tmp / "smoke.db"
    snapshot_db(db_copy)

    con = sqlite3.connect(str(db_copy))
    row = con.execute(
        "select id, token_version from users where role = 1 or is_super = 1 order by id limit 1"
    ).fetchone()
    con.close()
    if not row:
        raise SystemExit("库里没有管理员，先手动建一个再来跑这个脚本")
    admin_cookie = create_token(int(row[0]), int(row[1]))

    from app.config import settings

    bm_server, bm_base = start_fake_bm()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db_copy.as_posix()}",
            "STORAGE_DIR": str(tmp / "storage"),
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "RELOAD": "false",
            "DB_PROBE_INTERVAL": "0",
            "DB_MIRROR_INTERVAL": "0",
            "DB_FALLBACK_ENABLED": "0",
            "SECRET_KEY": settings.SECRET_KEY,
            # .env 里一条邮件配置都不给，全部走后台保存
            "MAIL_ENABLED": "",
            "MAIL_PROVIDER": "",
            "MAIL_HOST": "",
            "MAIL_USER": "",
            "MAIL_PASSWORD": "",
            "MAIL_FROM": "",
            "MAIL_BM_BASE": "",
            "MAIL_BM_KEY": "",
        }
    )
    log_path = tmp / "server.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "main.py"], cwd=str(ROOT), env=env,
        stdout=log_file, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来，看日志：" + str(log_path))

        print("\n[C] 初始状态：什么都没配")
        status, data = call(base, "GET", "/api/admin/mail", admin_cookie)
        cfg = data.get("config") or {}
        check("管理接口 200", status == 200, str(status))
        check("ready=false", cfg.get("ready") is False, str(cfg.get("ready")))
        check("enabled 默认关", cfg.get("enabled") is False, str(cfg.get("enabled")))
        check("默认通道是 BillionMail", cfg.get("provider") == "billionmail", str(cfg.get("provider")))
        check("模板路径有默认值", (cfg.get("billionmail") or {}).get("templatePath") == "/api/batch_mail/api/send")
        _, meta = call(base, "GET", "/api/meta")
        check("/api/meta 的 mailReady=false", meta.get("mailReady") is False, str(meta.get("mailReady")))
        status, err = call(
            base, "POST", "/api/auth/email/code", None,
            {"scene": "bind", "target": "nobody@example.com", "device": "smoke-device-x"},
        )
        check("没配好时发码 503", status == 503, str(status))

        print("\n[D] 保存 BillionMail 配置")
        status, data = call(
            base, "PUT", "/api/admin/mail", admin_cookie,
            {
                "enabled": True,
                "provider": "billionmail",
                "fromName": "CloudNETP 测试",
                "fromAddr": "no-reply@example.com",
                "bmBase": bm_base,
                "bmKey": BM_KEY,
            },
        )
        cfg = data.get("config") or {}
        check("保存 200", status == 200, str(status))
        check("ready=true", cfg.get("ready") is True, str(cfg.get("ready")))
        bm = cfg.get("billionmail") or {}
        check("密钥只回掩码", bool(bm.get("keyMask")), str(bm.get("keyMask")))
        check("hasKey=true", bm.get("hasKey") is True)
        check("响应不含密钥原文", BM_KEY not in json.dumps(data, ensure_ascii=False))
        check("地址来源标 admin", bm.get("baseSource") == "admin", str(bm.get("baseSource")))
        _, meta = call(base, "GET", "/api/meta")
        check("/api/meta 的 mailReady=true", meta.get("mailReady") is True, str(meta.get("mailReady")))

        conn = sqlite3.connect(str(db_copy))
        stored = conn.execute("select v from system_settings where k = 'mail_config'").fetchone()
        conn.close()
        check("库里存的是密文", bool(stored) and "enc:" in stored[0], (stored or [""])[0][:60])
        check("库里没有密钥原文", not stored or BM_KEY not in stored[0])

        print("\n[E] 测试发信（模板模式，用未保存的值）")
        STATE["requests"].clear()
        status, data = call(
            base, "POST", "/api/admin/mail/test", admin_cookie,
            {"to": "check@example.com", "received": True},
        )
        check("测试接口 200", status == 200, str(status))
        check("返回 provider", data.get("provider") == "billionmail", str(data.get("provider")))
        check("返回耗时", isinstance(data.get("elapsedMs"), int), str(data.get("elapsedMs")))
        check("假服务收到一次请求", len(STATE["requests"]) == 1, str(len(STATE["requests"])))
        req = STATE["requests"][0] if STATE["requests"] else {}
        check("请求带 X-API-Key", (req.get("headers") or {}).get("x-api-key") == BM_KEY)
        check("收件人正确", (req.get("body") or {}).get("recipient") == "check@example.com", str(req.get("body")))

        print("\n[F] 验证码真发（模板变量）")
        STATE["requests"].clear()
        target = f"smoke-{int(time.time())}@example.com"
        status, data = call(
            base, "POST", "/api/auth/email/code", None,
            {"scene": "bind", "target": target, "device": "smoke-device-1"},
        )
        check("发码 200", status == 200, str(status))
        check("假服务收到邮件", len(STATE["requests"]) == 1, str(len(STATE["requests"])))
        attribs = ((STATE["requests"][0].get("body") or {}).get("attribs") or {}) if STATE["requests"] else {}
        code = attribs.get("code", "")
        check("attribs 里带 code", bool(code) and code.isdigit() and len(code) == 6, str(attribs))
        status, _ = call(
            base, "POST", "/api/auth/email/verify", None,
            {"scene": "bind", "target": target, "code": code},
        )
        check("收到的验证码能通过核验", status == 200, str(status))

        print("\n[G] 切到直传模式")
        status, data = call(
            base, "PUT", "/api/admin/mail", admin_cookie, {"bmMode": "direct"}
        )
        check("切模式 200", status == 200, str(status))
        check("mode=direct", ((data.get("config") or {}).get("billionmail") or {}).get("mode") == "direct")
        STATE["requests"].clear()
        status, _ = call(
            base, "POST", "/api/admin/mail/test", admin_cookie, {"to": "check@example.com"}
        )
        check("直传测试 200", status == 200, str(status))
        body = (STATE["requests"][0].get("body") or {}) if STATE["requests"] else {}
        check("直传 body 有 subject", "邮件服务测试" in str(body.get("subject")), str(body.get("subject")))
        check("直传 body 有 html", "<p>" in str(body.get("html")))

        print("\n[H] 发信失败要把冷却回滚掉")
        STATE["mode"] = "error500"
        target2 = f"smoke-fail-{int(time.time())}@example.com"
        status1, data1 = call(
            base, "POST", "/api/auth/email/code", None,
            {"scene": "bind", "target": target2, "device": "smoke-device-2"},
        )
        check("发信失败返回 502", status1 == 502, str(status1))
        check("提示带上原因", "发送失败" in str(data1.get("detail")), str(data1.get("detail"))[:80])
        status2, data2 = call(
            base, "POST", "/api/auth/email/code", None,
            {"scene": "bind", "target": target2, "device": "smoke-device-2"},
        )
        check("立刻重试不被 30 秒冷却拦住", status2 == 502, str(status2))
        STATE["mode"] = "ok"

        print("\n[I] 清空覆盖回到 .env")
        status, data = call(base, "DELETE", "/api/admin/mail", admin_cookie)
        cfg = data.get("config") or {}
        check("清空 200", status == 200, str(status))
        check("ready 回 false", cfg.get("ready") is False, str(cfg.get("ready")))
        check("密钥没了", (cfg.get("billionmail") or {}).get("hasKey") is False)
        _, meta = call(base, "GET", "/api/meta")
        check("/api/meta 的 mailReady 回 false", meta.get("mailReady") is False, str(meta.get("mailReady")))
        conn = sqlite3.connect(str(db_copy))
        left = conn.execute("select v from system_settings where k = 'mail_config'").fetchone()
        conn.close()
        check("库里已清空", not left or not left[0], str(left))

        print("\n[J] 地址格式校验")
        status, data = call(
            base, "PUT", "/api/admin/mail", admin_cookie, {"bmBase": "mail.example.com"}
        )
        check("没有协议头要 400", status == 400, str(status))
        check("提示说明要 http(s)", "http" in str(data.get("detail")), str(data.get("detail")))

        print("\n[K] 审计日志留痕")
        _, data = call(base, "GET", "/api/admin/logs?page=1", admin_cookie)
        actions = {item.get("action") for item in (data.get("items") or [])}
        check("有 admin_mail_save", "admin_mail_save" in actions, str(sorted(a for a in actions if a))[:200])
        check("有 admin_mail_test", "admin_mail_test" in actions)
        check("有 admin_mail_clear", "admin_mail_clear" in actions)

        print("\n[L] 下载 BillionMail 模板")
        status, headers, html = raw_get(base, "/api/admin/mail/template", admin_cookie)
        check("下载 200", status == 200, str(status))
        check("是 HTML", "text/html" in (headers.get("Content-Type") or ""), str(headers.get("Content-Type")))
        check(
            "带 attachment 文件名",
            "cloudnetp-billionmail-template.html" in (headers.get("Content-Disposition") or ""),
            str(headers.get("Content-Disposition")),
        )
        check("占位符是 {{.API.code}}", "{{.API.code}}" in html)
        check("与真实邮件同一套版式", "<!doctype html>" in html and "max-width:600px" in html)
        check("注释里写了粘贴位置", "BillionMail 后台" in html)

        status, _, html = raw_get(base, "/api/admin/mail/template?codeVar=vcode", admin_cookie)
        check("变量名可跟随卡片", status == 200 and "{{.API.vcode}}" in html, str(status))

        status, _, body = raw_get(base, "/api/admin/mail/template?codeVar=1bad-name", admin_cookie)
        check("非法变量名要 400", status == 400, str(status))
        check("非法提示可读", "变量名" in body, body[:80])

        status, _, _ = raw_get(base, "/api/admin/mail/template", None)
        check("未登录不能下载", status in (401, 404), str(status))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        bm_server.shutdown()
        bm_server.server_close()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failed:
        print(f"失败 {failed} 项，通过 {passed} 项")
        return 1
    print(f"全部通过（{passed} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
