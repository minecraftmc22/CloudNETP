# -*- coding: utf-8 -*-
"""快传离线联调：扫码直传 / 站内快传 / 取件码。

用库副本起独立实例（不动真实库），把三条链路各跑一遍：

  A 扫码直传：建会话 → 匿名 init/分片/complete → 文件落在指定目录 → 撤销后 410
  B 站内快传：A 发给 B → B 根目录出现副本 → 收件箱未读数 → 标已读 → 发给自己报错
  C 取件码：建码 → 匿名查码 → 匿名下载 → 次数用完 410 → 文件夹被拒 → 撤销 410
  D 越权：B 撤销 A 的会话必须 404

    ./.venv/Scripts/python.exe scripts/quick_smoke.py
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
        with urllib.request.urlopen(req, timeout=15) as resp:
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


def upload_file(base: str, cookie: str, name: str, content: bytes, parent_id: int | None = None):
    status, data = call(
        base,
        "POST",
        "/api/uploads/init",
        cookie,
        {"name": name, "size": len(content), "parentId": parent_id},
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


def main() -> int:
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-quick-")).resolve()
    db_copy = tmp / "smoke.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    try:
        con.execute("delete from quick_transfers")
    except sqlite3.OperationalError:
        pass
    users = con.execute(
        "select id, username, token_version from users order by role desc, id limit 2"
    ).fetchall()
    if len(users) < 2:
        con.execute(
            "insert into users (username, password_hash, display_name, quota_bytes, used_bytes, role,"
            " token_version, created_at) values (?,?,?,?,?,?,?,datetime('now'))",
            ("quicksmokeb", "x", "QuickB", 10 * 1024 ** 3, 0, 0, 0),
        )
        con.commit()
        users = con.execute(
            "select id, username, token_version from users where username in"
            " ((select username from users order by role desc, id limit 1), 'quicksmokeb')"
        ).fetchall()
    hashed = hash_password(PASSWORD)
    for row in users:
        con.execute("update users set password_hash=? where id=?", (hashed, row[0]))
    con.commit()
    con.close()

    a_id, a_name, a_ver = users[0]
    b_id, b_name, b_ver = users[1]
    a_cookie = create_token(a_id, a_ver)
    b_cookie = create_token(b_id, b_ver)

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
            "SECRET_KEY": __import__("app.config", fromlist=["settings"]).settings.SECRET_KEY,
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

        # ---------------- A 扫码直传 ----------------
        print("\n[A] 扫码直传")
        status, folder = call(base, "POST", "/api/nodes/folder", a_cookie, {"name": "手机相册"})
        check("建目标目录", status == 201, str(status))
        target_id = (folder.get("node") or {}).get("id")
        if not target_id:
            raise SystemExit(f"建目录返回异常：{folder}")

        status, data = call(base, "POST", "/api/quick/drops", a_cookie, {"parentId": target_id, "minutes": 30})
        drop = data.get("drop") or {}
        check("创建会话 200", status == 200, str(status))
        check("返回 token 与 url", bool(drop.get("token")) and str(drop.get("url", "")).startswith("/drop/"), str(drop.get("url")))

        token = drop.get("token")
        status, meta = call(base, "GET", f"/api/public/quick/drop/{token}")
        check("匿名读取会话 200", status == 200, str(status))
        check("目标目录名正确", (meta.get("drop") or {}).get("folder") == "手机相册", str((meta.get("drop") or {}).get("folder")))

        payload = "hello 快传".encode("utf-8") * 500
        status, init = call(
            base,
            "POST",
            f"/api/public/quick/drop/{token}/init",
            None,
            {"name": "相册-01.txt", "size": len(payload), "mime": "text/plain", "chunkSize": 0},
        )
        check("匿名 init 200", status == 200, str(status))
        sid = init.get("sessionId")
        if not sid:
            raise SystemExit(f"init 没给 sessionId：{init}")
        chunk = init.get("chunkSize") or len(payload)
        for index in init.get("missing") or []:
            start = index * chunk
            status, _ = put_bytes(
                base,
                f"/api/public/quick/drop/{token}/{sid}/{index}",
                payload[start : start + chunk],
            )
            check(f"匿名分片 {index} 200", status == 200, str(status))
        status, done = call(base, "POST", f"/api/public/quick/drop/{token}/{sid}/complete")
        check("匿名 complete 200", status == 200, str(status))
        node_id = (done.get("node") or {}).get("id")

        status, listing = call(base, "GET", f"/api/nodes?parentId={target_id}", a_cookie)
        names = [item["name"] for item in listing.get("items") or []]
        check("文件落在指定目录", "相册-01.txt" in names, str(names))

        status, meta = call(base, "GET", f"/api/public/quick/drop/{token}")
        check("会话里能看到已收文件", (meta.get("drop") or {}).get("count") == 1, str((meta.get("drop") or {}).get("count")))

        status, _ = call(base, "DELETE", f"/api/quick/drops/{drop.get('id')}", b_cookie)
        check("别人撤销 404", status == 404, str(status))
        status, _ = call(base, "DELETE", f"/api/quick/drops/{drop.get('id')}", a_cookie)
        check("主人撤销 200", status == 200, str(status))
        status, _ = call(base, "GET", f"/api/public/quick/drop/{token}")
        check("撤销后 410", status == 410, str(status))

        # ---------------- B 站内快传 ----------------
        print("\n[B] 站内快传")
        node = upload_file(base, a_cookie, "发给B.txt", b"flash-content" * 100)
        status, data = call(
            base,
            "POST",
            "/api/quick/sends",
            a_cookie,
            {"nodeIds": [node["id"]], "toUsername": b_name, "message": "测试附言"},
        )
        check("发送 200", status == 200, str(status))
        send = data.get("send") or {}
        check("附言回传", send.get("message") == "测试附言", str(send.get("message")))

        status, listing = call(base, "GET", "/api/nodes", b_cookie)
        names = [item["name"] for item in listing.get("items") or []]
        check("B 根目录收到副本", "发给B.txt" in names, str(names))

        status, inbox = call(base, "GET", "/api/quick/inbox", b_cookie)
        check("B 收件箱 1 条", len(inbox.get("items") or []) == 1, str(len(inbox.get("items") or [])))
        check("未读数 1", inbox.get("unread") == 1, str(inbox.get("unread")))
        check("发件人信息正确", ((inbox.get("items") or [{}])[0].get("peer") or {}).get("username") == a_name, a_name)

        status, outbox = call(base, "GET", "/api/quick/outbox", a_cookie)
        check("A 发件箱 1 条", len(outbox.get("items") or []) == 1, str(len(outbox.get("items") or [])))

        status, read = call(base, "POST", "/api/quick/inbox/read", b_cookie, {})
        check("标记已读", status == 200 and read.get("unread") == 0, str(read))

        status, data = call(
            base,
            "POST",
            "/api/quick/sends",
            a_cookie,
            {"nodeIds": [node["id"]], "toUsername": a_name},
        )
        check("发给自己 400", status == 400, f"{status} {data.get('detail')}")

        status, data = call(
            base,
            "POST",
            "/api/quick/sends",
            a_cookie,
            {"nodeIds": [node["id"]], "toUsername": "不存在的用户"},
        )
        check("查无此人 404", status == 404, str(status))

        # ---------------- C 取件码 ----------------
        print("\n[C] 取件码")
        payload = b"pickup-body" * 200
        pickup_node = upload_file(base, a_cookie, "取件.txt", payload)
        status, data = call(
            base,
            "POST",
            "/api/quick/pickups",
            a_cookie,
            {"nodeIds": [pickup_node["id"]], "days": 3, "maxUses": 1},
        )
        pickup = data.get("pickup") or {}
        check("生成取件码 200", status == 200, str(status))
        code = pickup.get("code") or ""
        check("码是 6 位", len(code) == 6 and code.isalnum(), code)
        check("链接指向 /pickup", str(pickup.get("url", "")).startswith("/pickup?code="), str(pickup.get("url")))

        status, data = call(base, "GET", f"/api/public/quick/pickup/{code}")
        check("匿名查码 200", status == 200, str(status))
        items = (data.get("pickup") or {}).get("items") or []
        check("列出 1 个文件", len(items) == 1, str(len(items)))

        status, body = fetch_bytes(base, f"/api/public/quick/pickup/{code}/download?nodeId={pickup_node['id']}")
        check("匿名下载 200", status == 200, str(status))
        check("下载内容一致", body == payload, f"{len(body)}/{len(payload)}")

        status, _ = call(base, "GET", f"/api/public/quick/pickup/{code}")
        check("次数用完 410", status == 410, str(status))

        status, data = call(base, "POST", "/api/quick/pickups", a_cookie, {"nodeIds": [target_id], "days": 3})
        check("文件夹被拒 400", status == 400, f"{status} {data.get('detail')}")

        status, data = call(base, "POST", "/api/quick/pickups", a_cookie, {"nodeIds": [pickup_node["id"]], "days": 7})
        code2 = (data.get("pickup") or {}).get("code")
        status, _ = call(base, "DELETE", f"/api/quick/pickups/{(data.get('pickup') or {}).get('id')}", a_cookie)
        check("撤销取件码 200", status == 200, str(status))
        status, _ = call(base, "GET", f"/api/public/quick/pickup/{code2}")
        check("撤销后 410", status == 410, str(status))

        status, _ = call(base, "GET", "/api/public/quick/pickup/ZZZZZZ")
        check("不存在的码 404", status == 404, str(status))

        # ---------------- D 未登录 ----------------
        print("\n[D] 未登录")
        status, _ = call(base, "GET", "/api/quick/drops")
        check("未登录看列表 401", status == 401, str(status))
        status, _ = call(base, "POST", "/api/quick/pickups", None, {"nodeIds": [1]})
        check("未登录建码 401", status == 401, str(status))

        print(f"\n结果：{passed} 通过，{failed} 失败")
        return 0 if failed == 0 else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
