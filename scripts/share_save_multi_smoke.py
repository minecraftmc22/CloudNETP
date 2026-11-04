# -*- coding: utf-8 -*-
"""公开分享多项转存冒烟：合集勾选转存、单项兼容、越权拦截、配额预检。

用法：python scripts/share_save_multi_smoke.py [port] [--cleanup]
默认跑完保留数据方便截图，加 --cleanup 则全部还原。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from app import db_failover  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import User  # noqa: E402
from app.security import create_token  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
CLEANUP = "--cleanup" in sys.argv
BASE = f"http://127.0.0.1:{PORT}"

FILE_A = 12  # content.pdf
FILE_B = 47  # john-pork png
OUTSIDE = 4  # 不在分享里的节点

ok_count = 0
fail_count = 0


def check(label, cond, extra=""):
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  OK   {label} {extra}")
    else:
        fail_count += 1
        print(f"  FAIL {label} {extra}")


def http(method, path, body=None, cookie=""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Origin", BASE)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return resp.status, json.loads(raw)
            return resp.status, raw[:80]
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw[:120]


def main():
    db_failover.startup()
    db = db_failover.current_sessionmaker()()
    admin = db.query(User).filter(User.role == 1).order_by(User.id).first()
    user_id = admin.id
    cookie = f"{settings.COOKIE_NAME}={create_token(admin.id, admin.token_version)}"
    used_before = int(admin.used_bytes or 0)
    db.close()

    print("== 准备：建文件夹并把 content.pdf 放进去 ==")
    st, d = http("POST", "/api/nodes/folder", {"name": "转存测试夹", "parentId": None}, cookie)
    check("建文件夹", st in (200, 201) and d.get("node"), st)
    folder_id = d["node"]["id"]
    st, _ = http("POST", f"/api/nodes/{FILE_A}/move", {"parentId": folder_id}, cookie)
    check("移入文件夹", st == 200, st)

    print("== 创建合集分享 ==")
    st, d = http("POST", "/api/shares", {"nodeIds": [folder_id, FILE_B], "expiresDays": 7}, cookie)
    check("创建合集", st == 200 and d["share"]["itemCount"] == 2, d.get("share", {}).get("itemCount"))
    token = d["share"]["token"]
    share_id = d["share"]["id"]

    print("== 多项转存 ==")
    st, r = http(
        "POST",
        f"/api/public/shares/{token}/save",
        {"nodeIds": [folder_id, FILE_B]},
        cookie,
    )
    check("转存成功", st == 200 and r.get("count") == 2, (st, r if st != 200 else r.get("count")))
    copies = r.get("nodeIds") or []
    check("返回两个新节点", len(copies) == 2, copies)

    # 转存出来的文件夹里应该带着 content.pdf
    if copies:
        st, listing = http("GET", f"/api/nodes?parentId={copies[0]}", cookie=cookie)
        names = [i["name"] for i in listing.get("items", [])] if st == 200 else []
        check("文件夹副本含原文件", "content.pdf" in names, names)

    print("== 单项转存（老接口兼容） ==")
    st, r1 = http("POST", f"/api/public/shares/{token}/save", {"nodeId": FILE_B}, cookie)
    check("nodeId 仍可用", st == 200 and r1.get("count") == 1, (st, r1 if st != 200 else r1.get("count")))

    print("== 不传参数 = 整条分享 ==")
    st, r2 = http("POST", f"/api/public/shares/{token}/save", {}, cookie)
    check("空 body 转存全部根", st == 200 and r2.get("count") == 2, (st, r2 if st != 200 else r2.get("count")))

    print("== 越权与去重 ==")
    st, r3 = http("POST", f"/api/public/shares/{token}/save", {"nodeIds": [FILE_B, OUTSIDE]}, cookie)
    check("含分享外的节点 → 404", st == 404, st)
    st, r4 = http("POST", f"/api/public/shares/{token}/save", {"nodeIds": [FILE_B, FILE_B]}, cookie)
    check("重复 id 去重", st == 200 and r4.get("count") == 1, (st, r4 if st != 200 else r4.get("count")))

    print("== 配额 ==")
    dbf = db_failover.current_sessionmaker()()
    fresh = dbf.query(User).filter(User.id == user_id).first()
    used_after = int(fresh.used_bytes or 0)
    dbf.close()
    check("用量有增长", used_after > used_before, f"{used_before} → {used_after}")

    # 收尾
    created = [i for sub in (copies,) for i in sub]
    for nid in created:
        http("DELETE", f"/api/nodes/{nid}", None, cookie)
    for nid in (r1.get("nodeId"), r2.get("nodeId"), r4.get("nodeId")):
        if nid:
            http("DELETE", f"/api/nodes/{nid}", None, cookie)
    if CLEANUP:
        http("POST", f"/api/nodes/{FILE_A}/move", {"parentId": None}, cookie)
        http("DELETE", f"/api/nodes/{folder_id}", None, cookie)
        http("DELETE", f"/api/shares/{share_id}", None, cookie)
        print("已清理测试数据")

    print(f"\n通过 {ok_count} · 失败 {fail_count}")
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
