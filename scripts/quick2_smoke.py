# -*- coding: utf-8 -*-
"""快传取件码增强 + 我的分享分页 的离线联调。

  A 快传上传：quick=true 上传不占配额、不出现在「我的文件」/搜索里，只能按 id 判 404
  B 逐文件设置：两个文件各自有效期/次数，一个用完不影响另一个
  C 登录取件：登录用户查码 → 转存到自己的网盘（占自己的配额）
  D 后台限额：最长有效期 / 下载次数上限 / 过期保留天数，超出按上限算
  E 后台清理：撤销 + 保留期 0 → 立即回收快传文件，用户自己的文件不受影响
  F 分享分页：pageSize 生效、total/pages 正确
  G 分享记录：失效记录删除、重新分享生成新链接

    ./.venv/Scripts/python.exe scripts/quick2_smoke.py
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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


def call(base: str, method: str, path: str, cookie: str | None = None, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", f"cloudnetp_session={cookie}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw}


def put_bytes(base: str, path: str, data: bytes, cookie: str | None = None):
    req = urllib.request.Request(f"{base}{path}", data=data, method="PUT")
    req.add_header("Content-Type", "application/octet-stream")
    if cookie:
        req.add_header("Cookie", f"cloudnetp_session={cookie}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw}


def fetch_bytes(base: str, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def upload_quick(base: str, cookie: str, name: str, content: bytes) -> dict:
    """走快传通道上传（quick=true）：不落目录、不占配额。"""
    status, data = call(
        base, "POST", "/api/uploads/init", cookie, {"name": name, "size": len(content), "quick": True}
    )
    if status != 200:
        raise SystemExit(f"quick init 失败：{status} {data}")
    sid = data.get("sessionId")
    chunk = data.get("chunkSize") or len(content)
    for index in data.get("missing") or []:
        start = index * chunk
        status, _ = put_bytes(
            base, f"/api/uploads/{sid}/{index}", content[start : start + chunk], cookie
        )
        if status != 200:
            raise SystemExit(f"分片 {index} 失败：{status}")
    status, done = call(base, "POST", f"/api/uploads/{sid}/complete", cookie)
    if status != 200:
        raise SystemExit(f"quick complete 失败：{status} {done}")
    return done["node"]


def upload_normal(base: str, cookie: str, name: str, content: bytes) -> dict:
    status, data = call(
        base, "POST", "/api/uploads/init", cookie, {"name": name, "size": len(content)}
    )
    if status != 200:
        raise SystemExit(f"init 失败：{status} {data}")
    sid = data.get("sessionId")
    chunk = data.get("chunkSize") or len(content)
    for index in data.get("missing") or []:
        start = index * chunk
        status, _ = put_bytes(
            base, f"/api/uploads/{sid}/{index}", content[start : start + chunk], cookie
        )
        if status != 200:
            raise SystemExit(f"分片 {index} 失败：{status}")
    status, done = call(base, "POST", f"/api/uploads/{sid}/complete", cookie)
    if status != 200:
        raise SystemExit(f"complete 失败：{status} {done}")
    return done["node"]


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


def days_between(iso_text: str) -> float:
    stamp = datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
    return (stamp - datetime.now(timezone.utc)).total_seconds() / 86400


def main() -> int:
    from app.config import settings
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-quick2-")).resolve()
    db_copy = tmp / "smoke.db"
    snapshot_db(db_copy)

    con = sqlite3.connect(str(db_copy))
    for stmt in (
        "delete from quick_transfers",
        "delete from quick_pickup_items",
        "delete from share_reports",
        "delete from blocked_blobs",
    ):
        try:
            con.execute(stmt)
        except sqlite3.OperationalError:
            pass
    users = con.execute(
        "select id, username, token_version from users order by role desc, id limit 2"
    ).fetchall()
    if len(users) < 2:
        raise SystemExit("库里至少要有两个账号（管理员 + 普通用户）")
    admin_id, admin_name, admin_tv = users[0]
    user_id, user_name, user_tv = users[1]
    hashed = hash_password(PASSWORD)
    con.execute("update users set password_hash=? where id in (?,?)", (hashed, admin_id, user_id))
    try:
        con.execute(
            "update user_settings set quick_terms_at=datetime('now') where user_id in (?,?)",
            (admin_id, user_id),
        )
    except sqlite3.OperationalError:
        pass
    con.commit()
    con.close()

    admin_cookie = create_token(admin_id, admin_tv)
    user_cookie = create_token(user_id, user_tv)

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

        stamp = str(int(time.time()))
        # ---------------- A 快传上传 ----------------
        print("\n[A] 快传上传不占配额、不进我的文件")
        st, me_before = call(base, "GET", "/api/me", admin_cookie)
        used_before = me_before["user"]["usedBytes"]
        quick_one = upload_quick(base, admin_cookie, f"快传样本A-{stamp}.txt", f"quick-a-{stamp}".encode() * 60)
        check("快传上传成功", bool(quick_one.get("id")), str(quick_one.get("id")))
        st, me_after = call(base, "GET", "/api/me", admin_cookie)
        check("配额没有变化", me_after["user"]["usedBytes"] == used_before, f"{used_before} -> {me_after['user']['usedBytes']}")

        st, listing = call(base, "GET", "/api/nodes", admin_cookie)
        names = [item["name"] for item in listing.get("items") or []]
        check("不出现在我的文件", quick_one["name"] not in names, str(names[:5]))
        st, found = call(base, "GET", f"/api/search?q={urllib.parse.quote(quick_one['name'])}", admin_cookie)
        hits = [item["name"] for item in found.get("items") or []]
        check("搜索也搜不到", quick_one["name"] not in hits, str(hits))
        st, _ = call(base, "GET", f"/api/nodes/{quick_one['id']}", admin_cookie)
        check("按 id 访问 404", st == 404, str(st))

        # ---------------- B 逐文件有效期 / 次数 ----------------
        print("\n[B] 逐文件设置")
        quick_two = upload_quick(base, admin_cookie, f"快传样本B-{stamp}.txt", f"quick-b-{stamp}".encode() * 40)
        st, data = call(
            base,
            "POST",
            "/api/quick/pickups",
            admin_cookie,
            {
                "items": [
                    {"nodeId": quick_one["id"], "days": 1, "maxUses": 1},
                    {"nodeId": quick_two["id"], "days": 7},
                ]
            },
        )
        check("按 items 建码 200", st == 200, str(st))
        pickup = data.get("pickup") or {}
        code = pickup.get("code")
        files = {item["name"]: item for item in pickup.get("items") or []}
        one = files.get(quick_one["name"], {})
        two = files.get(quick_two["name"], {})
        check("第一个文件 1 次上限", one.get("maxUses") == 1 and one.get("remaining") == 1, str(one))
        check("第二个文件不限次数", two.get("maxUses") is None and two.get("remaining") is None, str(two))
        check("第一个文件 1 天到期", 0.5 < days_between(one.get("expiresAt")) <= 1.05, str(one.get("expiresAt")))
        check("第二个文件 7 天到期", 6.5 < days_between(two.get("expiresAt")) <= 7.05, str(two.get("expiresAt")))

        st, body1 = fetch_bytes(base, f"/api/public/quick/pickup/{code}/download?nodeId={quick_one['id']}")
        check("第一个文件下载成功", st == 200 and body1, str(st))
        st, _ = fetch_bytes(base, f"/api/public/quick/pickup/{code}/download?nodeId={quick_one['id']}")
        check("第一个文件次数用完 410", st == 410, str(st))
        st, body2 = fetch_bytes(base, f"/api/public/quick/pickup/{code}/download?nodeId={quick_two['id']}")
        check("第二个文件仍可下载", st == 200 and body2, str(st))
        st, meta = call(base, "GET", f"/api/public/quick/pickup/{code}")
        check("码本身仍然有效", st == 200, str(st))
        check("只剩 1 个可下载", (meta.get("pickup") or {}).get("aliveCount") == 1, str((meta.get("pickup") or {}).get("aliveCount")))

        # ---------------- C 登录后取件 / 转存 ----------------
        print("\n[C] 登录取件与转存")
        st, info = call(base, "GET", f"/api/quick/pickups/code/{code}", user_cookie)
        check("登录后可查询取件码", st == 200 and (info.get("pickup") or {}).get("code") == code, str(st))
        check("不是自己的码", info.get("own") is False, str(info.get("own")))
        st, me_b = call(base, "GET", "/api/me", user_cookie)
        used_b = me_b["user"]["usedBytes"]
        st, saved = call(base, "POST", f"/api/quick/pickups/code/{code}/save", user_cookie, {"parentId": None})
        check("转存成功", st == 200 and saved.get("saved") == 1, f"{st} {saved.get('saved')}")
        st, me_b2 = call(base, "GET", "/api/me", user_cookie)
        check("转存后占用自己的配额", me_b2["user"]["usedBytes"] > used_b, f"{used_b} -> {me_b2['user']['usedBytes']}")
        st, listing_b = call(base, "GET", "/api/nodes", user_cookie)
        names_b = [item["name"] for item in listing_b.get("items") or []]
        check("文件出现在对方网盘", quick_two["name"] in names_b, str(names_b[:5]))

        # ---------------- D 后台限额 ----------------
        print("\n[D] 后台限额")
        st, _ = call(
            base,
            "PATCH",
            "/api/admin/settings",
            admin_cookie,
            {"quickMaxDays": 2, "quickMaxUses": 3, "quickRetentionDays": 0},
        )
        check("保存限额 200", st == 200, str(st))
        st, limits = call(base, "GET", "/api/quick/limits", admin_cookie)
        check(
            "限额生效",
            limits.get("limits", {}).get("maxDays") == 2
            and limits.get("limits", {}).get("maxUses") == 3,
            str(limits.get("limits")),
        )
        st, capped = call(
            base,
            "POST",
            "/api/quick/pickups",
            admin_cookie,
            {"items": [{"nodeId": quick_two["id"], "days": 30, "maxUses": 10}]},
        )
        item = ((capped.get("pickup") or {}).get("items") or [{}])[0]
        check("有效期被压到 2 天", 1.5 < days_between(item.get("expiresAt")) <= 2.05, str(item.get("expiresAt")))
        check("次数被压到 3 次", item.get("maxUses") == 3, str(item.get("maxUses")))
        capped_code = (capped.get("pickup") or {}).get("code")

        # ---------------- E 清理 ----------------
        print("\n[E] 清理过期快传文件")
        normal = upload_normal(base, admin_cookie, f"我的正常文件-{stamp}.txt", f"normal-{stamp}".encode() * 30)
        st, _ = call(base, "DELETE", f"/api/quick/pickups/{pickup.get('id')}", admin_cookie)
        check("撤销原取件码", st == 200, str(st))
        st, ov = call(base, "GET", "/api/admin/quick", admin_cookie)
        check(
            "后台概况能读到",
            st == 200 and (ov.get("quick") or {}).get("files", 0) >= 1,
            str(ov.get("quick")),
        )
        st, _ = call(base, "POST", "/api/admin/quick/cleanup", admin_cookie, {"days": 0})
        cleaned = (_ or {}).get("cleaned") or {}
        check("清理接口 200", st == 200, str(st))
        check("删掉了快传文件", (cleaned.get("files") or 0) >= 1, str(cleaned))
        st, _ = call(base, "GET", f"/api/public/quick/pickup/{code}")
        check("旧码已不存在", st in (404, 410), str(st))
        st, listing = call(base, "GET", "/api/nodes", admin_cookie)
        names = [item["name"] for item in listing.get("items") or []]
        check("用户自己的文件还在", normal["name"] in names, str(names[:5]))
        # 上限改回宽松，避免影响后面的用例
        call(base, "PATCH", "/api/admin/settings", admin_cookie, {"quickMaxDays": 30, "quickMaxUses": 0, "quickRetentionDays": 7})

        # ---------------- F 分享分页 ----------------
        print("\n[F] 我的分享分页")
        created = []
        for index in range(12):
            st, data = call(
                base,
                "POST",
                "/api/shares",
                admin_cookie,
                {"nodeId": normal["id"], "expiresDays": 7},
            )
            if st == 200:
                created.append(data["share"]["id"])
        check("建了 12 条分享", len(created) == 12, str(len(created)))
        st, page1 = call(base, "GET", "/api/shares?page=1&pageSize=10", admin_cookie)
        check("pageSize=10 生效", len(page1.get("items") or []) == 10, str(len(page1.get("items") or [])))
        check("total 正确", page1.get("total", 0) >= 12, str(page1.get("total")))
        check("pages 至少 2 页", page1.get("pages", 0) >= 2, str(page1.get("pages")))
        st, page2 = call(base, "GET", "/api/shares?page=2&pageSize=10", admin_cookie)
        check("第二页有内容", len(page2.get("items") or []) >= 2, str(len(page2.get("items") or [])))
        st, small = call(base, "GET", "/api/shares?page=1&pageSize=5", admin_cookie)
        check("pageSize=5 生效", len(small.get("items") or []) == 5, str(len(small.get("items") or [])))

        # ---------------- G 删除记录 / 重新分享 ----------------
        print("\n[G] 删除记录与重新分享")
        target = created[0]
        st, data = call(base, "DELETE", f"/api/shares/{target}", admin_cookie)
        check("取消有效分享", st == 200 and data.get("revoked") is True, str(data))
        st, data = call(base, "POST", f"/api/shares/{target}/renew", admin_cookie)
        renewed = data.get("share") or {}
        check("重新分享 200", st == 200 and renewed.get("id"), str(st))
        check("状态变回有效", renewed.get("status") == "active", str(renewed.get("status")))
        check("拿到了新 token", renewed.get("token") != "", str(renewed.get("token"))[:10])
        # 重新分享出来的新链接可以直接取消；再删一次就是删除记录
        renewed_id = renewed.get("id")
        st, data = call(base, "DELETE", f"/api/shares/{renewed_id}", admin_cookie)
        check("取消新链接", st == 200 and data.get("revoked") is True, str(data))
        st, data = call(base, "DELETE", f"/api/shares/{renewed_id}", admin_cookie)
        check("失效记录可删除", st == 200 and data.get("deleted") is True, str(data))
        st, page_all = call(base, "GET", "/api/shares?page=1&pageSize=100", admin_cookie)
        ids = [item["id"] for item in page_all.get("items") or []]
        check("删除后列表里没有", renewed_id not in ids, str(renewed_id))

        print(f"\n结果：{passed} 通过，{failed} 失败")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_file.close()

    if failed == 0:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print("临时目录保留：", tmp)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
