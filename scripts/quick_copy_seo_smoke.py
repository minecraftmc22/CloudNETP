# -*- coding: utf-8 -*-
"""取件码分享文案 + 后台 SEO 的接口联调。

  [A] 默认值：/api/quick/limits 带默认模板与「自动复制」开；SEO 默认不收录，
      robots.txt 整站 Disallow、sitemap 404、首页输出 noindex
  [B] 后台改文案：模板与你填的一字不差、占位符大小写都认、清空回落默认、
      超长被 422 挡下、关掉自动复制后 autoCopy=false
  [C] 后台改 SEO：首页 <title>/description/og:*/canonical 全都写进 HTML，
      robots.txt 换成放行策略并带 Sitemap，sitemap.xml 能列出静态页
  [D] 关收录后立刻回退：首页 noindex、robots 整站禁止、sitemap 404
  [E] 分享页 / 取件页无论收录开没开都带 noindex

    ./.venv/Scripts/python.exe scripts/quick_copy_seo_smoke.py
"""

from __future__ import annotations

import json
import os
import re
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
PASSWORD = "Test123456"

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


def request(base: str, method: str, path: str, body=None, cookie: str = "", raw: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Origin", base)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
            headers = resp.headers
            if raw:
                return resp.status, payload.decode("utf-8", "replace"), headers
            return resp.status, json.loads(payload or b"{}"), headers
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        if raw:
            return exc.code, payload.decode("utf-8", "replace"), exc.headers
        try:
            return exc.code, json.loads(payload or b"{}"), exc.headers
        except json.JSONDecodeError:
            return exc.code, {"raw": payload[:200].decode("utf-8", "replace")}, exc.headers


def main() -> int:
    from app.config import settings
    from app.security import hash_password
    from app.settings_store import QUICK_COPY_TEMPLATE_DEFAULT

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-copyseo-")).resolve()
    db_copy = tmp / "smoke.db"
    snapshot_db(db_copy)

    con = sqlite3.connect(str(db_copy))
    for stmt in (
        "delete from quick_transfers",
        # 后台覆盖值会影响断言，先清掉
        "delete from system_settings where k like 'seo_%' or k like 'quick_%'",
    ):
        try:
            con.execute(stmt)
        except sqlite3.OperationalError:
            pass
    users = con.execute(
        "select id, username, token_version, role from users order by role desc, id limit 2"
    ).fetchall()
    while len([u for u in users if u[3] == 1]) < 1:
        con.execute(
            "insert into users (username, password_hash, display_name, quota_bytes, used_bytes, role,"
            " token_version, created_at) values (?,?,?,?,?,?,?,datetime('now'))",
            ("copyseoadmin", "x", "copyseoadmin", 10 * 1024 ** 3, 0, 1, 0),
        )
        con.commit()
        users = con.execute(
            "select id, username, token_version, role from users order by role desc, id limit 2"
        ).fetchall()
    hashed = hash_password(PASSWORD)
    for row in users:
        con.execute("update users set password_hash=? where id=?", (hashed, row[0]))
    con.execute(
        "update user_settings set quick_terms_at=datetime('now') where user_id in (?,?)",
        [row[0] for row in users],
    )
    con.commit()
    con.close()

    admin_name = users[0][1]
    user_name = users[1][1] if len(users) > 1 else admin_name

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

        # 真登录拿 cookie：副本实例只认自己签发的会话
        def login(name: str) -> str:
            status, data, headers = request(
                base, "POST", "/api/auth/login", {"username": name, "password": PASSWORD}
            )
            if status != 200:
                raise SystemExit(f"登录失败 {name}: {status} {data}")
            cookie = (headers.get("Set-Cookie") or "").split(";")[0]
            if not cookie:
                raise SystemExit("登录没拿到 cookie")
            return cookie

        admin_cookie = login(admin_name)
        user_cookie = login(user_name) if user_name != admin_name else admin_cookie

        # ---------------- A 默认值 ----------------
        print("\n[A] 默认值")
        status, data, _ = request(base, "GET", "/api/quick/limits", cookie=user_cookie)
        limits = data.get("limits") or {}
        check("limits 200", status == 200, str(status))
        check("默认自动复制开着", limits.get("autoCopy") is True, str(limits.get("autoCopy")))
        tpl = limits.get("copyTemplate") or ""
        check("默认模板就是那段喵", tpl == QUICK_COPY_TEMPLATE_DEFAULT, repr(tpl[:30]))
        for token in ("{site}", "{file}", "{code}", "{link}"):
            check(f"模板含占位符 {token}", token in tpl)

        status, html, headers = request(base, "GET", "/", raw=True)
        check("首页 200", status == 200, str(status))
        check("默认不收录（noindex）", 'name="robots" content="noindex,nofollow"' in html)
        check("默认没有 og:image", "og:image" not in html)
        check("页面标题保持原样", "<title>CloudNETP" in html or "<title>" in html)

        status, robots, _ = request(base, "GET", "/robots.txt", raw=True)
        check("robots.txt 200", status == 200, str(status))
        check("关收录时整站 Disallow", robots.strip() == "User-agent: *\nDisallow: /", repr(robots))
        status, _, _ = request(base, "GET", "/sitemap.xml")
        check("关收录时 sitemap 404", status == 404, str(status))

        # ---------------- B 后台改文案 ----------------
        print("\n[B] 后台改取件码文案")
        custom = "先看这个：{file}\n码 {Code} —— {link}（来自 {site}）"
        status, data, _ = request(
            base, "PATCH", "/api/admin/settings",
            {"quickCopyTemplate": custom, "quickAutoCopy": False}, admin_cookie,
        )
        check("保存文案 200", status == 200, str(status))
        saved = (data.get("settings") or {})
        check("后台回显一致", saved.get("quickCopyTemplate") == custom, str(saved.get("quickCopyTemplate")))
        check("后台回显自动复制已关", saved.get("quickAutoCopy") is False, str(saved.get("quickAutoCopy")))

        status, data, _ = request(base, "GET", "/api/quick/limits", cookie=user_cookie)
        limits = data.get("limits") or {}
        check("用户接口读到新模板", limits.get("copyTemplate") == custom, str(limits.get("copyTemplate"))[:30])
        check("用户接口读到关闭状态", limits.get("autoCopy") is False, str(limits.get("autoCopy")))

        status, data, _ = request(
            base, "PATCH", "/api/admin/settings", {"quickCopyTemplate": ""}, admin_cookie
        )
        check("清空文案 200", status == 200, str(status))
        status, data, _ = request(base, "GET", "/api/quick/limits", cookie=user_cookie)
        check(
            "清空后回落默认模板",
            (data.get("limits") or {}).get("copyTemplate") == QUICK_COPY_TEMPLATE_DEFAULT,
        )

        status, data, _ = request(
            base, "PATCH", "/api/admin/settings", {"quickCopyTemplate": "x" * 1001}, admin_cookie
        )
        check("超长模板被 422 挡下", status == 422, str(status))

        # ---------------- C 后台改 SEO ----------------
        print("\n[C] 后台开 SEO")
        status, data, _ = request(
            base, "PATCH", "/api/admin/settings",
            {
                "seoIndex": True,
                "seoTitle": "小云的私有盘",
                "seoDescription": "自建快传与网盘，文件分享不用注册。",
                "seoKeywords": "网盘, 私有云, 快传",
                "seoImage": "https://cdn.example.com/cover.png",
                "seoBaseUrl": "pan.example.com/",
            },
            admin_cookie,
        )
        check("保存 SEO 200", status == 200, str(status))
        saved = data.get("settings") or {}
        check("后台回显收录已开", saved.get("seoIndex") is True, str(saved.get("seoIndex")))
        check("后台回显标题", saved.get("seoTitle") == "小云的私有盘", str(saved.get("seoTitle")))

        status, html, _ = request(base, "GET", "/", raw=True)
        check("首页 <title> 被换掉", "<title>小云的私有盘</title>" in html)
        check("写入了 description", 'name="description" content="自建快传与网盘' in html)
        check("写入了 keywords", 'name="keywords" content="网盘, 私有云, 快传"' in html)
        check("收录时是 index,follow", 'name="robots" content="index,follow"' in html)
        check("canonical 补了 https", '<link rel="canonical" href="https://pan.example.com/">' in html)
        check("og:image 用的是填的图", 'property="og:image" content="https://cdn.example.com/cover.png"' in html)
        check("og:title 跟着走", 'property="og:title" content="小云的私有盘"' in html)
        check("有结构化数据", '<script type="application/ld+json">' in html)
        check("没留下注释占位符", "<!--seo-->" not in html)

        status, robots, _ = request(base, "GET", "/robots.txt", raw=True)
        check("robots 放行首页", "Disallow: /api/" in robots, repr(robots[:40]))
        check("robots 挡住分享页", "Disallow: /s/" in robots)
        check("robots 带 Sitemap", "Sitemap: https://pan.example.com/sitemap.xml" in robots)

        status, sitemap, _ = request(base, "GET", "/sitemap.xml", raw=True)
        check("sitemap 200", status == 200, str(status))
        check("sitemap 有首页", "<loc>https://pan.example.com/</loc>" in sitemap, sitemap[:120])
        check("sitemap 有协议页", "<loc>https://pan.example.com/terms</loc>" in sitemap)
        check("sitemap 有隐私页", "<loc>https://pan.example.com/privacy</loc>" in sitemap)
        check("sitemap 声明了命名空间", "www.sitemaps.org/schemas/sitemap" in sitemap)

        status, meta, _ = request(base, "GET", "/api/meta")
        check("meta 带上 SEO 标题", (meta or {}).get("seoTitle") == "小云的私有盘", str(meta.get("seoTitle")))

        # ---------------- D 关收录立刻回退 ----------------
        print("\n[D] 关收录回退")
        status, _, _ = request(
            base, "PATCH", "/api/admin/settings", {"seoIndex": False}, admin_cookie
        )
        check("关闭收录 200", status == 200, str(status))
        status, html, _ = request(base, "GET", "/", raw=True)
        check("首页回到 noindex", 'name="robots" content="noindex,nofollow"' in html)
        check("标题仍然保留", "<title>小云的私有盘</title>" in html)
        status, robots, _ = request(base, "GET", "/robots.txt", raw=True)
        check("robots 回到整站禁止", robots.strip() == "User-agent: *\nDisallow: /", repr(robots))
        status, _, _ = request(base, "GET", "/sitemap.xml")
        check("sitemap 再次 404", status == 404, str(status))

        # ---------------- E 分享页 / 取件页永不收录 ----------------
        print("\n[E] 私链页面永不收录")
        status, share_html, _ = request(base, "GET", "/s/faketoken123", raw=True)
        check("分享页 200", status == 200, str(status))
        check("分享页带 noindex", 'name="robots" content="noindex, nofollow"' in share_html)
        status, pickup_html, _ = request(base, "GET", "/pickup", raw=True)
        check("取件页 200", status == 200, str(status))
        check("取件页带 noindex", 'name="robots" content="noindex, nofollow"' in pickup_html)
        status, drop_html, _ = request(base, "GET", "/drop/faketoken123", raw=True)
        check("扫码直传页也带 noindex", 'name="robots" content="noindex, nofollow"' in drop_html)

        # 模板渲染：占位符大小写不敏感，坏占位符原样留着
        import re as _re

        def render(template: str, **values) -> str:
            out = template
            for key, value in values.items():
                out = _re.sub(r"\{" + key + r"\}", lambda _m, v=value: v, out, flags=_re.IGNORECASE)
            return out

        text = render("码 {Code} / {file} / {nope}", code="ABC123", file="a.txt")
        check("渲染时大小写不敏感", "码 ABC123" in text, text)
        check("不认识的占位符原样保留", "{nope}" in text, text)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    print(f"\n通过 {passed} 项，失败 {failed} 项")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
