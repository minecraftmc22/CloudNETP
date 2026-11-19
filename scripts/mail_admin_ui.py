# -*- coding: utf-8 -*-
"""管理后台「邮箱服务」页签的端到端（假 BillionMail）。

  1. 页签点得开，卡片默认走 BillionMail，SMTP 组是隐藏的
  2. 切换通道只改显隐，切回来已输入的内容还在（不重建 DOM）
  3. 用未保存的值发测试邮件，真的打到配置的地址并带上 X-API-Key
  4. 保存后密钥只回掩码、出现「清除」勾选、页面与库里都没有原文
  5. /api/meta 的 mailReady 与「绑定邮箱」验证码一起生效
  6. 切成直传模式后，请求体里带 subject/html
  7. 「恢复 .env 配置」能清掉后台覆盖
  8. 「下载模板」能拿到带 {{.API.xxx}} 占位的 HTML，变量名校验拦得住非法输入
  9. 英文界面无残留中文、手机宽度不溢出、全程无 JS 报错

跑法：
    ./.venv/Scripts/python.exe scripts/mail_admin_ui.py
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-mail-admin-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'smoke.db').as_posix()}"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ["SECRET_KEY"] = "smoke-secret-key-for-mail-admin"
os.environ["DB_FALLBACK_ENABLED"] = "0"
os.environ["DB_PROBE_INTERVAL"] = "0"
os.environ["ALLOW_REGISTER"] = "1"
# 起点：一条邮件配置都没有（全部走后台保存，验证「未配置 → 已就绪 → 再清空」）
for _key in (
    "MAIL_ENABLED",
    "MAIL_PROVIDER",
    "MAIL_HOST",
    "MAIL_USER",
    "MAIL_PASSWORD",
    "MAIL_FROM",
    "MAIL_BM_BASE",
    "MAIL_BM_KEY",
):
    os.environ[_key] = ""

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BM_KEY = "bm-ui-key-abcdefgh"
PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  OK   {name} {detail}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


class FakeMail(BaseHTTPRequestHandler):
    calls: list[dict] = []
    mode = "ok"

    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {"raw": raw.decode("utf-8", "replace")}
        FakeMail.calls.append(
            {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
        )
        payload = (
            {"success": False, "code": 1, "msg": "sender rejected"}
            if FakeMail.mode == "fail"
            else {"success": True, "code": 0, "msg": "ok"}
        )
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeMail)
threading.Thread(target=fake.serve_forever, daemon=True).start()
FAKE_BASE = f"http://127.0.0.1:{fake.server_address[1]}"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def hide_mascot(page) -> None:
    """看板娘画布常驻右下角，会挡住卡片底部的按钮，测试时先藏起来。"""
    page.evaluate(
        """() => {
            const style = document.createElement('style');
            style.id = 'hide-mascot';
            style.textContent = '#mascot{display:none!important}';
            document.head.appendChild(style);
        }"""
    )


def main() -> None:
    import uvicorn

    from app import db_failover, settings_store
    from app.config import settings
    from app.main import app as fastapi_app
    from app.models import User
    from app.security import create_token, hash_password
    from app.user_service import ensure_settings

    port = free_port()
    cfg = uvicorn.Config(
        fastapi_app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if server.started:
            break
        time.sleep(0.2)
    if not server.started:
        raise SystemExit("服务没起来")

    Factory = db_failover.current_sessionmaker()
    with Factory() as db:
        user = User(
            username="sakura",
            password_hash=hash_password("Cloud@2026"),
            display_name="樱花",
            role=1,
            quota_bytes=1024**3,
            token_version=1,
            created_at=datetime.utcnow(),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        ensure_settings(db, user.id)
        db.commit()
        cookie = create_token(user.id, user.token_version)
        admin_id = user.id

    errors: list[str] = []
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1360, "height": 1100})
            ctx.add_cookies(
                [
                    {
                        "name": settings.COOKIE_NAME,
                        "value": cookie,
                        "domain": "127.0.0.1",
                        "path": "/",
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))

            page.goto(f"{base}/#/admin", wait_until="domcontentloaded")
            page.wait_for_selector("[data-tab='mail']", timeout=20000)
            hide_mascot(page)
            page.click("[data-tab='mail']")
            page.wait_for_selector("[data-card='mail'] [data-mail-save]", timeout=20000)

            print("\n[A] 页签与默认状态")
            check("邮箱服务页签在", page.locator("[data-tab='mail']").count() == 1)
            check("卡片只有一个", page.locator("[data-card='mail']").count() == 1)
            text = page.inner_text("[data-card='mail']")
            check("标题是邮箱服务", "邮箱服务" in text, text.splitlines()[0])
            active = page.evaluate(
                "() => document.querySelector('[data-card=\"mail\"] [data-mail-provider].is-active')?.dataset.mailProvider"
            )
            check("默认通道是 BillionMail", active == "billionmail", active)
            check("SMTP 组默认隐藏", page.locator("[data-mail-group='smtp']").is_hidden())
            check("BillionMail 组可见", page.locator("[data-mail-group='billionmail']").is_visible())
            check("未配置时状态是未配置", "未配置" in text, text[:120])
            meta = page.request.get(f"{base}/api/meta").json()
            check("/api/meta 的 mailReady 为 false", meta.get("mailReady") is False, str(meta.get("mailReady")))

            print("\n[B] 切换通道不丢已输入内容")
            page.click("[data-mail-provider='smtp']")
            check("SMTP 组显示出来了", page.locator("[data-mail-group='smtp']").is_visible())
            check("BillionMail 组隐藏", page.locator("[data-mail-group='billionmail']").is_hidden())
            page.fill("[data-mail-smtp-host]", "smtp.persist.example.com")
            page.click("[data-mail-provider='billionmail']")
            check("切回 BillionMail 后 SMTP 组隐藏", page.locator("[data-mail-group='smtp']").is_hidden())
            page.click("[data-mail-provider='smtp']")
            kept = page.input_value("[data-mail-smtp-host]")
            check("切回来后输入还在", kept == "smtp.persist.example.com", kept)
            page.fill("[data-mail-smtp-host]", "")
            page.click("[data-mail-provider='billionmail']")

            print("\n[C] 用未保存的值发测试邮件")
            page.fill("[data-mail-from-name]", "云盘测试")
            page.fill("[data-mail-from-addr]", "no-reply@example.com")
            page.fill("[data-mail-bm-base]", FAKE_BASE)
            page.fill("[data-mail-bm-key]", BM_KEY)
            page.fill("[data-mail-test-to]", "check@example.com")
            # 开关的 input 是视觉隐藏的（透明覆盖在 label 上），JS 点更稳
            page.evaluate("() => document.querySelector('[data-mail-enabled]').click()")
            check(
                "总开关已打开",
                page.evaluate("() => document.querySelector('[data-mail-enabled]').checked") is True,
            )
            FakeMail.calls.clear()
            page.click("[data-mail-test]")
            page.wait_for_function(
                "() => (document.querySelector('[data-mail-status]')?.textContent || '').includes('billionmail')",
                timeout=20000,
            )
            status = page.inner_text("[data-mail-status]")
            check("测试发信成功", "billionmail" in status, status)
            check("假服务收到请求", len(FakeMail.calls) == 1, len(FakeMail.calls))
            headers = FakeMail.calls[0]["headers"] if FakeMail.calls else {}
            check("请求带 X-API-Key", headers.get("x-api-key") == BM_KEY, headers.get("x-api-key"))
            body = (FakeMail.calls[0]["body"] if FakeMail.calls else {})
            check("收件人正确", body.get("recipient") == "check@example.com", str(body))
            check("模板模式不带 subject", "subject" not in body, list(body))

            print("\n[C2] 下载 BillionMail 模板")
            downloads: list = []
            page.on("download", lambda d: downloads.append(d))
            with page.expect_download() as info:
                page.click("[data-mail-template]")
            tpl_file = info.value
            check(
                "文件名正确",
                tpl_file.suggested_filename == "cloudnetp-billionmail-template.html",
                tpl_file.suggested_filename,
            )
            tpl = Path(tpl_file.path()).read_text(encoding="utf-8")
            check("模板带 {{.API.code}} 占位符", "{{.API.code}}" in tpl)
            check("模板是完整 HTML", "<!doctype html>" in tpl and "max-width:600px" in tpl)
            check("提示已下载", "模板已下载" in page.inner_text("body"))

            page.fill("[data-mail-bm-code-var]", "vcode")
            with page.expect_download() as info:
                page.click("[data-mail-template]")
            tpl2 = Path(info.value.path()).read_text(encoding="utf-8")
            check("变量名跟着输入框走", "{{.API.vcode}}" in tpl2)

            before = len(downloads)
            page.fill("[data-mail-bm-code-var]", "1bad-name")
            page.click("[data-mail-template]")
            page.wait_for_timeout(800)
            check("非法变量名不下载", len(downloads) == before, len(downloads))
            check(
                "非法变量名有提示",
                "变量名只能包含字母、数字和下划线" in page.inner_text("body"),
                page.inner_text("[data-card='mail']")[-160:],
            )
            page.fill("[data-mail-bm-code-var]", "")

            print("\n[D] 保存配置")
            page.click("[data-mail-save]")
            page.wait_for_selector("[data-mail-clear-bm-key]", timeout=20000)
            masked = page.get_attribute("[data-mail-bm-key]", "placeholder")
            check("密钥框只显示掩码", "…efgh" in (masked or ""), masked)
            check("出现清除密钥勾选", page.locator("[data-mail-clear-bm-key]").count() == 1)
            content = page.content()
            check("页面里没有密钥原文", BM_KEY not in content)
            check("保存后状态变成已就绪", "已就绪" in page.inner_text("[data-card='mail']"))
            meta = page.request.get(f"{base}/api/meta").json()
            check("/api/meta 的 mailReady 变成 true", meta.get("mailReady") is True, str(meta.get("mailReady")))

            with Factory() as db:
                stored = settings_store.get_value(db, "mail_config") or ""
            check("库里是密文", "enc:" in stored, stored[:60])
            check("库里没有密钥原文", BM_KEY not in stored)
            check("库里记下了发件人", "no-reply@example.com" in stored)

            page.locator("[data-card='mail']").screenshot(path=str(_TMP / "mail_card_desktop.png"))

            print("\n[E] 绑定邮箱的验证码真的发出去")
            FakeMail.calls.clear()
            target = f"ui-{int(time.time())}@example.com"
            resp = page.request.post(
                f"{base}/api/auth/email/code",
                data={"scene": "bind", "target": target, "device": "ui-device-1"},
            )
            check("发码 200", resp.status == 200, str(resp.status))
            check("假服务收到邮件", len(FakeMail.calls) == 1, len(FakeMail.calls))
            attribs = (FakeMail.calls[0]["body"] if FakeMail.calls else {}).get("attribs") or {}
            check("模板变量里带 code", bool(attribs.get("code")), str(attribs))

            print("\n[F] 切成直传模式")
            page.select_option("[data-mail-bm-mode]", "direct")
            FakeMail.calls.clear()
            page.click("[data-mail-test]")
            page.wait_for_function(
                "() => (document.querySelector('[data-mail-status]')?.textContent || '').includes('billionmail')",
                timeout=20000,
            )
            body2 = FakeMail.calls[0]["body"] if FakeMail.calls else {}
            check("直传带 subject", "邮件服务测试" in str(body2.get("subject")), str(body2.get("subject")))
            check("直传带 html", "<p>" in str(body2.get("html")))
            check("直传带 from", body2.get("from") == "no-reply@example.com", str(body2.get("from")))

            print("\n[G] 恢复 .env 配置")
            page.click("[data-mail-reset]")
            page.wait_for_selector(".dialog [data-act='ok']", timeout=10000)
            page.click(".dialog [data-act='ok']")
            page.wait_for_function(
                "() => !document.querySelector('[data-mail-clear-bm-key]')",
                timeout=20000,
            )
            check("回到未配置", "未配置" in page.inner_text("[data-card='mail']"))
            meta = page.request.get(f"{base}/api/meta").json()
            check("/api/meta 的 mailReady 回 false", meta.get("mailReady") is False, str(meta.get("mailReady")))
            with Factory() as db:
                left = settings_store.get_value(db, "mail_config") or ""
            check("库里已清空", not left, left)

            print("\n[H] 英文界面与手机宽度")
            page.evaluate(
                """async () => {
                    await fetch('/api/me/settings', {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ language: 'en-US' }),
                    });
                }"""
            )
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("[data-tab='mail']", timeout=20000)
            hide_mascot(page)
            page.click("[data-tab='mail']")
            page.wait_for_selector("[data-card='mail'] [data-mail-save]", timeout=20000)
            en_text = page.inner_text("[data-card='mail']")
            leftover = re.findall(r"[\u4e00-\u9fa5]+", en_text)
            check("英文卡片没有残留中文", not leftover, leftover[:6])
            check("英文卡片有标题", "Mail service" in en_text, en_text.splitlines()[0])

            page.set_viewport_size({"width": 390, "height": 900})
            page.wait_for_timeout(600)
            box = page.evaluate(
                """() => {
                    const c = document.querySelector("[data-card='mail']");
                    const r = c.getBoundingClientRect();
                    return { right: Math.round(r.right), vw: window.innerWidth };
                }"""
            )
            check("手机宽度卡片不溢出", box["right"] <= box["vw"] + 1, box)
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(300)
            page.locator("[data-card='mail']").screenshot(path=str(_TMP / "mail_card_mobile.png"))

            check("没有 JS 报错", not errors, "; ".join(errors[:3]))
            browser.close()
    finally:
        fake.shutdown()

    print(f"\n{'=' * 56}")
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for name in FAILED:
        print("  FAILED:", name)
    print("截图:", _TMP / "mail_card_mobile.png")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
