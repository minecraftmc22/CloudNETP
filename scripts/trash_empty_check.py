# -*- coding: utf-8 -*-
"""回收站「清空」的回归：不能 500，也不能留下幽灵节点。

覆盖两个曾经的真实故障：

1. **500**：``release_blob`` 只信 ``blobs.ref_count``，计数一旦漂移归零就去
   ``DELETE FROM blobs``；而 ``nodes.blob_sha256`` 的外键是 RESTRICT，MySQL 上直接
   1451 → 整个清空事务回滚、前端看到「请求失败 500」。SQLite 默认不校验外键，
   所以本地很容易漏掉，这里启动时打开 ``PRAGMA foreign_keys=ON`` 模拟 InnoDB。
2. **幽灵残留**：清空含嵌套文件夹的回收站时，深层文件行没被删掉（父行没了、子行还在），
   既清不掉又继续占配额。

    ./.venv/Scripts/python.exe scripts/trash_empty_check.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

RUNNER = r"""
import os, sys
sys.path.insert(0, os.environ["SMOKE_ROOT"])
from app import captcha
captcha.verify = lambda *a, **k: (True, "")
import app.main as m
from sqlalchemy import event
from app.db import engine

# SQLite 默认不校验外键，打开它才等价于 MySQL InnoDB（fk_node_blob 是 RESTRICT）
@event.listens_for(engine, "connect")
def _fk(dbapi_conn, rec):
    dbapi_conn.execute("PRAGMA foreign_keys=ON")

