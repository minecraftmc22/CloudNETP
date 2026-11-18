# -*- coding: utf-8 -*-
"""设置页标签页改版回归。

  1. 五个页签齐全，默认停在外观页，其它页隐藏
  2. 点页签只切 class/hidden 不重建 DOM（同一节点还在）
  3. 外观页藏着时进去再切回来，主题模式滑块要重新排（不是 0 宽）
  4. 记住上次的页签：刷新后还在那一页
  5. 手机竖屏页签条能横向滚、切换正常

    ./.venv/Scripts/python.exe scripts/settings_tabs_ui.py
"""

from __future__ import annotations

import os
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


def wait_up(base: str, proc: subprocess.Popen, log_path: Path) -> bool:
    for _ in range(90):
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
    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-settabs-")).resolve()
    db_path = tmp / "ui.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["STORAGE_DIR"] = str(tmp / "storage")
    os.environ["DB_FALLBACK_ENABLED"] = "0"
    os.environ["DB_PROBE_INTERVAL"] = "0"
    os.environ["DB_MIRROR_INTERVAL"] = "0"

    sys.path.insert(0, str(ROOT))
    from app.config import settings
    from app.db import SessionLocal, engine, ensure_schema
    from app.models import User, utcnow
    from app.security import create_token, hash_password

    ensure_schema(engine)
    pw = "Passw0rd!2024"
    with SessionLocal() as db:
        who = User(
            username="settabs",
            password_hash=hash_password(pw),
            email="settabs@example.com",
            email_verified=True,
            role=0,
            quota_bytes=10 * 1024**3,
            token_version=1,
            created_at=utcnow(),
        )
        db.add(who)
        db.commit()
        uid, ver = who.id, who.token_version

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
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

    from playwright.sync_api import sync_playwright

    try:
        if not wait_up(base, proc, log_path):
            raise SystemExit("服务没起来")
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)

            def make_page(w=1440, h=1000, mobile=False):
                ctx = (
                    browser.new_context(
                        viewport={"width": w, "height": h}, device_scale_factor=2,
                        is_mobile=True, has_touch=True,
                    )
                    if mobile
                    else browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2)
                )
                ctx.add_cookies([{"name": "cloudnetp_session", "value": create_token(uid, ver), "url": base}])
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                return ctx, page, errors

            print("\n[A] 页签结构与默认页")
            ctx, page, errors = make_page()
            page.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            page.wait_for_selector("[data-settings-tabs] .segmented__btn", timeout=20000)
            page.wait_for_timeout(700)

            structure = page.evaluate(
                """() => {
                  const btns = [...document.querySelectorAll('[data-settings-tab]')];
                  const panes = [...document.querySelectorAll('[data-settings-pane]')];
                  const thumb = document.querySelector('[data-tab-thumb]');
                  return {
                    tabs: btns.map((b) => b.dataset.settingsTab),
                    active: btns.filter((b) => b.classList.contains('is-active')).map((b) => b.dataset.settingsTab),
                    panes: panes.map((p) => [p.dataset.settingsPane, !p.hidden]),
                    thumbW: Math.round(thumb.getBoundingClientRect().width),
                  };
                }"""
            )
            check(
                "五个页签",
                structure["tabs"] == ["appearance", "mascot", "files", "quick", "account"],
                str(structure["tabs"]),
            )
            check("默认停在外观页", structure["active"] == ["appearance"], str(structure["active"]))
            check(
                "只有外观页可见",
                structure["panes"]
                == [
                    ["appearance", True],
                    ["mascot", False],
                    ["files", False],
                    ["quick", False],
                    ["account", False],
                ],
                str(structure["panes"]),
            )
            check("页签滑块排开了", structure["thumbW"] > 20, str(structure["thumbW"]))

            print("\n[A2] 滑块贴合标签 / 页签按文字宽度排 / 切换有过渡")
            geo = page.evaluate(
                """() => {
                  const seg = document.querySelector('[data-settings-tabs]');
                  const thumb = document.querySelector('[data-tab-thumb]');
                  const btns = [...document.querySelectorAll('[data-settings-tab]')];
                  const active = btns.find((b) => b.classList.contains('is-active'));
                  const tr = thumb.getBoundingClientRect();
                  const ar = active.getBoundingClientRect();
                  return {
                    dx: Math.round(tr.left - ar.left), dy: Math.round(tr.top - ar.top),
                    dw: Math.round(tr.width - ar.width), dh: Math.round(tr.height - ar.height),
                    widths: btns.map((b) => Math.round(b.getBoundingClientRect().width)),
                    thumbBg: getComputedStyle(thumb).backgroundColor,
                  };
                }"""
            )
            check(
                "滑块与选中页签严丝合缝",
                max(abs(geo["dx"]), abs(geo["dy"]), abs(geo["dw"]), abs(geo["dh"])) <= 1,
                str(geo),
            )
            check("页签按各自文字宽度排（不再等宽）", len(set(geo["widths"])) > 1, str(geo["widths"]))

            anim = None
            page.evaluate(
                """() => {
                  window.__tabAnims = [];
                  const thumb = document.querySelector('[data-tab-thumb]');
                  const btn = document.querySelector('[data-settings-tab="files"]');
                  btn.addEventListener('click', () => {
                    requestAnimationFrame(() =>
                      window.__tabAnims.push(thumb.getAnimations().map((a) => a.transitionProperty + ':' + a.playState))
                    );
                  }, { once: true });
                }"""
            )
            page.click('[data-settings-tab="files"]')
            page.wait_for_timeout(120)
            anim = page.evaluate(
                """() => {
                  const pane = [...document.querySelectorAll('[data-settings-pane]')].find((p) => !p.hidden);
                  const thumb = document.querySelector('[data-tab-thumb]');
                  return {
                    running: thumb.getAnimations().filter((a) => a.playState === 'running').length,
                    fps: window.__tabAnims.flat(),
                    paneAnim: pane ? getComputedStyle(pane).animationName : null,
                    paneRunning: pane ? pane.getAnimations().some((a) => a.playState === 'running') : false,
                  };
                }"""
            )
            check("切换时滑块在动（transform 过渡真的跑起来）", anim["running"] > 0, str(anim))
            check("内容层有淡入动画", anim["paneAnim"] == "settings-pane-in" and anim["paneRunning"], str(anim))
            page.click('[data-settings-tab="appearance"]')
            page.wait_for_timeout(500)

            page.screenshot(path=str(OUT / "settings_tabs_appearance.png"))

            print("\n[B] 切页签：不重建 DOM，目标卡可见")
            ai_card_before = page.evaluate("document.querySelector('[data-ai-card]') !== null")
            page.click('[data-settings-tab="mascot"]')
            page.wait_for_timeout(400)
            mascot = page.evaluate(
                """() => {
                  const pane = document.querySelector('[data-settings-pane="mascot"]');
                  const ai = document.querySelector('[data-ai-card]');
                  const appear = document.querySelector('[data-settings-pane="appearance"]');
                  return {
                    paneVisible: !pane.hidden,
                    aiAttached: !!ai,
                    appearHidden: appear.hidden,
                    saved: localStorage.getItem('settings-tab'),
                  };
                }"""
            )
            check("看板娘页可见", mascot["paneVisible"], str(mascot))
            check("AI 卡在这页里", ai_card_before and mascot["aiAttached"], str(mascot))
            check("外观页收起", mascot["appearHidden"], str(mascot))
            check("页签记进了 localStorage", mascot["saved"] == "mascot", str(mascot["saved"]))
            page.screenshot(path=str(OUT / "settings_tabs_mascot.png"))

            print("\n[C] 切回外观页：主题模式滑块要重新排")
            page.click('[data-settings-tab="appearance"]')
            page.wait_for_timeout(400)
            thumb = page.evaluate(
                """() => {
                  const seg = document.querySelector('[data-theme-seg]');
                  const th = seg.querySelector('[data-thumb]');
                  return { w: Math.round(th.getBoundingClientRect().width),
                           x: Math.round(th.getBoundingClientRect().left - seg.getBoundingClientRect().left) };
                }"""
            )
            check("主题模式滑块不是 0 宽", thumb["w"] > 20, str(thumb))

            print("\n[D] 文件与分享 / 账号页")
            page.click('[data-settings-tab="files"]')
            page.wait_for_timeout(350)
            files = page.evaluate(
                """() => ({
                  trash: !!document.querySelector('[data-toggle="trashEnabled"]'),
                  dav: !!document.querySelector('[data-dav-url]'),
                  share: !!document.querySelector('[data-toggle="shareShowName"]'),
                  visible: !document.querySelector('[data-settings-pane="files"]').hidden,
                })"""
            )
            check("文件页三张卡都在", all(files.values()), str(files))
            page.screenshot(path=str(OUT / "settings_tabs_files.png"))

            page.click('[data-settings-tab="account"]')
            page.wait_for_timeout(350)
            account = page.evaluate(
                """() => ({
                  profile: !!document.querySelector('[data-act="profile-save"]'),
                  danger: !!document.querySelector('[data-card="danger"]'),
                  visible: !document.querySelector('[data-settings-pane="account"]').hidden,
                })"""
            )
            check("账号页资料卡与危险区都在", all(account.values()), str(account))
            page.screenshot(path=str(OUT / "settings_tabs_account.png"))

            print("\n[E] 刷新后还在账号页")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("[data-settings-tabs] .segmented__btn", timeout=20000)
            page.wait_for_timeout(500)
            after = page.evaluate(
                """() => ({
                  active: document.querySelector('[data-settings-tab].is-active')?.dataset.settingsTab,
                  accountVisible: !document.querySelector('[data-settings-pane="account"]').hidden,
                })"""
            )
            check("刷新后仍在账号页", after["active"] == "account" and after["accountVisible"], str(after))
            ctx.close()

            print("\n[F] 手机竖屏")
            mctx, mpage, merrors = make_page(390, 844, mobile=True)
            mpage.goto(f"{base}/#/settings", wait_until="domcontentloaded")
            mpage.wait_for_selector("[data-settings-tabs] .segmented__btn", timeout=20000)
            mpage.wait_for_timeout(700)
            mpage.click('[data-settings-tab="files"]')
            mpage.wait_for_timeout(350)
            mobile_state = mpage.evaluate(
                """() => {
                  const bar = document.querySelector('.settings-tabs');
                  const seg = bar.querySelector('.segmented');
                  const pane = document.querySelector('[data-settings-pane="files"]');
                  const last = [...seg.querySelectorAll('[data-settings-tab]')].pop();
                  return {
                    paneVisible: !pane.hidden,
                    barW: Math.round(bar.getBoundingClientRect().width),
                    segClipped: seg.scrollWidth > seg.clientWidth + 1,
                    lastRight: Math.round(last.getBoundingClientRect().right),
                    barRight: Math.round(bar.getBoundingClientRect().right),
                    noHOverflow: document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1,
                  };
                }"""
            )
            check("手机上能切到文件页", mobile_state["paneVisible"], str(mobile_state))
            check("五个页签不用横向滚也能看完", not mobile_state["segClipped"], str(mobile_state))
            check(
                "最后一个页签没被裁掉",
                mobile_state["lastRight"] <= mobile_state["barRight"] + 1,
                str(mobile_state),
            )
            check("页面没有横向溢出", mobile_state["noHOverflow"], str(mobile_state))
            mpage.screenshot(path=str(OUT / "settings_tabs_mobile.png"))
            mctx.close()

            print("\n[G] 花壁纸 + 卡片不透明度 25%：页签仍要读得清")
            # 造一张高饱和壁纸塞进 --bg-image，模拟「自定义图片壁纸 + 调低卡片不透明度」
            import base64
            import io

            from PIL import Image as PILImage

            buf = io.BytesIO()
            tile = PILImage.new("RGB", (64, 64))
            for ty_ in range(64):
                for tx_ in range(64):
                    tile.putpixel((tx_, ty_), ((tx_ * 7) % 256, (ty_ * 11) % 256, (tx_ * ty_ * 3) % 256))
            tile.save(buf, format="PNG")
            data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

            def rel_lum(c):
                def f(v):
                    v /= 255
                    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

                r, g, b = (f(v) for v in c)
                return 0.2126 * r + 0.7152 * g + 0.0722 * b

            def contrast(c1, c2):
                l1, l2 = rel_lum(c1), rel_lum(c2)
                hi, lo = max(l1, l2), min(l1, l2)
                return round((hi + 0.05) / (lo + 0.05), 2)

            for scheme in ("dark", "light"):
                wctx = browser.new_context(
                    viewport={"width": 1440, "height": 1000},
                    device_scale_factor=2,
                    color_scheme=scheme,
                )
                wctx.add_cookies(
                    [{"name": "cloudnetp_session", "value": create_token(uid, ver), "url": base}]
                )
                wpage = wctx.new_page()
                wpage.goto(f"{base}/#/settings", wait_until="domcontentloaded")
                wpage.wait_for_selector("[data-settings-tabs] .segmented__btn", timeout=20000)
                wpage.evaluate(
                    """(img) => {
                      document.documentElement.dataset.wallpaper = 'custom';
                      document.documentElement.style.setProperty('--bg-image', `url("${img}")`);
                      document.documentElement.style.setProperty('--op-card', '0.25');
                    }""",
                    data_uri,
                )
                wpage.click('[data-settings-tab="files"]')
                wpage.wait_for_timeout(700)

                geo = wpage.evaluate(
                    """() => {
                      const seg = document.querySelector('[data-settings-tabs]');
                      const thumb = document.querySelector('[data-tab-thumb]');
                      const btn = document.querySelector('[data-settings-tab="files"]');
                      const tr = thumb.getBoundingClientRect();
                      const br = btn.getBoundingClientRect();
                      const btns = [...seg.querySelectorAll('[data-settings-tab]')];
                      const next = btns[btns.indexOf(btn) + 1];
                      return {
                        pill: { x: tr.left, y: tr.top, w: tr.width, h: tr.height },
                        text: { x: br.left, y: br.top, w: br.width, h: br.height },
                        gapX: next ? (br.right + next.getBoundingClientRect().left) / 2 : br.right + 4,
                        midY: br.top + br.height / 2,
                      };
                    }"""
                )
                shot = OUT / f"settings_tabs_wallpaper_{scheme}.png"
                wpage.screenshot(path=str(shot))
                wctx.close()

                im = PILImage.open(shot).convert("RGB")
                S = 2  # device_scale_factor

                def px(x, y):
                    return im.getpixel((int(x * S), int(y * S)))

                pill_bg = px(geo["pill"]["x"] + 6, geo["midY"])
                strip_bg = px(geo["gapX"], geo["midY"])
                tx, ty = geo["text"]["x"], geo["text"]["y"]
                tw, th = geo["text"]["w"], geo["text"]["h"]
                samples = [
                    im.getpixel((int((tx + dx) * S), int((ty + dy) * S)))
                    for dx in range(2, max(3, int(tw) - 2), 2)
                    for dy in range(5, max(6, int(th) - 5), 2)
                ]
                brightest = max(samples, key=rel_lum)
                darkest = min(samples, key=rel_lum)
                # 文字色取「跟色块差得最远」的那个样本（浅色主题下文字比底色暗，
                # 光看最亮会把色块本身当成文字）
                text_col = max(
                    (brightest, darkest), key=lambda c: abs(rel_lum(c) - rel_lum(pill_bg))
                )

                check(
                    f"[{scheme}] 选中块与页签底色分得开",
                    contrast(pill_bg, strip_bg) >= 1.18,
                    f"pill={pill_bg} strip={strip_bg} → {contrast(pill_bg, strip_bg)}:1",
                )
                check(
                    f"[{scheme}] 选中页签文字在色块上读得清",
                    contrast(text_col, pill_bg) >= 4.5,
                    f"text={text_col} pill={pill_bg} → {contrast(text_col, pill_bg)}:1",
                )

            print("\n[H] 页签条不能被 .view-body 的滚动裁剪切掉上边缘")
            # .view-body 是 overflow:auto 的滚动容器，它按内容框裁剪子元素：
            # 页签条一旦用负的上外边距顶出去，上边缘 / 圆角 / 描边就被切平
            # （125% 缩放下尤其明显，看着像「上面被什么东西挡住了」）
            for dpr in (1, 1.25, 1.5, 2):
                zctx = browser.new_context(
                    viewport={"width": 1280, "height": 800}, device_scale_factor=dpr
                )
                zctx.add_cookies(
                    [{"name": "cloudnetp_session", "value": create_token(uid, ver), "url": base}]
                )
                zpage = zctx.new_page()
                zpage.goto(f"{base}/#/settings", wait_until="domcontentloaded")
                zpage.wait_for_selector("[data-settings-tabs] .segmented__btn", timeout=20000)
                zpage.wait_for_timeout(800)
                zgeo = zpage.evaluate(
                    """() => {
                      const body = document.querySelector('.view-body');
                      const head = document.querySelector('.view-head');
                      const bar = document.querySelector('.settings-tabs');
                      const seg = document.querySelector('[data-settings-tabs]');
                      const br = body.getBoundingClientRect();
                      const hr = head.getBoundingClientRect();
                      const wr = bar.getBoundingClientRect();
                      const sr = seg.getBoundingClientRect();
                      return {
                        bodyTop: br.top,
                        headBottom: hr.bottom,
                        barTop: wr.top,
                        segTop: sr.top,
                        gap: Math.round(sr.top - hr.bottom),
                        segRadius: getComputedStyle(seg).borderTopLeftRadius,
                      };
                    }"""
                )
                check(
                    f"[{dpr}x] 页签条没被滚出内容框（bar {zgeo['barTop']:.1f} ≥ body {zgeo['bodyTop']:.1f}）",
                    zgeo["barTop"] >= zgeo["bodyTop"] - 0.5,
                    str(zgeo),
                )
                check(
                    f"[{dpr}x] 页签容器上描边与圆角完整（seg {zgeo['segTop']:.1f} > body）",
                    zgeo["segTop"] > zgeo["bodyTop"] + 0.5 and zgeo["segRadius"] != "0px",
                    str(zgeo),
                )
                check(
                    f"[{dpr}x] 与标题的间距不突兀（{zgeo['gap']}px）",
                    0 <= zgeo["gap"] <= 24,
                    str(zgeo),
                )
                zctx.close()

            check("没有 JS 报错", not errors and not merrors, str((errors + merrors)[:1]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_file.close()

    print()
    print(f"通过 {PASS} / {PASS + FAIL}，截图在 {OUT}")
    return 1 if FAIL else 0


sys.exit(main())
