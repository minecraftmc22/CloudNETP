"""数据库容灾（0.3.1）smoke 测试。

A 部分（纯函数级）：两个 SQLite 库互相当主/备，验证 mirror_data / promote_data
的跨方言 upsert——覆盖、新增、冲突以备库为准、audit_logs 条数上限、主库多出的行不删。

B 部分（端到端）：起一个子进程实例，DATABASE_URL 指向不可达的 MySQL，
验证启动即降级 SQLite：meta.dbMode=fallback、注册/登录/建文件夹/上传/列表全通、
fallback.db 落在临时 STORAGE_DIR、管理后台显示降级状态。

用法： ./.venv/Scripts/python.exe scripts/db_failover_smoke.py
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
PORT = 8031
BASE = f"http://127.0.0.1:{PORT}"

passed = 0
failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def request(method: str, url: str, body: dict | None = None, cookie: str | None = None,
            raw: bytes | None = None, ctype: str | None = None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", ctype or "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), resp.headers
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload or b"{}"), exc.headers
        except json.JSONDecodeError:
            return exc.code, {"raw": payload.decode("utf-8", "replace")}, exc.headers


# ================================================================ A. 同步核心

def part_a() -> None:
    print("\n[A] 同步核心（双 SQLite 互当主备）")
    from sqlalchemy import create_engine, event, func, select
    from sqlalchemy.orm import Session

    from app import db_failover
    from app.db import Base
    from app.models import AuditLog, Blob, Node, Share, SystemSetting, User, UserSetting, utcnow

    tmp = Path(tempfile.mkdtemp(prefix="failover_a_"))
    eng_a = create_engine(f"sqlite:///{(tmp / 'a.db').as_posix()}", future=True)
    eng_b = create_engine(f"sqlite:///{(tmp / 'b.db').as_posix()}", future=True)
    for eng in (eng_a, eng_b):

        @event.listens_for(eng, "connect")
        def _fk(dbapi_conn, _):  # noqa: F811
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    Base.metadata.create_all(eng_a)
    Base.metadata.create_all(eng_b)

    with Session(eng_a) as sess:
        alice = User(username="alice", password_hash="x", quota_bytes=1, used_bytes=2)
        bob = User(username="bob", password_hash="y")
        sess.add(alice)
        sess.add(bob)
        sess.flush()
        sess.add(UserSetting(user_id=alice.id))
        blob = Blob(sha256="a" * 64, size=123, ref_count=1)
        sess.add(blob)
        sess.flush()
        sess.add(Node(user_id=alice.id, type="file", name="old.txt", blob_sha256=blob.sha256, size=123))
        sess.add(Node(user_id=alice.id, type="folder", name="docs"))
        sess.add(Share(user_id=alice.id, node_id=1, token="tok" + "x" * 40))
        for i in range(db_failover.MIRROR_AUDIT_LIMIT + 120):
            sess.add(AuditLog(user_id=alice.id, action=f"act{i}", detail=None, ip=None))
        sess.add(SystemSetting(k="site_name", v="测试站"))
        sess.commit()

    n = db_failover.mirror_data(eng_a, eng_b)
    check(f"镜像同步 {db_failover.MIRROR_AUDIT_LIMIT + 7} 行量级", n > 0, f"实际 {n}")

    def counts(eng):
        out = {}
        with eng.connect() as conn:
            for t in db_failover._SYNC_TABLES:
                md = Base.metadata.tables[t]
                out[t] = conn.scalar(select(func.count()).select_from(md))
        return out

    ca, cb = counts(eng_a), counts(eng_b)
    check("users 行数一致", ca["users"] == cb["users"] == 2, f"{ca['users']} vs {cb['users']}")
    check("nodes 行数一致", ca["nodes"] == cb["nodes"] == 2, f"{ca['nodes']} vs {cb['nodes']}")
    check("audit_logs 镜像限流", cb["audit_logs"] == db_failover.MIRROR_AUDIT_LIMIT, f"{cb['audit_logs']}")
    check("system_settings 同步", cb["system_settings"] == 1, f"{cb['system_settings']}")

    # 模拟降级期间在备库（SQLite 接管方）发生的变更：改名、新用户、新文件、计数变化
    with Session(eng_b) as sess, sess.begin():
        node = sess.scalar(select(Node).where(Node.name == "old.txt"))
        node.name = "renamed.txt"
        node.updated_at = utcnow()
        sess.add(User(username="carol", password_hash="z"))
        sess.flush()
        sess.add(Node(user_id=sess.scalar(select(User.id).where(User.username == "carol")),
                      type="file", name="c.txt"))
        share = sess.scalar(select(Share))
        share.download_count += 1
    # 主库同时改了同一个文件（冲突：降级期间的备库应当胜出）
    with Session(eng_a) as sess, sess.begin():
        node = sess.scalar(select(Node).where(Node.name == "old.txt"))
        node.name = "main-wins.txt"

    n = db_failover.promote_data(eng_b, eng_a)
    check("回迁同步行数 > 0", n > 0, f"实际 {n}")
    with eng_a.connect() as conn:
        name = conn.scalar(select(Node.name).where(Node.type == "file", Node.name.in_(["renamed.txt", "main-wins.txt"])))
        check("冲突以备库为准（改名保留）", name == "renamed.txt", f"{name}")
        check("备库新增用户已回迁", conn.scalar(select(func.count()).select_from(User).where(User.username == "carol")) == 1)
        check("carol 的文件已回迁", conn.scalar(select(func.count()).select_from(Node).where(Node.name == "c.txt")) == 1)
        dl = conn.scalar(select(Share.download_count))
        check("分享计数回迁", dl == 1, f"{dl}")
    # 主库独有的行（比备库新）不应被回迁删除
    with Session(eng_a) as sess, sess.begin():
        sess.add(User(username="dave", password_hash="w"))
    db_failover.promote_data(eng_b, eng_a)
    with eng_a.connect() as conn:
        check("回迁不删除主库独有行", conn.scalar(select(func.count()).select_from(User)) == 4)

    # 回切后镜像一次，备库与主库回到一致（audit 仍受限流）
    db_failover.mirror_data(eng_a, eng_b)
    ca, cb = counts(eng_a), counts(eng_b)
    check("回切后镜像：users 一致", ca["users"] == cb["users"])
    check("回切后镜像：nodes 一致", ca["nodes"] == cb["nodes"])
    check("回切后镜像：shares 一致", ca["shares"] == cb["shares"])

    # 来源不明的副本（从未镜像过主库）回迁时必须只补缺失，绝不能覆盖主库
    eng_c = create_engine(f"sqlite:///{(tmp / 'c.db').as_posix()}", future=True)
    Base.metadata.create_all(eng_c)
    with Session(eng_c) as sess, sess.begin():
        sess.add(User(id=1, username="imposter", password_hash="hacked"))
        sess.add(User(id=99, username="newbie", password_hash="ok"))
        sess.add(SystemSetting(k="__failover_mirror_at", v="fake"))
    db_failover.promote_data(eng_c, eng_a, insert_only=True)
    with eng_a.connect() as conn:
        check("insert_only 不覆盖已有行",
              conn.scalar(select(User.username).where(User.id == 1)) != "imposter",
              str(conn.scalar(select(User.username).where(User.id == 1))))
        check("insert_only 仍补缺失行",
              conn.scalar(select(func.count()).select_from(User).where(User.username == "newbie")) == 1)
        check("内部标记键 __ 前缀不外传",
              conn.scalar(select(func.count()).select_from(SystemSetting)
                          .where(SystemSetting.k == "__failover_mirror_at")) == 0)


# ================================================================ B. 端到端降级

def seed_fallback_user(storage_dir: Path) -> None:
    """直接在容灾 SQLite 里预置一个账号。

    注册接口从 0.3.18 起强制图形验证码，脚本没法识别 SVG 里的点阵码，
    所以这里把账号写进库（等价于「站点已经有账号」），后面走登录接口拿会话。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db import Base
    from app.models import User, UserSetting
    from app.security import hash_password

    db_path = storage_dir / "db" / "fallback.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as sess, sess.begin():
        user = User(
            username="fbprobe",
            password_hash=hash_password("FbProbe#2026"),
            role=1,
            display_name="fbprobe",
        )
        sess.add(user)
        sess.flush()
        sess.add(UserSetting(user_id=user.id))
    engine.dispose()