import uvicorn
uvicorn.run(m.app, host="127.0.0.1", port=int(os.environ["SMOKE_PORT"]), log_level="warning")
"""

FAILED: list[str] = []
PASSED = 0


def check(name: str, ok: bool, detail: object = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  OK   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}   {detail}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="cloudnetp-trash-"))
    db_path = work / "test.db"
    shutil.copy(ROOT / "dev.db", db_path)
    storage_dir = work / "storage"
    port = free_port()
    base = f"http://localhost:{port}"
    runner = work / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")

    env = {
        **os.environ,
        "SMOKE_ROOT": str(ROOT),
        "SMOKE_PORT": str(port),
        "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "STORAGE_DIR": str(storage_dir),
        "SECRET_KEY": "smoke-secret-key-trash-empty",
        "DB_FALLBACK_ENABLED": "false",
        "MAIL_ENABLED": "false",
    }
    log_path = work / "server.log"
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([str(VENV_PY), str(runner)], cwd=str(ROOT), env=env,
                            stdout=log_file, stderr=subprocess.STDOUT)

    jar = urllib.request.HTTPCookieProcessor()
    opener = urllib.request.build_opener(jar, urllib.request.ProxyHandler({}))

    def call(method: str, path: str, body=None, raw=None, ctype=None, binary=False):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(base + path, data=data, method=method)
        if ctype:
            req.add_header("Content-Type", ctype)
        elif body is not None:
            req.add_header("Content-Type", "application/json")
        req.add_header("Origin", base)
        try:
            with opener.open(req, timeout=30) as res:
                data = res.read()
                if binary:
                    return res.status, data
                txt = data.decode()
                return res.status, (json.loads(txt) if txt else None)
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode()[:400]

    def upload(name: str, parent_id: int | None, data: bytes):
        sha = hashlib.sha256(data).hexdigest()
        status, init = call("POST", "/api/uploads/init", {
            "name": name, "size": len(data), "sha256": sha, "mime": "text/plain",
            "parentId": parent_id,
        })
        if status != 200:
            print("  upload init failed", status, str(init)[:200])
            return None, sha
        if init.get("instant"):
            return (init.get("node") or {}).get("id"), sha
        sid = init["sessionId"]
        call("PUT", f"/api/uploads/{sid}/0", raw=data, ctype="application/octet-stream")
        status, done = call("POST", f"/api/uploads/{sid}/complete", {})
        return (done or {}).get("node", {}).get("id"), sha

    def node_rows() -> list[tuple]:
        con = sqlite3.connect(db_path)
        try:
            return con.execute("SELECT id, name, parent_id, deleted_at FROM nodes ORDER BY id").fetchall()
        finally:
            con.close()

    def blob_ref(sha: str):
        con = sqlite3.connect(db_path)
        try:
            row = con.execute("SELECT ref_count FROM blobs WHERE sha256=?", (sha,)).fetchone()
        finally:
            con.close()
        return row[0] if row else None

    try:
        if not wait_http(base, proc):
            print(log_path.read_text(encoding="utf-8")[-2000:])
            return 1
        print(f"instance: {base}")

        stamp = str(int(time.time()))[-6:]
        status, res = call("POST", "/api/auth/register", {
            "username": f"te{stamp}", "password": "abc12345",
            "captchaId": "", "captcha": "0000", "agree": True,
        })
        check("注册成功", status == 201, status)

        # ---------- 场景 1：嵌套文件夹（深层文件必须一起删干净） ----------
        print("\n[1] 嵌套文件夹 A / B / deep.txt")
        _, a = call("POST", "/api/nodes/folder", {"parentId": None, "name": "A"})
        a_id = (a or {}).get("node", {}).get("id")
        _, b = call("POST", "/api/nodes/folder", {"parentId": a_id, "name": "B"})
        b_id = (b or {}).get("node", {}).get("id")
        deep_id, deep_sha = upload("deep.txt", b_id, b"deep-file-content")
        shallow_id, shallow_sha = upload("shallow.txt", a_id, b"shallow-file-content")
        check("建好两层文件夹与文件", all([a_id, b_id, deep_id, shallow_id]), (a_id, b_id, deep_id, shallow_id))

        call("DELETE", f"/api/nodes/{a_id}")
        _, trash = call("GET", "/api/trash")
        names = [i.get("name") for i in (trash.get("items") or [])]
        check("回收站只显示顶层条目 A", names == ["A"], names)

        status, body = call("POST", "/api/trash/empty", {})
        check("清空返回 200（不是 500）", status == 200, f"{status} {body}")
        check("计数为 4（A / B / 两个文件）", isinstance(body, dict) and body.get("removed") == 4, body)

        left = node_rows()
        names_left = [r[1] for r in left]
        check("深层文件 deep.txt 已删掉（无幽灵）", "deep.txt" not in names_left, names_left)
        check("A/B/shallow.txt 都已删掉", not ({"A", "B", "shallow.txt"} & set(names_left)), names_left)
        check("回收站空了", not [r for r in left if r[3]], left)
        check("两个 blob 记录都释放", blob_ref(deep_sha) is None and blob_ref(shallow_sha) is None,
              (blob_ref(deep_sha), blob_ref(shallow_sha)))

        # ---------- 场景 1b：还原嵌套文件夹（深层文件要一起回来） ----------
        print("\n[1b] 还原 A 里的深层文件")
        _, c = call("POST", "/api/nodes/folder", {"parentId": None, "name": "C"})
        c_id = (c or {}).get("node", {}).get("id")
        _, d = call("POST", "/api/nodes/folder", {"parentId": c_id, "name": "D"})
        d_id = (d or {}).get("node", {}).get("id")
        inner_id, _ = upload("inner.txt", d_id, b"inner-content")
        call("DELETE", f"/api/nodes/{c_id}")
        status, _ = call("GET", "/api/trash")
        status, restored = call("POST", f"/api/nodes/{c_id}/restore", {})
        check("还原成功", status == 200, f"{status} {restored}")

        _, root_list = call("GET", "/api/nodes")
        root_names = [i.get("name") for i in (root_list.get("items") or [])]
        check("C 回到根目录", "C" in root_names, root_names)
        _, d_list = call("GET", f"/api/nodes?parentId={d_id}")
        d_names = [i.get("name") for i in (d_list.get("items") or [])]
        check("深层的 inner.txt 也回来了（不在回收站）", d_names == ["inner.txt"], d_names)
        status, raw = call("GET", f"/api/nodes/{inner_id}/download", binary=True)
        check("还原后的文件能下载", status == 200 and raw == b"inner-content", status)

        call("DELETE", f"/api/nodes/{c_id}")
        status, _ = call("POST", "/api/trash/empty", {})
        check("再清空一次仍是 200", status == 200, status)

        # ---------- 场景 1c：先单独删文件，再删父文件夹 ----------
        print("\n[1c] 先删 F/G/a.txt，再删整个 F")
        _, f = call("POST", "/api/nodes/folder", {"parentId": None, "name": "F"})
        f_id = (f or {}).get("node", {}).get("id")
        _, g = call("POST", "/api/nodes/folder", {"parentId": f_id, "name": "G"})
        g_id = (g or {}).get("node", {}).get("id")
        a_id, _ = upload("a.txt", g_id, b"aaa-content")
        b_id, _ = upload("b.txt", g_id, b"bbb-content")

        call("DELETE", f"/api/nodes/{a_id}")
        _, trash = call("GET", "/api/trash")
        names = [i.get("name") for i in (trash.get("items") or [])]
        check("单独删的文件自成一條", names == ["a.txt"], names)

        call("DELETE", f"/api/nodes/{f_id}")
        _, trash = call("GET", "/api/trash")
        names = [i.get("name") for i in (trash.get("items") or [])]
        check("删父文件夹后仍显示两条（没被吞并）", sorted(names) == ["F", "a.txt"], names)

        status, _ = call("POST", f"/api/nodes/{f_id}/restore", {})
        check("还原 F 成功", status == 200, status)
        _, f_list = call("GET", f"/api/nodes?parentId={f_id}")
        f_names = [i.get("name") for i in (f_list.get("items") or [])]
        check("F 里只有 G（a.txt 不在，它是独立条目）", f_names == ["G"], f_names)
        _, g_list = call("GET", f"/api/nodes?parentId={g_id}")
        g_names = [i.get("name") for i in (g_list.get("items") or [])]
        check("深层 G 里的 b.txt 一起回来了", g_names == ["b.txt"], g_names)

        _, trash = call("GET", "/api/trash")
        names = [i.get("name") for i in (trash.get("items") or [])]
        check("回收站只剩 a.txt", names == ["a.txt"], names)

        status, _ = call("POST", f"/api/nodes/{a_id}/restore", {})
        check("单独还原 a.txt 成功", status == 200, status)
        _, g_list = call("GET", f"/api/nodes?parentId={g_id}")
        g_names = [i.get("name") for i in (g_list.get("items") or [])]
        check("a.txt 回到原目录 G", sorted(g_names) == ["a.txt", "b.txt"], g_names)

        for nid in (a_id, b_id):
            call("DELETE", f"/api/nodes/{nid}")
        call("DELETE", f"/api/nodes/{f_id}")
        status, _ = call("POST", "/api/trash/empty", {})
        check("这组清空也返回 200", status == 200, status)

        # ---------- 场景 1d：先删子文件夹、再删父文件夹 ----------
        print("\n[1d] 先删 H/I（含 c.txt），再删整个 H")
        _, h = call("POST", "/api/nodes/folder", {"parentId": None, "name": "H"})
        h_id = (h or {}).get("node", {}).get("id")
        _, i_ = call("POST", "/api/nodes/folder", {"parentId": h_id, "name": "I"})
        i_id = (i_ or {}).get("node", {}).get("id")
        c_id, _ = upload("c.txt", i_id, b"ccc-content")

        call("DELETE", f"/api/nodes/{i_id}")
        call("DELETE", f"/api/nodes/{h_id}")
        _, trash = call("GET", "/api/trash")
        names = [i.get("name") for i in (trash.get("items") or [])]
        check("两条独立条目（H 与 I）", sorted(names) == ["H", "I"], names)

        call("POST", f"/api/nodes/{h_id}/restore", {})
        _, h_list = call("GET", f"/api/nodes?parentId={h_id}")
        h_names = [i.get("name") for i in (h_list.get("items") or [])]
        check("H 还原后是空的（I 独立）", h_names == [], h_names)
        _, trash = call("GET", "/api/trash")
        names = [i.get("name") for i in (trash.get("items") or [])]
        check("回收站只剩 I", names == ["I"], names)

        call("POST", f"/api/nodes/{i_id}/restore", {})
        _, i_list = call("GET", f"/api/nodes?parentId={i_id}")
        i_names = [i.get("name") for i in (i_list.get("items") or [])]
        check("I 还原后 c.txt 回来了", i_names == ["c.txt"], i_names)

        call("DELETE", f"/api/nodes/{h_id}")
        status, _ = call("POST", "/api/trash/empty", {})
        check("再清空仍是 200", status == 200, status)

        # ---------- 场景 2：引用计数漂移（曾经必 500） ----------
        print("\n[2] 同内容两份 + ref_count 人为调低")
        dup = b"duplicate-payload"
        keep_id, dup_sha = upload("keep.txt", None, dup)
        drop_id, _ = upload("drop.txt", None, dup)
        check("两份同内容节点建好", bool(keep_id and drop_id and keep_id != drop_id), (keep_id, drop_id))
        check("秒传后 ref_count = 2", blob_ref(dup_sha) == 2, blob_ref(dup_sha))

        con = sqlite3.connect(db_path)
        con.execute("UPDATE blobs SET ref_count=1 WHERE sha256=?", (dup_sha,))
        con.commit()
        con.close()

        call("DELETE", f"/api/nodes/{drop_id}")
        status, body = call("POST", "/api/trash/empty", {})
        check("计数漂移下清空仍是 200", status == 200, f"{status} {body}")
        check("removed = 1", isinstance(body, dict) and body.get("removed") == 1, body)

        left = node_rows()
        names_left = [r[1] for r in left]
        check("目录里的 keep.txt 没被牵连", "keep.txt" in names_left, names_left)
        check("drop.txt 已删掉", "drop.txt" not in names_left, names_left)
        check("blob 记录被兜回 1", blob_ref(dup_sha) == 1, blob_ref(dup_sha))
        blob_file = storage_dir / "blobs" / dup_sha[:2] / dup_sha[2:4] / dup_sha
        check("blob 物理文件仍在（没被误删）", blob_file.is_file(), blob_file)

        status, raw = call("GET", f"/api/nodes/{keep_id}/download", binary=True)
        check("keep.txt 仍能下载", status == 200 and raw == dup, f"{status} {str(raw)[:60]}")

        # ---------- 场景 3：空回收站 ----------
        print("\n[3] 空回收站再点一次清空")
        status, body = call("POST", "/api/trash/empty", {})
        check("空回收站清空返回 200 + removed 0", status == 200 and body.get("removed") == 0, f"{status} {body}")

        # ---------- 场景 4：配额 ----------
        print("\n[4] 配额重算")
        _, me = call("GET", "/api/me")
        used = (me or {}).get("user", {}).get("usedBytes")
        expect = len(dup)  # 只剩 keep.txt 这份测试内容（dev.db 里原有账号的文件不在这里）
        check("清空后已用空间只剩 keep.txt", used is not None and used >= expect, f"{used} vs {expect}")

        print("\n" + ("全部通过 %d 项" % PASSED if not FAILED else "失败 %d 项: %s" % (len(FAILED), FAILED)))
        return 0 if not FAILED else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        if FAILED:
            print("--- server log ---")
            print(log_path.read_text(encoding="utf-8")[-2500:])
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
