# -*- coding: utf-8 -*-
"""举报内容核对冒烟：文件清单、缩略图、预览流、越权拦截。

用法：python scripts/report_preview_smoke.py [port] [--cleanup]
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
from app.models import ShareReport, User  # noqa: E402
from app.security import create_token  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
CLEANUP = "--cleanup" in sys.argv
BASE = f"http://127.0.0.1:{PORT}"

IMG = 47  # png 图片
PDF = 12  # pdf
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


def http(method, path, body=None, cookie="", raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Origin", BASE)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if raw:
                return resp.status, payload, ctype
            if "json" in ctype:
                return resp.status, json.loads(payload)
            return resp.status, payload[:80]
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload)
        except Exception:
            return exc.code, payload[:120]


def main():
    db_failover.startup()
    db = db_failover.current_sessionmaker()()
    admin = db.query(User).filter(User.role == 1).order_by(User.id).first()
    cookie = f"{settings.COOKIE_NAME}={create_token(admin.id, admin.token_version)}"
    db.close()

    print("== 合集分享 + 举报 ==")
    st, d = http("POST", "/api/shares", {"nodeIds": [IMG, PDF], "expiresDays": 7}, cookie)
    check("创建合集分享", st == 200, st)
    token = d["share"]["token"]
    share_id = d["share"]["id"]
    st, rep = http("POST", f"/api/public/shares/{token}/report", {"reason": "测试：内容可能违规"})
    check("匿名举报成功", st == 200 and rep.get("id"), (st, rep))
    report_id = rep.get("id")

    print("== 后台看举报 ==")
    st, listing = http("GET", "/api/admin/reports?page=1&pageSize=20&status=pending", None, cookie)
    row = next((i for i in listing.get("items", []) if i["id"] == report_id), None) if st == 200 else None
    check("列表里能找到该举报", bool(row), st)
    if row:
        check("列表带类型字段", row.get("previewKind") is not None, row.get("previewKind"))
        print("     列表行:", {k: row.get(k) for k in ("firstName", "isFolder", "previewKind", "icon", "nodeId", "size")})

    print("== 文件清单 ==")
    st, files = http("GET", f"/api/admin/reports/{report_id}/files", None, cookie)
    check("清单接口 200", st == 200, st)
    items = files.get("items", []) if st == 200 else []
    check("清单含 2 个文件", len(items) == 2, [i["name"] for i in items])
    kinds = {i["previewKind"] for i in items}
    check("类型识别正确", kinds == {"image", "pdf"}, kinds)
    thumb_item = next((i for i in items if i["previewKind"] == "image"), None)
    check("图片项可用于缩略图", bool(thumb_item), thumb_item and thumb_item["name"])

    print("== 缩略图 ==")
    if thumb_item:
        st, payload, ctype = http(
            "GET",
            f"/api/admin/reports/{report_id}/thumb?nodeId={thumb_item['nodeId']}&w=160",
            None,
            cookie,
            raw=True,
        )
        check("缩略图 200", st == 200, st)
        check("是 webp", "image/webp" in (ctype or ""), ctype)
        check("缩略图非空", len(payload) > 500, len(payload))
        check("webp 魔数", payload[:4] == b"RIFF" and payload[8:12] == b"WEBP", payload[:12])

    print("== 内容预览 ==")
    for it in items:
        st, payload, ctype = http(
            "GET", f"/api/admin/reports/{report_id}/preview?nodeId={it['nodeId']}", None, cookie, raw=True
        )
        check(f"预览 {it['name'][:20]}", st == 200 and len(payload) == it["size"], (st, len(payload), it["size"]))

    print("== 越权：拼一个不在举报里的节点 ==")
    st, _ = http("GET", f"/api/admin/reports/{report_id}/preview?nodeId={OUTSIDE}", None, cookie, raw=True)
    check("分享外节点被拒", st in (404, 403), st)

    print("== 单文件举报 ==")
    st, d2 = http("POST", "/api/shares", {"nodeId": IMG, "expiresDays": 7}, cookie)
    token2, share2 = d2["share"]["token"], d2["share"]["id"]
    st, rep2 = http("POST", f"/api/public/shares/{token2}/report", {"reason": "单文件"})
    report2 = rep2.get("id")
    st, files2 = http("GET", f"/api/admin/reports/{report2}/files", None, cookie)
    items2 = files2.get("items", []) if st == 200 else []
    check("单文件清单 1 项", len(items2) == 1, [i["name"] for i in items2])
    if items2:
        st, payload, ctype = http(
            "GET", f"/api/admin/reports/{report2}/preview?nodeId={items2[0]['nodeId']}", None, cookie, raw=True
        )
        check("单文件预览可用", st == 200 and len(payload) == items2[0]["size"], (st, len(payload)))

    print("== 不传 nodeId 时默认给第一个文件 ==")
    st, payload, ctype = http("GET", f"/api/admin/reports/{report_id}/preview", None, cookie, raw=True)
    check("默认预览 200", st == 200, (st, len(payload)))

    # 清理
    http("DELETE", f"/api/shares/{share_id}", None, cookie)
    http("DELETE", f"/api/shares/{share2}", None, cookie)
    dbf = db_failover.current_sessionmaker()()
    for rid in (report_id, report2):
        r = dbf.get(ShareReport, rid)
        if r and CLEANUP:
            dbf.delete(r)
    dbf.commit()
    dbf.close()
    print("已撤销测试分享" + ("，并删除举报记录" if CLEANUP else "（举报记录保留，加 --cleanup 可删）"))

    print(f"\n通过 {ok_count} · 失败 {fail_count}")
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