def part_b() -> None:
    print("\n[B] 端到端：主库不可达 → 启动即降级 SQLite")
    tmp = Path(tempfile.mkdtemp(prefix="failover_b_"))
    storage_dir = tmp / "storage"
    seed_fallback_user(storage_dir)
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": "mysql+pymysql://nobody:nope@127.0.0.1:1/nonexistent",
        "STORAGE_DIR": str(storage_dir),
        "PORT": str(PORT),
        "RELOAD": "false",
        "DB_PROBE_INTERVAL": "15",
        "DB_MIRROR_INTERVAL": "300",
    })
    log_path = tmp / "server.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "main.py"],
        cwd=str(ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        # 等服务起来（对不可达 MySQL 的连接失败很快：connection refused）
        up = False
        for _ in range(60):
            if proc.poll() is not None:
                break
            time.sleep(0.5)
            try:
                code, _, _ = request("GET", f"{BASE}/api/version")
                if code == 200:
                    up = True
                    break
            except Exception:
                continue
        check("服务在降级模式下启动", up, log_path.read_text(encoding="utf-8", errors="replace")[-2000:])

        code, meta, _ = request("GET", f"{BASE}/api/meta")
        check("meta 可用", code == 200)
        check("meta.dbMode = fallback", meta.get("dbMode") == "fallback", str(meta.get("dbMode")))

        # 注册接口强制图形验证码：不带码必须被拦（0.3.18+ 的行为，顺带回归一次）
        code, body, _ = request("POST", f"{BASE}/api/auth/register",
                                {"username": "fbprobe2", "password": "FbProbe#2026"})
        check("降级模式下注册仍要验证码",
              code == 400 and "验证码" in str(body.get("detail", "")), f"{code} {body}")

        # 登录预置账号 → 拿会话
        code, body, headers = request("POST", f"{BASE}/api/auth/login",
                                      {"username": "fbprobe", "password": "FbProbe#2026"})
        check("降级模式可登录", code == 200, f"{code} {body}")
        cookie = ""
        for v in headers.get_all("Set-Cookie") or []:
            if v.startswith("cloudnetp_session="):
                cookie = v.split(";")[0]
        check("拿到会话 Cookie", bool(cookie))

        code, me, _ = request("GET", f"{BASE}/api/me", cookie=cookie)
        check("会话有效（/api/me）", code == 200 and me.get("user", {}).get("username") == "fbprobe", f"{code}")

        code, folder, _ = request("POST", f"{BASE}/api/nodes/folder",
                                  {"name": "降级测试"}, cookie=cookie)
        check("降级模式可建文件夹", code in (200, 201), f"{code} {folder}")
        folder_id = folder.get("id") if isinstance(folder, dict) else None

        # 分片协议上传一个小文件（不传 Origin，模拟无来源头客户端）
        content = b"hello fallback db"
        sha = hashlib.sha256(content).hexdigest()
        code, sess, _ = request("POST", f"{BASE}/api/uploads/init",
                                {"name": "hello.txt", "size": len(content), "parentId": folder_id},
                                cookie=cookie)
        check("上传会话创建", code in (200, 201) and sess.get("sessionId"), f"{code} {sess}")
        sid = sess.get("sessionId", "")
        req = urllib.request.Request(f"{BASE}/api/uploads/{sid}/0", data=content, method="PUT")
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                put_code = resp.status
        except urllib.error.HTTPError as exc:
            put_code = exc.code
        check("分片上传", put_code == 200, f"{put_code}")
        code, done, _ = request("POST", f"{BASE}/api/uploads/{sid}/complete?sha256={sha}",
                                cookie=cookie)
        check("上传完成", code in (200, 201), f"{code} {done}")

        code, listing, _ = request("GET", f"{BASE}/api/nodes?parentId={folder_id}" if folder_id else f"{BASE}/api/nodes",
                                   cookie=cookie)
        names = [n.get("name") for n in (listing.get("items") or [])] if isinstance(listing, dict) else []
        check("列表能看到刚上传的文件", "hello.txt" in names, str(names))

        # 管理后台显示降级状态
        code, ov, _ = request("GET", f"{BASE}/api/admin/overview", cookie=cookie)
        check("admin overview 可用", code == 200)
        check("admin 显示 dbMode=fallback", ov.get("system", {}).get("dbMode") == "fallback", str(ov.get("system")))

        # fallback.db 落在临时 STORAGE_DIR
        fb = tmp / "storage" / "db" / "fallback.db"
        check("fallback.db 已生成", fb.is_file(), str(fb))

        log = log_path.read_text(encoding="utf-8", errors="replace")
        check("启动日志声明降级", "数据库降级" in log or "数据库模式 fallback" in log)
        check("首次镜像因主库不可用而跳过", "首次镜像失败" in log or "数据库降级" in log)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        log_file.close()
        print(f"\n  日志: {log_path}")


if __name__ == "__main__":
    part_a()
    part_b()
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)
