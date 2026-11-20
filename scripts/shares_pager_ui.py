# -*- coding: utf-8 -*-
"""「我的分享」底部分页条的配色 / 透明度 + 翻页箭头方向。

用户报的两个问题：
  1. 分页条那条横带颜色和透明度都不对 —— 跟上面的分享行完全不是一个东西，
     像一条实心紫色板压在壁纸上，而且「每页」下拉框根本没有底色。
     根因：底色用了 --dialog-surface 86%（那是个**不透明实色**，暗色 #2e2146）；
     下拉框写的是 var(--surface-2)，这个变量全项目从来没定义过（等于透明）。
  2. 上一页 / 下一页 的箭头左右反了 —— #i-chevron 本身是朝右的，
     原来给「下一页」加了 180° 旋转，正好反了。

脚本起临时实例，20 个空文件各开一条分享凑出两页，然后在亮 / 暗两套主题下：
  [A] 分页条底色与分享行同一族（computed backgroundColor 完全一致，alpha < 0.7）
  [B] 不再是 dialog-surface 的实色（暗色 rgb(46, 33, 70)）
  [C] 底色带当次主题色的薄雾
  [D] 「每页」下拉有底色（不是 transparent）
  [E] 上一页箭头朝左、下一页朝右（看 computed transform 矩阵）
  [F] 翻页仍能用（点下一页 → 第 2 页，上一页变为可用）
  [G] 窄高度下滚动到底，分页条压在内容上的观感（截图）

    ./.venv/Scripts/python.exe scripts/shares_pager_ui.py
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

PASS = FAIL = 0
ACCENT = "#22c55e"
RGB_ACCENT = "34, 197, 94"

INIT_JS = (
    "document.addEventListener('DOMContentLoaded', () => {"
    "  const s = document.createElement('style');"
    "  s.textContent = '#mascot{display:none!important}"
    "*{animation-duration:0s!important;transition-duration:0s!important}';"
    "  document.head.appendChild(s);"
    "});"
)


def check(label: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


RUNNER = r"""
import os, sys
sys.path.insert(0, os.environ["SMOKE_ROOT"])
from app import captcha
captcha.verify = lambda *a, **k: (True, "")
import app.main as m
import uvicorn
uvicorn.run(m.app, host="127.0.0.1", port=int(os.environ["SMOKE_PORT"]), log_level="warning")
"""


def wait_http(base: str, proc: subprocess.Popen) -> bool:
    for _ in range(120):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"{base}/api/meta", timeout=5) as res:
                if res.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


STYLE_JS = """
() => {
  const pager = document.querySelector(".share-pager");
  const row = document.querySelector(".share-row");
  const sel = document.querySelector(".share-pager__size select");
  const prevSvg = document.querySelector('.share-pager [data-page="prev"] .icon');
  const nextSvg = document.querySelector('.share-pager [data-page="next"] .icon');
  if (!pager) return null;
  // color-mix 在 Chrome 里算成 color(srgb 1 1 1 / 0.44)，跟 rgba(255,255,255,0.44)
  // 是同一个颜色但字符串不同 —— 统一归一化成 [r, g, b, a]，别按字符串比
  const norm = (color) => {
    if (!color) return null;
    let m = color.match(/^color\\(srgb\\s+([\\d.]+)\\s+([\\d.]+)\\s+([\\d.]+)(?:\\s*\\/\\s*([\\d.]+))?\\)$/);
    if (m) {
      return [Math.round(Number(m[1]) * 255), Math.round(Number(m[2]) * 255),
              Math.round(Number(m[3]) * 255), m[4] === undefined ? 1 : Number(m[4])];
    }
    m = color.match(/^rgba?\\(([^)]+)\\)$/);
    if (m) {
      const p = m[1].split(",").map((s) => s.trim());
      return [Number(p[0]), Number(p[1]), Number(p[2]), p.length > 3 ? Number(p[3]) : 1];
    }
    return null;
  };
  const pcs = getComputedStyle(pager);
  return {
    pagerBg: norm(pcs.backgroundColor),
    pagerBgRaw: pcs.backgroundColor,
    pagerImage: pcs.backgroundImage,
    rowBg: row ? norm(getComputedStyle(row).backgroundColor) : null,
    rowBgRaw: row ? getComputedStyle(row).backgroundColor : null,
    selectBg: sel ? norm(getComputedStyle(sel).backgroundColor) : null,
    prevTransform: prevSvg ? getComputedStyle(prevSvg).transform : null,
    nextTransform: nextSvg ? getComputedStyle(nextSvg).transform : null,
    pagerInfo: document.querySelector(".share-pager__info")?.textContent?.trim() || "",
    prevDisabled: document.querySelector('.share-pager [data-page="prev"]')?.disabled,
    nextDisabled: document.querySelector('.share-pager [data-page="next"]')?.disabled,
  };
}
"""


def same_color(a, b, tol_rgb: int = 1, tol_a: float = 0.02) -> bool:
    if not a or not b:
        return False
    return (
        all(abs(a[i] - b[i]) <= tol_rgb for i in range(3))
        and abs(a[3] - b[3]) <= tol_a
    )



def main() -> int:
    from playwright.sync_api import sync_playwright

    work = Path(tempfile.mkdtemp(prefix="cloudnetp-pager-"))
    db_path = work / "test.db"
    shutil.copy(ROOT / "dev.db", db_path)
    port = free_port()
    base = f"http://localhost:{port}"
    runner = work / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")

    env = {
        **os.environ,
        "SMOKE_ROOT": str(ROOT),
        "SMOKE_PORT": str(port),
        "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "STORAGE_DIR": str(work / "storage"),
        "SECRET_KEY": "smoke-secret-key-for-pager",
        "DB_FALLBACK_ENABLED": "false",
        "MAIL_ENABLED": "false",
    }
    log_file = open(work / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(VENV_PY), str(runner)], cwd=str(ROOT), env=env,
        stdout=log_file, stderr=subprocess.STDOUT,
    )

    try:
        if not wait_http(base, proc):
            print((work / "server.log").read_text(encoding="utf-8")[-3000:])
            return 1

        stamp = str(int(time.time()))[-6:]
        username = f"pg{stamp}"
        password = "abc12345"

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            r = ctx.request.post(
                f"{base}/api/auth/register",
                data={
                    "username": username,
                    "password": password,
                    "captchaId": "",
                    "captcha": "0000",
                    "agree": True,
                },
            )
            check("注册", r.ok, str(r.status))
            r = ctx.request.post(
                f"{base}/api/auth/login",
                data={"username": username, "password": password, "captchaId": "", "captcha": "0000"},
            )
            check("登录", r.ok, str(r.status))

            # 20 个空文件（size=0 直接建节点，不用传分片）
            node_ids: list[int] = []
            for index in range(1, 21):
                resp = ctx.request.post(
                    f"{base}/api/uploads/init",
                    data={"name": f"示例文件 {index:02d}.mp4", "size": 0, "parentId": None},
                )
                assert resp.ok, resp.text()
                payload = resp.json()
                node_ids.append(int(payload["node"]["id"]))
            check("建了 20 个文件", len(node_ids) == 20, str(len(node_ids)))

            # 一条分享一行：合集分享会把多个 node 折成一条，所以这里逐个开
            created = 0
            for node_id in node_ids:
                resp = ctx.request.post(f"{base}/api/shares", data={"nodeId": node_id})
                if resp.ok:
                    created += 1
            check("开了 20 条分享", created == 20, str(created))
            resp = ctx.request.patch(
                f"{base}/api/me/settings",
                data={"accentColor": ACCENT, "accentStyle": {"mode": "solid", "colors": []}},
            )
            check("写入主题色", resp.ok, str(resp.status))

            page = ctx.new_page()
            page.add_init_script(INIT_JS)
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            def load_shares(mode: int) -> None:
                resp = ctx.request.patch(f"{base}/api/me/settings", data={"themeMode": mode})
                assert resp.ok, resp.text()
                page.goto(f"{base}/#/shares", wait_until="domcontentloaded")
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector(".share-row", timeout=20000)
                page.wait_for_selector(".share-pager", timeout=20000)
                page.wait_for_timeout(500)

            for label, mode, shot in (("亮色", 0, "shares-pager-light.png"), ("暗色", 1, "shares-pager-dark.png")):
                print(f"\n[{label}]")
                load_shares(mode)
                info = page.evaluate(STYLE_JS)
                if not info:
                    check("分页条存在", False)
                    continue
                rows = page.locator(".share-row").count()
                check("列表有 10 行（第一页）", rows == 10, str(rows))
                check("分页文案正确", "共 20 个链接" in info["pagerInfo"], info["pagerInfo"])
                check("分页条与分享行同一色相（同一族玻璃）",
                      same_color(info["pagerBg"], info["rowBg"], tol_rgb=14, tol_a=0.30),
                      f'{info["pagerBgRaw"]} vs {info["rowBgRaw"]}')
                check("分页条是半透明遮罩、不是实色",
                      0.5 <= (info["pagerBg"] or [0, 0, 0, 1])[3] <= 0.78,
                      info["pagerBgRaw"])
                check("分页条不再用 dialog-surface 实色",
                      not same_color(info["pagerBg"], [46, 33, 70, 0.86], tol_rgb=6, tol_a=0.1),
                      info["pagerBgRaw"])
                check("分页条带主题色薄雾", RGB_ACCENT in (info["pagerImage"] or ""),
                      (info["pagerImage"] or "")[:70])
                check("「每页」下拉有底色", bool(info["selectBg"]), str(info["selectBg"]))
                check("「每页」下拉底色不是全透明", (info["selectBg"] or [0, 0, 0, 0])[3] > 0.05,
                      str(info["selectBg"]))
                check("上一页箭头朝左（旋转 180°）", info["prevTransform"].startswith("matrix(-1"),
                      info["prevTransform"])
                check("下一页箭头朝右（不旋转）", info["nextTransform"] in ("none", "matrix(1, 0, 0, 1, 0, 0)"),
                      info["nextTransform"])
                check("第一页时上一页禁用、下一页可用",
                      info["prevDisabled"] is True and info["nextDisabled"] is False,
                      f'prev={info["prevDisabled"]} next={info["nextDisabled"]}')

                page.screenshot(path=str(OUT / shot))

                # 翻页
                page.click('.share-pager [data-page="next"]')
                page.wait_for_timeout(700)
                info2 = page.evaluate(STYLE_JS)
                check("点下一页到了第 2 页", "第 2/2 页" in info2["pagerInfo"], info2["pagerInfo"])
                check("第二页时上一页可用、下一页禁用",
                      info2["prevDisabled"] is False and info2["nextDisabled"] is True,
                      f'prev={info2["prevDisabled"]} next={info2["nextDisabled"]}')

            # 窄高度滚到底：看分页条压在内容上的实际观感
            print("\n[暗色 · 滚动到底]")
            page.set_viewport_size({"width": 1280, "height": 560})
            load_shares(1)
            page.evaluate("() => { const b = document.querySelector('.view-body'); b.scrollTop = b.scrollHeight; }")
            page.wait_for_timeout(500)
            page.screenshot(path=str(OUT / "shares-pager-scrolled.png"))
            box = page.evaluate(
                "() => { const el = document.querySelector('.share-pager'); const r = el.getBoundingClientRect();"
                " const body = document.querySelector('.view-body').getBoundingClientRect();"
                " return { bottom: Math.round(r.bottom), bodyBottom: Math.round(body.bottom),"
                "  height: Math.round(r.height), vh: window.innerHeight }; }"
            )
            # .view-body 自己带 padding-bottom，分页条贴着滚动区底部但要留出这段内边距
            check(
                "滚动时贴在滚动区底部（只差内容内边距）",
                0 <= box["bodyBottom"] - box["bottom"] <= 32,
                str(box),
            )
            check("分页条在视口内", box["bottom"] <= box["vh"], str(box))

            # 窄屏：分页条会折成两行，别横向溢出、也别把箭头挤丢
            print("\n[暗色 · 390 窄屏]")
            page.set_viewport_size({"width": 390, "height": 780})
            load_shares(1)
            page.evaluate("() => { const b = document.querySelector('.view-body'); b.scrollTop = b.scrollHeight; }")
            page.wait_for_timeout(500)
            page.screenshot(path=str(OUT / "shares-pager-mobile.png"))
            narrow = page.evaluate(
                "() => { const el = document.querySelector('.share-pager'); const r = el.getBoundingClientRect();"
                " const prev = document.querySelector('[data-page=\"prev\"]').getBoundingClientRect();"
                " const next = document.querySelector('[data-page=\"next\"]').getBoundingClientRect();"
                " return { over: Math.round(r.right - window.innerWidth),"
                "  prevLeftOfNext: prev.left < next.left, docOver: document.documentElement.scrollWidth - window.innerWidth }; }"
            )
            check("窄屏分页条不横向溢出", narrow["over"] <= 0 and narrow["docOver"] <= 0, str(narrow))
            check("窄屏箭头顺序仍是左上一个 / 右下一个", narrow["prevLeftOfNext"], str(narrow))

            check("页面没有 JS 报错", not errors, str(errors[:3]))
            browser.close()

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        print(f"\n服务日志：{work / 'server.log'}")
        print(f"截图目录：{OUT}")

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
