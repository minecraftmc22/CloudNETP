# -*- coding: utf-8 -*-
"""历史过期记录冒烟：分享与快传的「历史过期」分组、逐条删除、一键清空。

覆盖：
  [A] 分享列表按 scope 分组（active / history / 默认全都要），带两边条数
  [B] 分享历史：逐条删除记录、一键清空，都不影响还剩效的那些
  [C] 快传：还能用的删 = 关闭，已失效的删 = 删记录
  [D] 取件码：删记录时回收自己独占的快传暂存文件，用户网盘里的文件不动
  [E] 一键清空 /api/quick/history

自带实例与数据库副本，不碰真实库：python scripts/history_purge_smoke.py
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
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
TMP = Path(tempfile.mkdtemp(prefix="cn_history_"))
DB_COPY = TMP / "probe.db"
STAMP = str(int(time.time()) % 100000)

ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {label} {extra}")
    else:
        fail += 1
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


snapshot_db(DB_COPY)
PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
os.environ.update(
    {
        "DATABASE_URL": f"sqlite:///{DB_COPY.as_posix()}",
        "STORAGE_DIR": str(TMP / "storage"),
        "HOST": "127.0.0.1",
        "PORT": str(PORT),
        "RELOAD": "0",
        "DB_PROBE_INTERVAL": "0",
        "DB_MIRROR_INTERVAL": "0",
        "DB_FALLBACK_ENABLED": "0",
    }
)
(TMP / "storage").mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ROOT))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from app import db_failover  # noqa: E402
from app.models import Node, User  # noqa: E402
from app.security import create_token  # noqa: E402


def http(method, path, body=None, headers=None):
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Origin", BASE)
    req.add_header(
        "Content-Type", "application/octet-stream" if isinstance(body, bytes) else "application/json"
    )
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw) if raw else {}
            except Exception:
                return resp.status, raw[:200]
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw[:300]


def sql(stmt: str, args: tuple = ()):
    con = sqlite3.connect(str(DB_COPY), timeout=15)
    try:
        cur = con.execute(stmt, args)
        rows = cur.fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def upload_quick(cookie, name: str, payload: bytes) -> dict:
    """走分片接口上传一个快传暂存文件（quick=true）。"""
    st, init = http(
        "POST",
        "/api/uploads/init",
        {"name": name, "size": len(payload), "quick": True, "sha256": None, "mime": "text/plain"},
        cookie,
    )
    if st != 200:
        raise SystemExit(f"init 失败 {st} {init}")
    if init.get("instant"):
        return init["node"]
    sid = init["sessionId"]
    st, res = http("PUT", f"/api/uploads/{sid}/0", payload, cookie)
    if st != 200:
        raise SystemExit(f"分片失败 {st} {res}")
    st, done = http("POST", f"/api/uploads/{sid}/complete", None, cookie)
    if st != 200:
        raise SystemExit(f"合并失败 {st} {done}")
    return done["node"]


def upload_plain(cookie, name: str, payload: bytes) -> dict:
    st, data = http("PUT", f"/api/nodes/upload?name={urllib.request.quote(name)}", payload, cookie)
    if st != 201:
        raise SystemExit(f"普通上传失败 {st} {data}")
    return data["node"]


def main():
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    admin = db.query(User).filter(User.role == 1).order_by(User.id).first()
    token = create_token(admin.id, admin.token_version)
    cookie = {"Cookie": f"cloudnetp_session={token}"}
    admin_id = admin.id
    db.close()

    # 库是副本：先把这位用户名下的分享与快传清空，好按绝对条数断言
    sql("DELETE FROM shares WHERE user_id = ?", (admin_id,))
    sql("DELETE FROM quick_transfers WHERE owner_id = ?", (admin_id,))
    sql("DELETE FROM quick_pickup_items")

    log_file = (TMP / "server.log").open("wb")
    server = subprocess.Popen(
        [str(VENV_PY), "main.py"],
        cwd=str(ROOT),
        env=os.environ.copy(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(120):
            if server.poll() is not None:
                raise SystemExit(
                    "测试实例没起来：" + (TMP / "server.log").read_text(errors="replace")[-800:]
                )
            try:
                with urllib.request.urlopen(f"{BASE}/api/meta", timeout=2) as resp:
                    if resp.status == 200:
                        break
            except Exception:
                pass
            time.sleep(0.5)

        payload = b"history-payload" * 40
        keep_file = upload_plain(cookie, f"历史保留-{STAMP}.txt", payload * 3)

        # ---------------- A 分享分组 ----------------
        print("== A 分享列表分组 ==")
        ids = []
        for idx in range(3):
            st, data = http(
                "POST", "/api/shares", {"nodeIds": [keep_file["id"]], "expiresDays": 7}, cookie
            )
            ids.append((data.get("share") or {}).get("id"))
        check("建了 3 条有效分享", all(ids) and st == 200, str(ids))

        # 第 4 条：取消掉 -> 历史
        st, data = http("POST", "/api/shares", {"nodeIds": [keep_file["id"]], "expiresDays": 7}, cookie)
        revoked_id = (data.get("share") or {}).get("id")
        st, data = http("DELETE", f"/api/shares/{revoked_id}", None, cookie)
        check("取消一条进历史", st == 200 and data.get("revoked") is True, str(data))

        # 第 5 条：把到期时间改成昨天 -> 历史（状态是算出来的）
        st, data = http("POST", "/api/shares", {"nodeIds": [keep_file["id"]], "expiresDays": 7}, cookie)
        expired_id = (data.get("share") or {}).get("id")
        sql(
            "UPDATE shares SET expires_at = ? WHERE id = ?",
            ((datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.000000"), expired_id),
        )

        st, active = http("GET", "/api/shares?page=1&pageSize=50&scope=active", None, cookie)
        items = active.get("items") or []
        check("active 只剩 3 条", len(items) == 3, str(len(items)))
        check("active 全是有效", all(i["status"] == "active" for i in items))
        check("counts.active=3", (active.get("counts") or {}).get("active") == 3, str(active.get("counts")))

        st, hist = http("GET", "/api/shares?page=1&pageSize=50&scope=history", None, cookie)
        hitems = hist.get("items") or []
        check("history 2 条", len(hitems) == 2, str(len(hitems)))
        check("history 里没有有效分享", all(i["status"] != "active" for i in hitems))
        check(
            "过期与取消都在",
            {i["status"] for i in hitems} == {"expired", "revoked"},
            str([i["status"] for i in hitems]),
        )
        check("counts.history=2", (hist.get("counts") or {}).get("history") == 2, str(hist.get("counts")))

        st, allp = http("GET", "/api/shares?page=1&pageSize=50", None, cookie)
        check("不带 scope 仍是全部 5 条", len(allp.get("items") or []) == 5, str(len(allp.get("items"))))

        st, paged = http("GET", "/api/shares?page=2&pageSize=2&scope=active", None, cookie)
        check("scope 下分页仍然正确", paged.get("pages") == 2 and len(paged.get("items") or []) == 1,
              f"{paged.get('pages')} / {len(paged.get('items') or [])}")

        # ---------------- B 分享历史删除 ----------------
        print("== B 分享历史：逐条删除 / 一键清空 ==")
        st, data = http("DELETE", f"/api/shares/{revoked_id}", None, cookie)
        check("删掉失效记录", st == 200 and data.get("deleted") is True, str(data))
        st, hist = http("GET", "/api/shares?scope=history&pageSize=50", None, cookie)
        check("历史只剩 1 条", len(hist.get("items") or []) == 1, str(len(hist.get("items") or [])))

        st, data = http("DELETE", "/api/shares/history", None, cookie)
        check("一键清空历史", st == 200 and data.get("deleted") == 1, str(data))
        st, hist = http("GET", "/api/shares?scope=history&pageSize=50", None, cookie)
        check("历史清空了", not (hist.get("items") or []), str(hist.get("items")))
        st, active = http("GET", "/api/shares?scope=active&pageSize=50", None, cookie)
        check("还剩效的分享没被牵连", len(active.get("items") or []) == 3, str(len(active.get("items") or [])))

        # 收尾：把 3 条有效分享删掉，免得留在副本里（副本最后会删，这里只验证接口还能用）
        for item in active.get("items") or []:
            http("DELETE", f"/api/shares/{item['id']}", None, cookie)

        # ---------------- C 快传：关闭 vs 删记录 ----------------
        print("== C 快传：关闭 / 删记录 ==")
        st, data = http("POST", "/api/quick/drops", {"minutes": 30}, cookie)
        drop_id = (data.get("drop") or {}).get("id")
        check("生成快传链接", st == 200 and bool(drop_id), str(st))

        st, data = http("DELETE", f"/api/quick/drops/{drop_id}", None, cookie)
        check("还能用的删 = 关闭", data.get("revoked") is True, str(data))
        st, data = http("GET", "/api/quick/drops?scope=history", None, cookie)
        check("关闭后进历史", len(data.get("items") or []) == 1, str(data.get("items")))
        check("counts 跟上", (data.get("counts") or {}).get("history") == 1, str(data.get("counts")))
        st, data = http("GET", "/api/quick/drops?scope=active", None, cookie)
        check("进行中空了", not (data.get("items") or []), str(data.get("items")))

        st, data = http("DELETE", f"/api/quick/drops/{drop_id}", None, cookie)
        check("失效的删 = 删记录", data.get("deleted") is True, str(data))
        st, data = http("GET", "/api/quick/drops", None, cookie)
        check("记录真的没了", not (data.get("items") or []), str(data.get("items")))

        st, data = http("GET", f"/api/quick/drops", None, cookie)
        check("删完的 id 再删 404", http("DELETE", f"/api/quick/drops/{drop_id}", None, cookie)[0] == 404)

        # 一键清空 drop 历史
        for _ in range(2):
            st, data = http("POST", "/api/quick/drops", {"minutes": 30}, cookie)
            did = (data.get("drop") or {}).get("id")
            http("DELETE", f"/api/quick/drops/{did}", None, cookie)
        st, data = http("DELETE", "/api/quick/history?kind=drop", None, cookie)
        check("清空 drop 历史 = 2 条", data.get("deleted") == 2, str(data))
        st, data = http("GET", "/api/quick/drops", None, cookie)
        check("drop 列表已空", not (data.get("items") or []), str(data.get("items")))

        # ---------------- D 取件码：删记录回收暂存文件 ----------------
        print("== D 取件码：回收自己的暂存文件，不动网盘文件 ==")
        staged = upload_quick(cookie, f"暂存-{STAMP}.txt", b"staged-file" * 60)
        check("快传暂存文件建好了", bool(staged.get("id")), str(staged.get("id")))
        staged_path = None
        rows = sql("SELECT quick_owned, blob_sha256 FROM nodes WHERE id = ?", (staged["id"],))
        check("节点标成 quick_owned", rows and rows[0][0] == 1, str(rows))

        st, data = http("POST", "/api/quick/pickups", {"nodeIds": [staged["id"]], "days": 7}, cookie)
        pickup_a = (data.get("pickup") or {}).get("id")
        check("建取件码（暂存文件）", st == 200 and bool(pickup_a), str(st))

        st, data = http("POST", "/api/quick/pickups", {"nodeIds": [keep_file["id"]], "days": 7}, cookie)
        pickup_b = (data.get("pickup") or {}).get("id")

        # 还能用的取件码：删一次 = 撤销，记录留在历史里
        st, data = http("POST", "/api/quick/pickups", {"nodeIds": [keep_file["id"]], "days": 7}, cookie)
        revoke_me = (data.get("pickup") or {}).get("id")
        st, data = http("DELETE", f"/api/quick/pickups/{revoke_me}", None, cookie)
        check("撤销取件码回 revoked", data.get("revoked") is True, str(data))
        st, hist = http("GET", "/api/quick/pickups?scope=history", None, cookie)
        check(
            "撤销后留在历史里（没被直接删掉）",
            any(i["id"] == revoke_me for i in hist.get("items") or []),
            str([(i["id"], i["status"]) for i in hist.get("items") or []]),
        )
        check("历史计数里算上了", (hist.get("counts") or {}).get("history") == 1, str(hist.get("counts")))
        st, _ = http("DELETE", f"/api/quick/pickups/{revoke_me}", None, cookie)
        check("建取件码（网盘文件）", st == 200 and bool(pickup_b), str(st))

        past = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.000000")
        sql("UPDATE quick_transfers SET expires_at = ? WHERE id IN (?, ?)", (past, pickup_a, pickup_b))
        st, data = http("GET", "/api/quick/pickups?scope=history", None, cookie)
        check("两个取件码都进历史", len(data.get("items") or []) == 2, str(len(data.get("items") or [])))
        check("counts.pickup 对齐", (data.get("counts") or {}).get("history") == 2, str(data.get("counts")))

        st, data = http("DELETE", f"/api/quick/pickups/{pickup_a}", None, cookie)
        check("删住暂存文件的记录", data.get("deleted") is True, str(data))
        rows = sql("SELECT COUNT(*) FROM nodes WHERE id = ?", (staged["id"],))
        check("暂存文件跟着回收了", rows[0][0] == 0, str(rows))
        rows = sql("SELECT COUNT(*) FROM quick_pickup_items WHERE quick_id = ?", (pickup_a,))
        check("取件项也清了", rows[0][0] == 0, str(rows))

        st, data = http("DELETE", f"/api/quick/pickups/{pickup_b}", None, cookie)
        check("删住网盘文件的记录", data.get("deleted") is True, str(data))
        st, listing = http("GET", "/api/nodes?sort=name&order=asc", None, cookie)
        names = [i["name"] for i in listing.get("items") or []]
        check("网盘里的文件还在", keep_file["name"] in names, str(names[:5]))

        # ---------------- E 一键清空取件码历史 ----------------
        print("== E 一键清空取件码历史 ==")
        st, data = http("POST", "/api/quick/pickups", {"nodeIds": [keep_file["id"]], "days": 7}, cookie)
        dead_id = (data.get("pickup") or {}).get("id")
        sql("UPDATE quick_transfers SET expires_at = ? WHERE id = ?", (past, dead_id))
        st, data = http("DELETE", "/api/quick/history?kind=pickup", None, cookie)
        check("清空 pickup 历史", data.get("deleted") == 1, str(data))
        st, data = http("GET", "/api/quick/pickups", None, cookie)
        check("pickup 列表已空", not (data.get("items") or []), str(data.get("items")))
        st, listing = http("GET", "/api/nodes?sort=name&order=asc", None, cookie)
        check("清空后网盘文件仍在", keep_file["name"] in [i["name"] for i in listing.get("items") or []])

        print("== F 服务端无异常 ==")
        log = (TMP / "server.log").read_text(encoding="utf-8", errors="replace")
        check("没有 Traceback", "Traceback" not in log, log[-400:] if "Traceback" in log else "")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()
        log_file.close()

    print(f"\n结果：{ok} 通过，{fail} 失败")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
