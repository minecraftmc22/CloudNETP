# -*- coding: utf-8 -*-
"""快传页入场动画 + 看板娘滚轮缩放动画的 UI 冒烟。

用库副本起独立实例，Playwright 驱动本机 Chrome，真实走一遍：

  A 快传视图：警示条挂 quick-rise；切标签时卡片依次浮起、标签栏只在首次滑入
  B 生成二维码 / 取件码后，新记录与二维码块单独弹一下（quick-pop）
  C 动画播完自己摘掉 class（不能越积越多）
  D 看板娘滚轮缩放：宽度是逐帧过渡而不是一格一跳，结束后回到最终尺寸
  E 缩放提示（xx%）出现、带弹跳；is-zooming 标记用完就清
  F 全程无 JS 报错

    ./.venv/Scripts/python.exe scripts/quick_anim_ui.py
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(tempfile.gettempdir()) / "cloudnetp_shots"
OUT.mkdir(exist_ok=True)

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


# 记录「谁被打了动画 class」，元素是 JS 后加 class，只能靠属性变更观察
RECORDER = """
window.__anim = [];
const seen = new WeakMap();
const startRecorder = () => {
  new MutationObserver((records) => {
    for (const r of records) {
      if (r.type !== "attributes" || r.attributeName !== "class") continue;
      const el = r.target;
      for (const name of ["quick-rise", "quick-pop", "quick-slide"]) {
        if (!el.classList.contains(name)) continue;
        if (seen.get(el) === name) continue;
        seen.set(el, name);
        const klass = (el.getAttribute("class") || "")
          .split(/\\s+/)
          .filter((c) => c && !["quick-rise", "quick-pop", "quick-slide"].includes(c))
          .join(".");
        window.__anim.push({ cls: name, tag: el.tagName.toLowerCase(), klass, text: (el.textContent || "").trim().slice(0, 24) });
      }
    }
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["class"], subtree: true });
};
// 文档开始时 <html> 可能还没建出来，等一等再挂
if (document.documentElement) startRecorder();
else document.addEventListener("DOMContentLoaded", startRecorder);
"""


def main() -> int:
    from app.config import settings
    from app.security import create_token

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-quick-anim-")).resolve()
    db_copy = tmp / "ui.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    row = con.execute("select id, token_version from users order by id limit 1").fetchone()
    if not row:
        con.execute(
            "insert into users (username, password_hash, display_name, quota_bytes, used_bytes, role,"
            " token_version, created_at) values (?,?,?,?,?,?,?,datetime('now'))",
            ("animui", "x", "AnimUI", 10 * 1024 ** 3, 0, 1, 0),
        )
        con.commit()
        row = con.execute("select id, token_version from users where username = 'animui'").fetchone()
    # 快传要先同意使用规范，否则一进来只有闸门
    con.execute("update user_settings set quick_terms_at = datetime('now') where user_id = ?", (row[0],))
    if con.execute("update user_settings set show_mascot = 1 where user_id = ?", (row[0],)).rowcount == 0:
        con.execute(
            "insert into user_settings (user_id, show_mascot) values (?, 1)",
            (row[0],),
        )
    con.commit()
    con.close()
    cookie = create_token(row[0], row[1])

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
            "DB_FALLBACK_ENABLED": "1",
            "SECRET_KEY": settings.SECRET_KEY,
        }
    )
    log_path = tmp / "server.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "main.py"], cwd=str(ROOT), env=env,
        stdout=log_file, stderr=subprocess.STDOUT,
    )

    from playwright.sync_api import sync_playwright

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来，看日志：" + str(log_path))

        with sync_playwright() as p:
            # headless 下页面容易被判定为「不可见」，rAF 会被节流到 1fps，
            # 那样根本测不出过渡动画，得把后台节流的开关都关掉
            args = [
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ]
            try:
                browser = p.chromium.launch(channel="chrome", headless=True, args=args)
            except Exception:
                browser = p.chromium.launch(executable_path=CHROME, headless=True, args=args)

            ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
            ctx.add_cookies([{"name": "cloudnetp_session", "value": cookie, "url": base}])
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            page.add_init_script(RECORDER)

            def anims() -> list[dict]:
                return page.evaluate("window.__anim || []")

            def reset_anims():
                page.evaluate("window.__anim = []")

            print("\n[A] 快传视图的入场动画")
            page.goto(f"{base}/#/quick", wait_until="domcontentloaded")
            page.wait_for_selector("[data-panel] .quick-card", timeout=20000)
            page.wait_for_timeout(900)
            warn_anim = page.evaluate(
                "getComputedStyle(document.querySelector('.quick-warn')).animationName"
            )
            check("警示条挂上入场动画", warn_anim == "quick-rise", warn_anim)
            first = anims()
            check(
                "首屏卡片浮起（quick-rise）",
                any(a["cls"] == "quick-rise" and "quick-card" in a["klass"] for a in first),
                str([a["klass"] for a in first][:6]),
            )
            check(
                "标签栏只在首次滑入（quick-slide）",
                any(a["cls"] == "quick-slide" for a in first),
                str([a["cls"] for a in first][:6]),
            )

            reset_anims()
            page.click('[data-tab="pickup"]')
            page.wait_for_timeout(120)
            second = anims()
            check(
                "切标签：卡片重新依次浮起",
                len([a for a in second if a["cls"] == "quick-rise" and "quick-card" in a["klass"]]) >= 2,
                str([a["klass"] for a in second][:6]),
            )
            check(
                "切标签：标签栏不重播滑入",
                not any(a["cls"] == "quick-slide" for a in second),
                str([a["cls"] for a in second][:6]),
            )
            page.screenshot(path=str(OUT / "quick_enter_anim.png"))

            print("\n[B] 新建记录的弹入")
            page.click('[data-tab="drop"]')
            page.wait_for_timeout(500)
            reset_anims()
            page.click('[data-act="new-drop"]')
            page.wait_for_selector("[data-panel] .quick-share", timeout=15000)
            page.wait_for_timeout(200)
            third = anims()
            check(
                "二维码块弹出来（quick-pop）",
                any(a["cls"] == "quick-pop" and "quick-share" in a["klass"] for a in third),
                str([(a["cls"], a["klass"]) for a in third][:6]),
            )
            check(
                "新记录那行也弹一下",
                any(a["cls"] == "quick-pop" and "quick-row" in a["klass"] for a in third),
                str([(a["cls"], a["klass"]) for a in third][:6]),
            )
            page.screenshot(path=str(OUT / "quick_new_drop.png"))

            print("\n[C] 动画 class 用完就摘")
            page.wait_for_timeout(900)
            left = page.evaluate(
                "document.querySelectorAll('.quick-rise, .quick-pop, .quick-slide').length"
            )
            check("没有残留的动画 class", left == 0, f"剩余 {left}")

            print("\n[D] 看板娘滚轮缩放是过渡而不是一格一跳")
            page.goto(f"{base}/#/files", wait_until="domcontentloaded")
            page.wait_for_selector("#mascot:not(.is-hidden)", timeout=20000)
            page.wait_for_timeout(1200)
            page.bring_to_front()
            page.evaluate("localStorage.setItem('mascot-wheel', '1')")
            wired = page.evaluate(
                """() => {
                  const s = getComputedStyle(document.querySelector('#mascot'));
                  return { prop: s.transitionProperty, dur: s.transitionDuration };
                }"""
            )
            check(
                "宽高都挂了过渡（不是瞬间跳）",
                "width" in wired["prop"] and "height" in wired["prop"],
                wired["prop"],
            )
            check("过渡时长约 220ms", "0.22s" in wired["dur"], wired["dur"])

            box = page.locator("#mascot").bounding_box()
            if not box:
                raise SystemExit("看板娘不可见，无法测滚轮缩放")
            page.mouse.move(box["x"] + box["width"] * 0.35, box["y"] + box["height"] * 0.3)
            start = page.evaluate("document.querySelector('#mascot').getBoundingClientRect().width")
            page.mouse.wheel(0, -300)  # 往上滚 = 放大
            page.wait_for_timeout(140)
            running = page.evaluate(
                """() => document.querySelector('#mascot').getAnimations()
                     .map((a) => a.transitionProperty || a.animationName)"""
            )
            check(
                "滚动后 width / height 过渡真的在跑",
                "width" in running and "height" in running,
                str(running),
            )
            check(
                "过渡期间挂着 is-zooming",
                page.evaluate("document.querySelector('#mascot').classList.contains('is-zooming')"),
            )
            # headless 下不一定产帧，截一次图顺手把过渡推到当前时刻（也顺便留档百分比标签）
            page.screenshot(path=str(OUT / "mascot_zoom_anim.png"))
            page.wait_for_timeout(900)
            page.screenshot(path=str(OUT / "mascot_zoom_settled.png"))

            scale = float(
                page.evaluate(
                    "getComputedStyle(document.documentElement).getPropertyValue('--mascot-scale')"
                )
                or 1
            )
            end = page.evaluate("document.querySelector('#mascot').getBoundingClientRect().width")
            check("缩放值确实变大了", scale > 1, f"{scale}")
            check("宽度落到目标尺寸", abs(end - start * scale) <= 1.6, f"{start:.1f} × {scale} = {start * scale:.1f} vs {end:.1f}")
            check(
                "停下后清掉 is-zooming",
                not page.evaluate("document.querySelector('#mascot').classList.contains('is-zooming')"),
            )

            print("\n[E] 缩放时的小提示")
            tag = page.locator("#mascot .mascot__size")
            check("提示元素出现", tag.count() == 1)
            text = tag.inner_text() if tag.count() else ""
            check("提示显示百分比", "%" in text, text)
            check(
                "提示带弹跳标记（is-tick）",
                "is-tick" in (tag.get_attribute("class") or "") if tag.count() else False,
            )

            print("\n[F] 控制台")
            check("无 JS 报错", not errors, str(errors[:2]))

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    print(f"\n通过 {passed} 项，失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
