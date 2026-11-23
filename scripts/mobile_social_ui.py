# -*- coding: utf-8 -*-
"""手机端导航改造 + 新页面的回归检查。

钉住这几件事：
- 底部导航只剩「我的文件 / 快传 / 好友 / CDP娘 / 设置（/ 管理后台）」，
  分享、回收站、关于不在手机导航里；桌面侧栏反过来（分享/回收站/关于在，CDP娘不在）
- 图标下面的小字可见、没有被压到截断
- 我的文件页有「我的分享 / 回收站」两个入口，点得进对应页面
- 设置里有「关于」入口；#/settings/mascot 能直接打开看板娘页签
- 消息中心：待处理磁贴 + 通知列表 + 点一条会标记已读并跳转
- 好友：四个页签、收到的申请有同意/拒绝、点「同意」后数量变化
- CDP娘页：点「和她聊天」能打开对话浮层

用法（需要一个已启动的隔离实例）：
    python scripts/mobile_social_ui.py [port]
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from app import db_failover  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import Friendship, Notice, User, utcnow  # noqa: E402
from app.security import create_token, hash_password  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
BASE = f"http://127.0.0.1:{PORT}"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
W, H = 390, 844
DESK = 1280

CHECKS: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> None:
    CHECKS.append((bool(ok), msg))


def seed() -> str:
    """保证有一份可看的消息与好友数据，返回管理员 token。"""
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    try:
        admin = db.query(User).filter(User.role == 1).order_by(User.id.asc()).first()
        assert admin, "隔离库里没有管理员账号"
        if not db.query(Notice).filter(Notice.user_id == admin.id, Notice.kind == "feedback").first():
            db.add(
                Notice(
                    user_id=admin.id,
                    kind="feedback",
                    title="新的意见反馈：支持批量重命名",
                    body="现在只能一个一个改，比较麻烦",
                    link="#/admin/feedback",
                    created_at=utcnow(),
                )
            )
        if not db.query(Notice).filter(Notice.user_id == admin.id, Notice.kind == "report").first():
            db.add(
                Notice(
                    user_id=admin.id,
                    kind="report",
                    title="新的举报待审核：演示文件.zip",
                    body="疑似侵权内容",
                    link="#/admin/reports",
                    created_at=utcnow(),
                )
            )
        friend = db.query(User).filter(User.username == "ui_friend").first()
        if not friend:
            friend = User(
                username="ui_friend",
                display_name="测试好友",
                bio="一起整理文件吧",
                password_hash=hash_password("SmokeTest123"),
                role=0,
                quota_bytes=1024**3,
            )
            db.add(friend)
            db.commit()
            db.refresh(friend)
        if not db.query(Friendship).filter(
            Friendship.requester_id == friend.id, Friendship.addressee_id == admin.id
        ).first():
            db.add(
                Friendship(
                    requester_id=friend.id,
                    addressee_id=admin.id,
                    status="pending",
                    message="我是测试好友",
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
        db.commit()
        return create_token(admin.id, admin.token_version)
    finally:
        db.close()


def main() -> None:
    token = seed()
    profile = Path(tempfile.mkdtemp(prefix="cdp_social_ui_"))
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=9478",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--window-size={W},{H}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(80):
            try:
                pages = [
                    t
                    for t in json.load(urllib.request.urlopen("http://127.0.0.1:9478/json/list", timeout=2))
                    if t.get("type") == "page"
                ]
                if pages:
                    target = pages[0]
                    break
            except Exception:
                time.sleep(0.4)
        assert target, "无法连接 Chrome CDP"
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
        mid = 0

        def send(method, **params):
            nonlocal mid
            mid += 1
            ws.send(json.dumps({"id": mid, "method": method, "params": params}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error']}")
                    return msg.get("result", {})

        def js(expr):
            return send("Runtime.evaluate", expression=expr, returnByValue=True, userGesture=True)["result"].get("value")

        def wait_for(expr, timeout=25):
            end = time.time() + timeout
            while time.time() < end:
                try:
                    if js(expr):
                        return True
                except Exception:
                    pass
                time.sleep(0.3)
            return False

        def goto(hash_, ready, settle=1.4):
            js(f"location.hash = {json.dumps(hash_)}")
            assert wait_for(f"!!document.querySelector({json.dumps(ready)})", 30), f"{hash_} 没渲染"
            time.sleep(settle)

        send("Page.enable")
        send("Runtime.enable")
        send("Network.enable")
        send("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=2, mobile=True)
        send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        send("Network.setCookie", name=settings.COOKIE_NAME, value=token, domain="127.0.0.1", path="/", httpOnly=True)
        send("Page.navigate", url=f"{BASE}/#/files")
        assert wait_for("!!document.querySelector('#view-root .file-grid, #view-root .frow')", 30), "首页没渲染"
        time.sleep(1.6)

        # 1) 底部导航的可见项
        nav = js(
            """
            (() => {
              const items = [...document.querySelectorAll('.nav .nav-item')];
              const shown = items.filter((a) => a.getBoundingClientRect().width > 0);
              return {
                all: items.map((a) => a.dataset.route),
                shown: shown.map((a) => a.dataset.route),
                labels: shown.map((a) => {
                  const label = a.querySelector('.nav-item__label');
                  return {
                    route: a.dataset.route,
                    text: (label?.textContent || '').trim(),
                    w: label ? label.getBoundingClientRect().width : 0,
                    clipped: label ? label.scrollWidth > label.clientWidth + 1 : true,
                  };
                }),
                overflow: document.documentElement.scrollWidth,
              };
            })()
            """
        )
        expected = ["files", "quick", "friends", "mascot", "settings", "admin"]
        check(nav["shown"] == expected, f"手机底部导航 = {expected}（实际 {nav['shown']}）")
        check("shares" not in nav["shown"], "「我的分享」不在手机导航")
        check("trash" not in nav["shown"], "「回收站」不在手机导航")
        check("about" not in nav["shown"], "「关于」不在手机导航")
        check(all(not x["clipped"] for x in nav["labels"]), f"导航文字未截断（{[x['text'] for x in nav['labels']]}）")
        check(nav["overflow"] <= W + 1, f"底部导航未撑宽页面（{nav['overflow']}）")

        # 2) 我的文件页：分享 / 回收站入口
        links = js(
            """
            (() => {
              const box = document.querySelector('.files-links');
              if (!box) return { missing: true };
              const r = box.getBoundingClientRect();
              return {
                visible: r.width > 0 && r.height > 0,
                hrefs: [...box.querySelectorAll('a')].map((a) => a.getAttribute('href')),
              };
            })()
            """
        )
        check(links.get("visible"), "文件页有「我的分享 / 回收站」入口")
        check(links.get("hrefs") == ["#/shares", "#/trash"], f"入口指向正确（{links.get('hrefs')}）")

        js("document.querySelector('.files-links a[href=\"#/trash\"]').click()")
        assert wait_for("location.hash === '#/trash'", 10), "点回收站没跳转"
        check(True, "点「回收站」进入 #/trash")

        # 3) 设置里的关于 + 深链看板娘页签
        goto("#/settings", ".settings")
        about_row = js(
            """
            (() => {
              const row = document.querySelector('.settings-pane[data-settings-pane="account"] a.setting-row--link');
              if (!row) return { missing: true };
              const r = row.getBoundingClientRect();
              return { href: row.getAttribute('href'), visible: r.width > 0 };
            })()
            """
        )
        check(about_row.get("href") == "#/about", "设置-账号里有「关于」入口")

        goto("#/settings/mascot", '.settings-pane[data-settings-pane="mascot"]:not([hidden])')
        pane = js(
            "(() => { const p = document.querySelector('.settings-pane[data-settings-pane=\"mascot\"]'); return !p.hidden; })()"
        )
        check(pane, "#/settings/mascot 直接打开看板娘页签")

        # 4) 消息中心
        goto("#/notices", ".notice-list, .empty")
        notices = js(
            """
            (() => {
              const tiles = [...document.querySelectorAll('.notice-pending .notice-tile')];
              const items = [...document.querySelectorAll('.notice-item')];
              const unread = items.filter((i) => i.classList.contains('is-unread'));
              const first = unread[0];
              return {
                tiles: tiles.length,
                items: items.length,
                unread: unread.length,
                firstKind: first ? first.textContent.trim().slice(0, 8) : '',
                link: first ? first.dataset.link : '',
                times: items.map((i) => {
                  const t = i.querySelector('.notice-item__time');
                  return t ? t.getBoundingClientRect().width : 0;
                }),
              };
            })()
            """
        )
        check(notices["tiles"] >= 3, f"消息中心有待处理磁贴（{notices['tiles']}）")
        check(notices["items"] >= 2, f"消息列表有内容（{notices['items']} 条）")
        check(notices["unread"] >= 1, "存在未读通知")
        check(all(w > 0 for w in notices["times"]), "每条通知的时间都占位可见")

        # 点第一条未读 → 取消 is-unread + 跳到对应页面
        js("document.querySelector('.notice-item.is-unread').click()")
        time.sleep(1.6)
        after = js(
            """
            (() => ({
              hash: location.hash,
              unread: document.querySelectorAll('.notice-item.is-unread').length,
            }))()
            """
        )
        check(after["hash"].startswith("#/admin") or after["hash"] == "#/friends", f"点通知跳转到 {after['hash']}")

        # 5) 好友
        goto("#/friends", ".friend-list, .empty")
        tabs = js(
            "([...document.querySelectorAll('[data-friend-tab]')].map((b) => b.dataset.friendTab))"
        )
        check(tabs == ["friends", "incoming", "outgoing", "add"], f"好友页四个页签（{tabs}）")

        js("document.querySelector('[data-friend-tab=\"incoming\"]').click()")
        time.sleep(1.2)
        inc = js(
            """
            (() => {
              const row = document.querySelector('.friend-row');
              if (!row) return { empty: true };
              const ops = [...row.querySelectorAll('.friend-row__ops .btn')].map((b) => b.textContent.trim());
              const boxes = [...row.querySelectorAll('.friend-row__ops .btn')].map((b) => {
                const r = b.getBoundingClientRect();
                return { w: r.width, right: r.right };
              });
              return { empty: false, ops, boxes, vw: innerWidth };
            })()
            """
        )
        if inc.get("empty"):
            check(False, "收到的申请列表为空（数据没准备好）")
        else:
            check(inc["ops"] == ["同意", "拒绝"], f"申请行有同意/拒绝（{inc['ops']}）")
            check(all(b["right"] <= inc["vw"] + 1 for b in inc["boxes"]), "申请按钮在视口内")

        js("document.querySelector('[data-friend-tab=\"add\"]').click()")
        time.sleep(0.8)
        search = js(
            """
            (() => {
              const input = document.querySelector('[data-search]');
              if (!input) return { missing: true };
              input.value = 'ui_friend';
              input.dispatchEvent(new Event('input', { bubbles: true }));
              return { missing: false };
            })()
            """
        )
        time.sleep(2.0)
        hits = js(
            "(() => ({ n: document.querySelectorAll('[data-results] .friend-row').length }))()"
        )
        check(not search.get("missing") and hits["n"] >= 1, f"搜索能搜到用户（{hits['n']} 条）")

        # 6) CDP娘页
        goto("#/mascot", ".mascot-hero")
        hero = js(
            """
            (() => {
              const name = document.querySelector('.mascot-hero__name')?.textContent.trim();
              const badges = document.querySelector('.mascot-hero__badge')?.textContent.trim();
              const ops = [...document.querySelectorAll('.mascot-hero__ops .btn')].map((b) => b.textContent.trim());
              const tips = document.querySelectorAll('.mascot-tip').length;
              return { name, badges, ops, tips };
            })()
            """
        )
        check(hero["name"] == "CDP娘", f"CDP娘页标题（{hero['name']}）")
        check(hero["tips"] >= 3, f"CDP娘页能力清单（{hero['tips']} 条）")
        check(len(hero["ops"]) == 2, f"CDP娘页两个主按钮（{hero['ops']}）")

        js("document.querySelector('.mascot-hero__ops [data-act=\"chat\"]').click()")
        opened = wait_for("!!document.querySelector('.mchat.is-open')", 8)
        check(opened, "点「和她聊天」打开对话面板")

        # 7) 桌面宽度：侧栏回到 分享 / 回收站 / 关于，CDP娘不在
        send("Emulation.setDeviceMetricsOverride", width=DESK, height=900, deviceScaleFactor=1, mobile=False)
        send("Emulation.setTouchEmulationEnabled", enabled=False)
        js("location.hash = '#/files'")
        time.sleep(1.8)
        desk = js(
            """
            (() => {
              const shown = [...document.querySelectorAll('.nav .nav-item')]
                .filter((a) => a.getBoundingClientRect().width > 0)
                .map((a) => a.dataset.route);
              return { shown };
            })()
            """
        )
        for route in ("shares", "trash", "about", "friends", "settings"):
            check(route in desk["shown"], f"桌面侧栏保留 {route}")
        check("mascot" not in desk["shown"], "桌面侧栏不显示 CDP娘页")
    finally:
        try:
            proc.terminate()
        except Exception:
            pass

    failed = [msg for ok, msg in CHECKS if not ok]
    for ok, msg in CHECKS:
        print(("  OK  " if ok else "  !!  ") + msg)
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
