# -*- coding: utf-8 -*-
"""站内快传「接收方同意」全链路联调。

以前的 send 是直接复制进对方根目录；现在先放暂存区，接收方同意后才落进
「我的文件」（默认 我的文件/快传的文件），同意前只能预览、不能直接用。

  [A] 接收开关：关掉后发送 403，打开后 200
  [B] 待接收：对方根目录看不到、inbox 里 pending=1、能逐层浏览与下载预览、
      发送方自己反而不能按 id 摸到这份暂存副本
  [C] 同意：文件落进 我的文件/快传的文件（内容一致），重复同意 400，发件箱变成 accepted
  [D] 拒绝：暂存文件被回收、接收方再浏览 410、发送方 outbox 出现「被拒绝」提示并可确认
  [E] 自动同意：开了开关直接落盘，不用人工点
  [F] 收件目录：自定义目录生效、恢复默认后 isDefault=true
  [G] 越权：别人不能同意 / 浏览别人的快传

    ./.venv/Scripts/python.exe scripts/quick_send_consent_smoke.py
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


def fetch_bytes(base: str, path: str, cookie: str | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(f"{base}{path}")
    if cookie:
        req.add_header("Cookie", f"cloudnetp_session={cookie}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
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


def list_names(base: str, cookie: str, parent_id: int | None = None) -> list[str]:
    query = f"?parentId={parent_id}" if parent_id else ""
    status, data = call(base, "GET", f"/api/nodes{query}", cookie)
    return [item["name"] for item in (data.get("items") or [])]


def find_folder(base: str, cookie: str, name: str, parent_id: int | None = None) -> int | None:
    query = f"?parentId={parent_id}" if parent_id else ""
    status, data = call(base, "GET", f"/api/nodes{query}", cookie)
    for item in data.get("items") or []:
        if item["type"] == "folder" and item["name"] == name:
            return item["id"]
    return None


def main() -> int:
    from app.security import create_token, hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-send-")).resolve()
    db_copy = tmp / "smoke.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    for stmt in (
        "delete from quick_transfers",
        "delete from share_reports",
        "delete from blocked_blobs",
    ):
        try:
            con.execute(stmt)
        except sqlite3.OperationalError:
            pass
    users = con.execute(
        "select id, username, token_version from users order by role desc, id limit 3"
    ).fetchall()
    while len(users) < 3:
        name = f"quicksend{len(users)}"
        con.execute(
            "insert into users (username, password_hash, display_name, quota_bytes, used_bytes, role,"
            " token_version, created_at) values (?,?,?,?,?,?,?,datetime('now'))",
            (name, "x", name, 10 * 1024 ** 3, 0, 0, 0),
        )
        con.commit()
        users = con.execute(
            "select id, username, token_version from users order by role desc, id limit 3"
        ).fetchall()
    hashed = hash_password(PASSWORD)
    for row in users:
        con.execute("update users set password_hash=? where id=?", (hashed, row[0]))
    ids = [row[0] for row in users]
    try:
        # 规范同意状态与接收偏好都是账号级的，跑第二遍要回到初始值
        con.execute(
            "update user_settings set quick_terms_at=NULL, quick_receive_enabled=1,"
            " quick_auto_accept=0, quick_inbox_folder=NULL where user_id in (?,?,?)",
            ids,
        )
    except sqlite3.OperationalError:
        pass
    con.commit()
    con.close()

    a_id, a_name, a_ver = users[0]
    b_id, b_name, b_ver = users[1]
    c_id, c_name, c_ver = users[2]
    a_cookie = create_token(a_id, a_ver)
    b_cookie = create_token(b_id, b_ver)
    c_cookie = create_token(c_id, c_ver)

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

        for cookie in (a_cookie, b_cookie):
            call(base, "POST", "/api/quick/terms", cookie, {"accepted": True})

        # ---------------- A 接收开关 ----------------
        print("\n[A] 接收开关")
        status, data = call(base, "GET", "/api/quick/receive", b_cookie)
        pref = data.get("receive") or {}
        check("默认允许接收", pref.get("enabled") is True, str(pref))
        check("默认不自动同意", pref.get("autoAccept") is False, str(pref))
        check("默认目录是 快传的文件", (pref.get("path") or {}).get("isDefault") is True, str(pref.get("path")))
        status, data = call(
            base, "PATCH", "/api/me/settings", b_cookie, {"quickReceiveEnabled": False}
        )
        check("关闭接收开关", status == 200 and data["settings"]["quickReceiveEnabled"] is False, str(status))

        payload_a = b"consent-body" * 300
        node = upload_file(base, a_cookie, "发B文件.txt", payload_a)
        status, data = call(
            base, "POST", "/api/quick/sends", a_cookie,
            {"nodeIds": [node["id"]], "toUsername": b_name, "message": "同意测试"},
        )
        check("对方关接收时 403", status == 403, str(status))
        check("提示文案点明关闭接收", "接收" in str(data.get("detail")), str(data.get("detail")))

        status, _ = call(
            base, "PATCH", "/api/me/settings", b_cookie, {"quickReceiveEnabled": True}
        )
        check("重新打开接收", status == 200, str(status))

        # ---------------- B 待接收 ----------------
        print("\n[B] 待接收")
        status, data = call(
            base, "POST", "/api/quick/sends", a_cookie,
            {"nodeIds": [node["id"]], "toUsername": b_name, "message": "同意测试"},
        )
        check("发送 200", status == 200, str(status))
        send = data.get("send") or {}
        send_id = send.get("id")
        check("状态是待接收", send.get("sendStatus") == "pending", str(send.get("sendStatus")))

        names = list_names(base, b_cookie)
        check("B 根目录还没出现文件", "发B文件.txt" not in names, str(names))
        inbox_folder_id = find_folder(base, b_cookie, "快传的文件")
        check("暂存阶段不建收件目录", inbox_folder_id is None, str(inbox_folder_id))

        status, inbox = call(base, "GET", "/api/quick/inbox", b_cookie)
        items = inbox.get("items") or []
        check("收件箱 1 条", len(items) == 1, str(len(items)))
        check("角标 pending=1", inbox.get("pending") == 1, str(inbox.get("pending")))
        check("待接收条目带附言", (items[0] if items else {}).get("message") == "同意测试", str(items))

        # 同意前可以先看内容：列目录 + 下载预览
        status, listing = call(base, "GET", f"/api/quick/sends/{send_id}/items", b_cookie)
        check("浏览条目 200", status == 200, str(status))
        browse = listing.get("items") or []
        check("列出 1 个待接收文件", len(browse) == 1 and browse[0]["name"] == "发B文件.txt", str(browse))
        staged_id = browse[0]["id"] if browse else 0
        status, blob = fetch_bytes(base, f"/api/nodes/{staged_id}/download", b_cookie)
        check("接收方可以预览/下载暂存文件", status == 200 and blob == payload_a, f"{status} {len(blob)}")
        status, _ = fetch_bytes(base, f"/api/nodes/{staged_id}/download", a_cookie)
        check("发送方不能按 id 摸这份副本 404", status == 404, str(status))
        status, _ = fetch_bytes(base, f"/api/nodes/{staged_id}/download", c_cookie)
        check("第三方 404", status == 404, str(status))

        # ---------------- C 同意 ----------------
        print("\n[C] 同意存入")
        status, data = call(base, "POST", f"/api/quick/sends/{send_id}/accept", b_cookie)
        check("同意 200", status == 200, str(status))
        check("落盘 1 项", data.get("saved") == 1, str(data.get("saved")))
        inbox_folder_id = find_folder(base, b_cookie, "快传的文件")
        check("默认建出 快传的文件 目录", inbox_folder_id is not None, str(inbox_folder_id))
        names = list_names(base, b_cookie, inbox_folder_id)
        check("文件在收件目录里", "发B文件.txt" in names, str(names))
        names_root = list_names(base, b_cookie)
        check("根目录还是干净的", "发B文件.txt" not in names_root, str(names_root))
        saved_id = (data.get("nodes") or [{}])[0].get("id")
        status, blob = fetch_bytes(base, f"/api/nodes/{saved_id}/download", b_cookie)
        check("内容一致", status == 200 and blob == payload_a, f"{status} {len(blob)}")

        status, again = call(base, "POST", f"/api/quick/sends/{send_id}/accept", b_cookie)
        check("重复同意 400", status == 400, f"{status} {again.get('detail')}")

        status, outbox = call(base, "GET", "/api/quick/outbox", a_cookie)
        out_items = outbox.get("items") or []
        check("发件箱 1 条", len(out_items) == 1, str(len(out_items)))
        check("发件箱状态 accepted", (out_items[0] if out_items else {}).get("sendStatus") == "accepted", str(out_items))
        check("已存入不提示", outbox.get("unseenResults") == 0, str(outbox.get("unseenResults")))

        # ---------------- D 拒绝 ----------------
        print("\n[D] 拒绝")
        node2 = upload_file(base, a_cookie, "会被拒.txt", b"rejected-body" * 100)
        status, data = call(
            base, "POST", "/api/quick/sends", a_cookie,
            {"nodeIds": [node2["id"]], "toUsername": b_name},
        )
        reject_id = (data.get("send") or {}).get("id")
        status, listing = call(base, "GET", f"/api/quick/sends/{reject_id}/items", b_cookie)
        staged2 = ((listing.get("items") or [{}])[0] or {}).get("id")
        status, data = call(base, "POST", f"/api/quick/sends/{reject_id}/reject", b_cookie)
        check("拒绝 200", status == 200, str(status))
        check("状态变成 rejected", (data.get("send") or {}).get("sendStatus") == "rejected", str(data.get("send")))
        status, _ = call(base, "GET", f"/api/quick/sends/{reject_id}/items", b_cookie)
        check("拒绝后再浏览 410", status == 410, str(status))
        status, _ = fetch_bytes(base, f"/api/nodes/{staged2}/download", b_cookie)
        check("暂存文件已被回收 404", status == 404, str(status))
        names = list_names(base, b_cookie, inbox_folder_id)
        check("拒绝不会落进收件目录", "会被拒.txt" not in names, str(names))

        status, outbox = call(base, "GET", "/api/quick/outbox", a_cookie)
        check("发送方看到待提示 1 条", outbox.get("unseenResults") == 1, str(outbox.get("unseenResults")))
        rejected = [i for i in (outbox.get("items") or []) if i.get("sendStatus") == "rejected"]
        check("被拒绝的那条在列表里", len(rejected) == 1, str(len(rejected)))
        status, ack = call(base, "POST", "/api/quick/outbox/ack", a_cookie)
        check("点确认后清零", status == 200 and ack.get("acked") == 1, str(ack))
        status, outbox = call(base, "GET", "/api/quick/outbox", a_cookie)
        check("不再提示", outbox.get("unseenResults") == 0, str(outbox.get("unseenResults")))

        # ---------------- E 自动同意 ----------------
        print("\n[E] 自动同意")
        call(base, "PATCH", "/api/me/settings", b_cookie, {"quickAutoAccept": True})
        node3 = upload_file(base, a_cookie, "自动收.txt", b"auto-body" * 50)
        status, data = call(
            base, "POST", "/api/quick/sends", a_cookie,
            {"nodeIds": [node3["id"]], "toUsername": b_name},
        )
        check("发送 200", status == 200, str(status))
        check("直接变 accepted", (data.get("send") or {}).get("sendStatus") == "accepted", str(data.get("send")))
        names = list_names(base, b_cookie, inbox_folder_id)
        check("自动落进收件目录", "自动收.txt" in names, str(names))
        call(base, "PATCH", "/api/me/settings", b_cookie, {"quickAutoAccept": False})

        # ---------------- F 自定义收件目录 ----------------
        print("\n[F] 收件目录")
        status, folder = call(
            base, "POST", "/api/nodes/folder", b_cookie, {"parentId": None, "name": "我的收件箱"}
        )
        custom_id = ((folder.get("node") or {}) or {}).get("id")
        check("建自定义目录", status == 201 and custom_id, str(status))
        status, data = call(
            base, "PUT", "/api/quick/inbox-folder", b_cookie, {"folderId": custom_id}
        )
        check("设置收件目录 200", status == 200, str(status))
        path = (data.get("path") or {})
        check("isDefault=false", path.get("isDefault") is False, str(path))
        check("路径最后一段是自定义目录", (path.get("path") or [{}])[-1].get("name") == "我的收件箱", str(path))

        node4 = upload_file(base, a_cookie, "进自定义目录.txt", b"custom-body" * 30)
        status, data = call(
            base, "POST", "/api/quick/sends", a_cookie,
            {"nodeIds": [node4["id"]], "toUsername": b_name},
        )
        sid4 = (data.get("send") or {}).get("id")
        call(base, "POST", f"/api/quick/sends/{sid4}/accept", b_cookie)
        names = list_names(base, b_cookie, custom_id)
        check("文件进了自定义目录", "进自定义目录.txt" in names, str(names))

        status, data = call(base, "PUT", "/api/quick/inbox-folder", b_cookie, {"folderId": None})
        check("恢复默认 200", status == 200 and (data.get("path") or {}).get("isDefault") is True, str(data))

        # ---------------- G 越权 ----------------
        print("\n[G] 越权")
        node5 = upload_file(base, a_cookie, "越权.txt", b"perm-body" * 20)
        status, data = call(
            base, "POST", "/api/quick/sends", a_cookie,
            {"nodeIds": [node5["id"]], "toUsername": b_name},
        )
        sid5 = (data.get("send") or {}).get("id")
        status, _ = call(base, "POST", f"/api/quick/sends/{sid5}/accept", c_cookie)
        check("第三方不能同意 404", status == 404, str(status))
        status, _ = call(base, "GET", f"/api/quick/sends/{sid5}/items", c_cookie)
        check("第三方不能浏览 404", status == 404, str(status))
        status, _ = call(base, "POST", f"/api/quick/sends/{sid5}/reject", a_cookie)
        check("发送方自己不能替对方拒绝 404", status == 404, str(status))

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
