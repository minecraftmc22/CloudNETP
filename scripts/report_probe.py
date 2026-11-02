# -*- coding: utf-8 -*-
"""分享举报 / 转存 / 哈希黑名单 / 管理员审核 端到端探针（0.3.9）。

覆盖：匿名举报 + 限流 / 管理员列表（哈希 + 首次文件名）/ 通过拉黑 /
公开访问被拦 / 重复创建分享被拦 / 转存成功 + 黑名单后转存被拦 /
文件本体不受影响（物主可下载）/ 驳回不影响分享 / 解除拉黑恢复。

用法：python scripts/report_probe.py [端口]
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8020
BASE = f"http://127.0.0.1:{PORT}"

failures: list[str] = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  | {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def http(method, path, body=None, cookie="", raw=None, ctype=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Origin", BASE)
    req.add_header("Content-Type", ctype or "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            payload = resp.read()
            return resp.status, (json.loads(payload) if payload[:1] in (b"{", b"[") else payload), resp.headers
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload), exc.headers
        except Exception:
            return exc.code, payload, exc.headers


def cookie_of(headers):
    for entry in headers.get_all("Set-Cookie") or []:
        if entry.startswith("cloudnetp_session="):
            return entry.split(";")[0]
    return ""


def make_account(username, password):
    s, _, headers = http("POST", "/api/auth/register", {"username": username, "password": password})
    if s not in (200, 201):
        s, _, headers = http("POST", "/api/auth/login", {"username": username, "password": password})
    assert s in (200, 201), f"账号准备失败 {username} {s}"
    return cookie_of(headers)


def dav_put(username, password, name, data: bytes):
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = urllib.request.Request(f"{BASE}/dav/{urllib.parse.quote(name)}", data=data, method="PUT")
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def make_admin(username, password):
    cookie = make_account(username, password)
    # 与运行中服务同款容灾初始化，保证拿到同一套库（主库不通时用 SQLite 副本）
    from app import db_failover
    from app.models import User

    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    row = db.query(User).filter_by(username=username).first()
    row.role = 1
    db.commit()
    db.close()
    return cookie


def main():
    # ---------- 准备 ----------
    sharer_cookie = make_account("rptshare", "RptShare#2026")
    receiver_cookie = make_account("rptrecv", "RptRecv#2026")
    admin_cookie = make_admin("rptadmin", "RptAdmin#2026")

    payload_a = b"report-probe target file A " + b"X" * 512
    payload_b = b"report-probe reject file B " + b"Y" * 512
    dav_put("rptshare", "RptShare#2026", "Report Target File.mp3", payload_a)
    dav_put("rptshare", "RptShare#2026", "Reject Me File.txt", payload_b)

    _, listing, _ = http("GET", "/api/nodes", cookie=sharer_cookie)
    items = {n["name"]: n for n in listing["items"]}
    node_a = items["Report Target File.mp3"]
    node_b = items["Reject Me File.txt"]

    s, data, _ = http("POST", "/api/shares", {"nodeId": node_a["id"]}, cookie=sharer_cookie)
    check("创建分享 A", s == 200, str(data)[:120])
    token_a = data["share"]["token"]
    # 同一文件的第二个分享（不被举报），用于验证哈希拉黑对其它链接的拦截
    s, data, _ = http("POST", "/api/shares", {"nodeId": node_a["id"]}, cookie=sharer_cookie)
    token_a2 = data["share"]["token"]
    check("创建同哈希分享 A2", s == 200, str(data)[:120])
    s, data, _ = http("POST", "/api/shares", {"nodeId": node_b["id"]}, cookie=sharer_cookie)
    check("创建分享 B", s == 200, str(data)[:120])
    token_b = data["share"]["token"]

    # ---------- 举报 ----------
    s, data, _ = http("POST", f"/api/public/shares/{token_a}/report", {"reason": "违规内容测试"})
    check("匿名举报 A", s == 200 and data.get("reported"), f"{s} {str(data)[:80]}")
    s, data, _ = http("POST", f"/api/public/shares/{token_a}/report", {"reason": "重复举报"})
    check("同 IP 重复举报被限流", s == 429, str(s))
    s, data, _ = http("POST", f"/api/public/shares/{token_b}/report", {"reason": "误报测试"})
    check("举报 B", s == 200, str(s))

    # ---------- 管理员列表 ----------
    s, data, _ = http("GET", "/api/admin/reports?status=pending", cookie=admin_cookie)
    check("管理员列表 pending", s == 200 and data["total"] >= 2, f"{s} total={data.get('total')}")
    rep_a = next((r for r in data["items"] if r["firstName"] == "Report Target File.mp3"), None)
    rep_b = next((r for r in data["items"] if r["firstName"] == "Reject Me File.txt"), None)
    check("列表含哈希与首次文件名", bool(rep_a and rep_a["blobSha256"] and len(rep_a["blobSha256"]) == 64),
          str(rep_a and rep_a["blobSha256"])[:80])
    check("列表含理由与状态", bool(rep_a and rep_a["reason"] == "违规内容测试" and rep_a["status"] == "pending"))

    sha_a = rep_a["blobSha256"]

    # ---------- 转存（拉黑前，应成功） ----------
    s, data, _ = http("POST", f"/api/public/shares/{token_a}/save", {}, cookie=receiver_cookie)
    check("转存 A 到接收者网盘", s == 200 and data.get("saved"), f"{s} {str(data)[:100]}")
    _, listing, _ = http("GET", "/api/nodes", cookie=receiver_cookie)
    check("接收者网盘出现该文件", any(n["name"].startswith("Report Target File") for n in listing["items"]))

    # ---------- 审核通过 → 拉黑 ----------
    s, data, _ = http("POST", f"/api/admin/reports/{rep_a['id']}/approve", {}, cookie=admin_cookie)
    check("审核通过 A", s == 200 and data.get("blocked"), f"{s} {str(data)[:100]}")

    s, data, _ = http("GET", f"/api/public/shares/{token_a}")
    check("被举报分享链接已失效(410)", s == 410, f"{s} {str(data)[:80]}")
    s, data, _ = http("GET", f"/api/public/shares/{token_a2}")
    check("同哈希其它分享被拦(403)", s == 403, f"{s} {str(data)[:80]}")
    s, data, _ = http("GET", f"/api/public/shares/{token_a2}/download")
    check("同哈希其它分享下载被拦", s == 403, str(s))
    s, data, _ = http("POST", f"/api/public/shares/{token_a2}/save", {}, cookie=receiver_cookie)
    check("同哈希其它分享转存被拦", s == 403, f"{s} {str(data)[:80]}")

    # 物主自己仍可下载（文件不删）
    s, body, _ = http("GET", f"/api/nodes/{node_a['id']}/download", cookie=sharer_cookie)
    check("物主下载不受影响", s == 200 and body == payload_a, f"{s} len={len(body) if isinstance(body, bytes) else '?'}")

    # 同哈希不能再被分享
    s, data, _ = http("POST", "/api/shares", {"nodeId": node_a["id"]}, cookie=sharer_cookie)
    check("拉黑后创建分享被拦", s == 403, f"{s} {str(data)[:80]}")

    # ---------- 驳回 B：分享不受影响 ----------
    s, data, _ = http("POST", f"/api/admin/reports/{rep_b['id']}/reject", {}, cookie=admin_cookie)
    check("驳回 B", s == 200, str(s))
    s, data, _ = http("GET", f"/api/public/shares/{token_b}")
    check("驳回后分享 B 仍可访问", s == 200 and data["share"]["name"] == "Reject Me File.txt", str(s))

    # ---------- 解除拉黑 ----------
    s, data, _ = http("POST", f"/api/admin/blacklist/{sha_a}/unblock", {}, cookie=admin_cookie)
    check("解除拉黑", s == 200 and data.get("unblocked"), str(s))
    s, data, _ = http("POST", "/api/shares", {"nodeId": node_a["id"]}, cookie=sharer_cookie)
    check("解除后可重新分享", s == 200, f"{s} {str(data)[:80]}")
    new_share_id = data["share"]["id"] if s == 200 else None

    # ---------- 收尾清理 ----------
    def cleanup_user(username, cookie, keep_node_ids=()):
        _, listing, _ = http("GET", "/api/nodes", cookie=cookie)
        ids = [n["id"] for n in listing["items"] if n["type"] == "file"]
        # 撤销分享
        _, shares, _ = http("GET", "/api/shares", cookie=cookie)
        for sh in shares.get("items", []):
            http("DELETE", f"/api/shares/{sh['id']}", cookie=cookie)
        for nid in ids:
            http("DELETE", f"/api/nodes/{nid}/purge", cookie=cookie)
        from app import db_failover
        from app.models import User

        db = db_failover.current_sessionmaker()()
        row = db.query(User).filter_by(username=username).first()
        if row:
            db.delete(row)
            db.commit()
        db.close()

    cleanup_user("rptshare", sharer_cookie)
    cleanup_user("rptrecv", receiver_cookie)
    cleanup_user("rptadmin", admin_cookie)

    # 清孤儿 blob（回收站/节点都删了之后）
    import sqlite3

    conn = sqlite3.connect("storage/db/fallback.db")
    cur = conn.cursor()
    orphans = cur.execute(
        "SELECT b.sha256 FROM blobs b WHERE NOT EXISTS "
        "(SELECT 1 FROM nodes n WHERE n.blob_sha256=b.sha256 AND n.deleted_at IS NULL)"
    ).fetchall()
    for (sha,) in orphans:
        p = Path("storage/blobs") / sha[:2] / sha[2:4] / sha
        if p.exists():
            p.unlink()
        cur.execute("DELETE FROM blobs WHERE sha256=?", (sha,))
    conn.commit()
    conn.close()
    print(f"清理 blob {len(orphans)} 个")

    print()
    if failures:
        print(f"共 {len(failures)} 项失败: {failures}")
        sys.exit(1)
    print("全部通过 ✔")


if __name__ == "__main__":
    main()
