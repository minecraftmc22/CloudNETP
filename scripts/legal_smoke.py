# -*- coding: utf-8 -*-
"""用户协议 / 隐私政策 与「已阅读并同意」的回归。

跑一个用库副本起的独立实例（不碰真实库，跑完删临时目录）：

  1. /terms、/privacy 能打开，正文、目录、站点名都渲染出来
  2. 静态资源 legal.css / legal.js 能取到
  3. 登录页有勾选行；未勾选提交被拦住、不发请求；勾选后才会发出请求
  4. 英文界面下勾选文案是英文
  5. 注册接口：不带 agree 返回 400 并提示同意协议；带了 agree 才会走到验证码校验

    ./.venv/Scripts/python.exe scripts/legal_smoke.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SHOT_DIR = Path(tempfile.gettempdir()) / "cloudnetp_shots"

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


def snapshot_db(dst: Path) -> None:
    src_path = ROOT / "dev.db"
    if not src_path.is_file():
        src_path = ROOT / "storage" / "db" / "fallback.db"
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        src.close()
        out.close()


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
    print(log_path.read_text(encoding="utf-8", errors="replace")[-1200:])
    return False


def request(url: str, *, method: str = "GET", body: dict | None = None, origin: str = "") -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> None:
    from app.config import settings

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-legal-")).resolve()
    db_copy = tmp / "legal.db"
    snapshot_db(db_copy)

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
            "DB_FALLBACK_ENABLED": "0",
            "SECRET_KEY": settings.SECRET_KEY,
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
            raise SystemExit("服务没起来")

        print("[1] 两个页面 + 静态资源")
        status, html = request(f"{base}/terms")
        check("/terms 200", status == 200, str(status))
        check("/terms 有正文", "用户协议" in html and "服务说明" in html and "使用规范" in html)
        status, html = request(f"{base}/privacy")
        check("/privacy 200", status == 200, str(status))
        check("/privacy 有正文", "隐私政策" in html and "我们收集哪些信息" in html and "你的权利" in html)
        for asset in ("/static/css/legal.css", "/static/js/legal.js"):
            status, _ = request(f"{base}{asset}")
            check(f"{asset} 可取", status == 200, str(status))

        print("\n[2] 注册接口必须带同意标记")
        status, body = request(
            f"{base}/api/auth/register", method="POST", origin=base,
            body={"username": "legal_probe", "password": "abcd1234", "captchaId": "", "captcha": ""},
        )
        check("不带 agree 返回 400", status == 400, str(status))
        check("提示要同意协议", "同意" in body and "用户协议" in body, body[:80])
        status, body = request(
            f"{base}/api/auth/register", method="POST", origin=base,
            body={"username": "legal_probe", "password": "abcd1234", "captchaId": "", "captcha": "", "agree": True},
        )
        check("带 agree 后不再被这条挡住", status == 400 and "同意" not in body, f"{status} {body[:80]}")

        print("\n[3] 页面渲染（Playwright）")
        from playwright.sync_api import sync_playwright

        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True)

            def new_page(lang: str = "zh-CN"):
                ctx = browser.new_context(viewport={"width": 1280, "height": 1000}, device_scale_factor=2)
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.add_init_script(
                    f"try {{ localStorage.setItem('cloudnetp.lang', '{lang}'); }} catch (e) {{}}"
                )
                return ctx, page, errors

            ctx, page, errors = new_page()
            for name, title in (("terms", "用户协议"), ("privacy", "隐私政策")):
                page.goto(f"{base}/{name}", wait_until="domcontentloaded")
                page.wait_for_selector(".legal-body section", timeout=15000)
                page.wait_for_timeout(500)
                info = page.evaluate(
                    """() => {
                        const sections = [...document.querySelectorAll('.legal-body section')];
                        return {
                            sections: sections.length,
                            numbered: sections.every((s) => Boolean(s.querySelector('.legal-body__no'))),
                            tocHidden: document.querySelector('[data-toc]')?.hidden,
                            tocItems: document.querySelectorAll('[data-toc] ol li').length,
                            site: document.querySelector('[data-site]')?.textContent?.trim(),
                            title: document.querySelector('h1')?.textContent?.trim(),
                        };
                    }"""
                )
                check(f"/{name} 标题是「{title}」", info["title"] == title, str(info["title"]))
                check(f"/{name} 小节数 > 6", info["sections"] > 6, str(info["sections"]))
                check(f"/{name} 每个小节都有编号", info["numbered"])
                check(f"/{name} 目录已生成且条目匹配", not info["tocHidden"] and info["tocItems"] == info["sections"])
                check(f"/{name} 站点名已填充", info["site"] == "CloudNETP", str(info["site"]))
                page.screenshot(path=str(SHOT_DIR / f"legal_{name}.png"), full_page=True)
            check("协议页无 JS 报错", not errors, f"{len(errors)} 条" if errors else "")

            # 未登录打开登录页：未勾选时提交要被拦住，且不发请求
            ctx2, page2, errors2 = new_page()
            calls = {"login": 0}
            page2.on(
                "request",
                lambda req: calls.__setitem__("login", calls["login"] + 1)
                if "/api/auth/login" in req.url
                else None,
            )
            page2.goto(f"{base}/", wait_until="domcontentloaded")
            page2.wait_for_selector("[data-agree]", timeout=15000)
            check("登录页有「已阅读并同意」勾选", True)
            check("默认未勾选", page2.is_checked("[data-agree]") is False)
            agree_text = page2.inner_text("[data-agree-row]")
            check("勾选行含两个协议链接", "《用户协议》" in agree_text and "《隐私政策》" in agree_text, agree_text)
            links = page2.eval_on_selector_all(
                "[data-agree-row] a", "els => els.map((a) => [a.getAttribute('href'), a.target])"
            )
            check(
                "链接指向 /terms 与 /privacy 且新窗口打开",
                links == [["/terms", "_blank"], ["/privacy", "_blank"]],
                str(links),
            )
            page2.fill('[name="account"]', "legal_probe")
            page2.fill('[name="password"]', "abcd1234")
            page2.click("[data-submit]")
            page2.wait_for_timeout(500)
            msg = page2.inner_text("[data-msg]")
            check("未勾选提交给出提示", "请先阅读并同意" in msg, msg)
            check("未勾选不发登录请求", calls["login"] == 0, str(calls["login"]))
            check(
                "未勾选时勾选行有警示态",
                page2.eval_on_selector("[data-agree-row]", "el => el.classList.contains('is-warn')"),
            )
            page2.screenshot(path=str(SHOT_DIR / "legal_login_warn.png"))

            page2.check("[data-agree]")
            page2.click("[data-submit]")
            page2.wait_for_timeout(1200)
            check("勾选后才会发出登录请求", calls["login"] == 1, str(calls["login"]))
            msg = page2.inner_text("[data-msg]")
            check("账号不对时提示换成服务端的错误", "请先阅读并同意" not in msg, msg)
            page2.screenshot(path=str(SHOT_DIR / "legal_login_checked.png"))
            check("登录页无 JS 报错", not errors2, f"{len(errors2)} 条" if errors2 else "")

            # 英文界面
            ctx3, page3, _ = new_page("en-US")
            page3.goto(f"{base}/", wait_until="domcontentloaded")
            page3.wait_for_selector("[data-agree]", timeout=15000)
            en_text = page3.inner_text("[data-agree-row]")
            check("英文界面勾选文案是英文", "I have read and agree to" in en_text, en_text.replace("\n", " "))
            page3.screenshot(path=str(SHOT_DIR / "legal_login_en.png"))

            page3.goto(f"{base}/privacy", wait_until="domcontentloaded")
            page3.wait_for_selector(".legal-body section", timeout=15000)
            page3.wait_for_timeout(300)
            note_hidden = page3.eval_on_selector("[data-lang-note]", "el => el.hidden")
            check("英文界面显示「以中文版为准」说明", note_hidden is False)

            # 手机端：两行文字最容易横向溢出
            mctx = browser.new_context(
                viewport={"width": 390, "height": 844}, device_scale_factor=3,
                is_mobile=True, has_touch=True, locale="zh-CN",
            )
            mpage = mctx.new_page()
            mpage.add_init_script("try { localStorage.setItem('cloudnetp.lang', 'zh-CN'); } catch (e) {}")
            mpage.goto(f"{base}/", wait_until="domcontentloaded")
            mpage.wait_for_selector("[data-agree]", timeout=15000)
            mpage.wait_for_timeout(600)
            overflow = mpage.eval_on_selector(
                "[data-agree-row]",
                "el => ({ scroll: el.scrollWidth, client: el.clientWidth })",
            )
            check(
                "手机端勾选行不横向溢出",
                overflow["scroll"] <= overflow["client"] + 1,
                f"{overflow['scroll']} / {overflow['client']}",
            )
            mpage.screenshot(path=str(SHOT_DIR / "legal_login_mobile.png"))

            mpage.goto(f"{base}/terms", wait_until="domcontentloaded")
            mpage.wait_for_selector(".legal-body section", timeout=15000)
            mpage.wait_for_timeout(400)
            mdoc = mpage.eval_on_selector(
                ".legal-doc",
                "el => ({ scroll: el.scrollWidth, client: el.clientWidth })",
            )
            check(
                "手机端协议页不横向溢出",
                mdoc["scroll"] <= mdoc["client"] + 1,
                f"{mdoc['scroll']} / {mdoc['client']}",
            )
            mpage.screenshot(path=str(SHOT_DIR / "legal_terms_mobile.png"), full_page=False)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_file.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n通过 {passed}，失败 {failed}")
    print(f"截图：{SHOT_DIR}")
    raise SystemExit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
