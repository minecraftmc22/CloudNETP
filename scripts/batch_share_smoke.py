# -*- coding: utf-8 -*-
"""批量分享（合集链接）冒烟：建合集、公开页元数据、虚拟根列表、子目录、下载、越权。

跑完会留下一条分享和一个测试文件夹，方便接着截图；加 --cleanup 则一并清理。
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

BASE = f"http://127.0.0.1:{int(sys.argv[1]) if len(sys.argv) > 1 else 8000}"
FILE_A = 12  # content.pdf
FILE_B = 47  # john-pork png
OUT_OF_SHARE = 4

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
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return resp.status, json.loads(payload)
            return resp.status, payload[:80]
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw[:120]


def main() -> int:
    db_failover.startup()
    db = db_failover.current_sessionmaker()()
    admin = db.query(User).filter(User.role == 1).order_by(User.id.asc()).first()
    cookie = f"{settings.COOKIE_NAME}={create_token(admin.id, admin.token_version)}"
    db.close()

    # 1. 建文件夹并把文件挪进去，构造「两个文件 + 一个文件夹」的合集
    status, data = http("POST", "/api/nodes/folder", {"name": "批量分享测试", "parentId": None}, cookie)
    check("创建测试文件夹", status == 201, status)
    folder_id = data["node"]["id"]
    status, _ = http("POST", f"/api/nodes/{FILE_B}/move", {"parentId": folder_id}, cookie)
    check("把文件移进文件夹", status == 200, status)

    # 2. 创建合集分享
    status, data = http(
        "POST",
        "/api/shares",
        {
            "nodeIds": [FILE_A, FILE_B, folder_id],
            "expiresDays": 7,
            "extractCode": None,
            "maxDownloads": None,
        },
        cookie,
    )
    check("创建合集分享", status == 200, status)
    share = data["share"]
    token = share["token"]
    check("payload 标记 multi", share["type"] == "multi", share["type"])
    check("payload 项数", share["itemCount"] == 3, share["itemCount"])
    check("payload 名称", share["name"] == "3 个项目", share["name"])
    print(f"  分享链接 {BASE}/s/{token}")

    # 3. 公开页元数据
    status, meta = http("GET", f"/api/public/shares/{token}")
    sm = meta["share"]
    check("公开元数据 200", status == 200, status)
    check("公开 items 数量", len(sm.get("items") or []) == 3, len(sm.get("items") or []))
    check("公开 type=multi", sm["type"] == "multi", sm["type"])
    check("公开总大小 > 0", sm["size"] > 0, sm["size"])

    # 4. 虚拟根列表
    status, listing = http("GET", f"/api/public/shares/{token}/list")
    check("虚拟根列表 200", status == 200, status)
    check("虚拟根 isRoot", listing["folder"]["isRoot"] is True, listing["folder"])
    check("虚拟根 id 为 null", listing["folder"]["id"] is None, listing["folder"]["id"])
    check("虚拟根条目数", listing["total"] == 3, listing["total"])

    # 5. 进入合集中的文件夹
    status, sub = http("GET", f"/api/public/shares/{token}/list?folderId={folder_id}")
    check("进入子文件夹 200", status == 200, status)
    check("子文件夹 1 项", sub["total"] == 1, sub["total"])

    # 6. 下载 / 预览合集中的文件
    status, _ = http("GET", f"/api/public/shares/{token}/download?nodeId={FILE_A}")
    check("下载合集内文件", status == 200, status)
    status, _ = http("GET", f"/api/public/shares/{token}/preview?nodeId={FILE_A}")
    check("预览合集内文件(pdf)", status == 200, status)
    status, _ = http("GET", f"/api/public/shares/{token}/download?nodeId={FILE_B}")
    check("下载子文件夹里的文件", status == 200, status)

    # 7. 越权：不在分享内的节点
    status, _ = http("GET", f"/api/public/shares/{token}/download?nodeId={OUT_OF_SHARE}")
    check("越权节点被拒", status == 404, status)

    # 8. 单文件分享回归
    status, data = http("POST", "/api/shares", {"nodeIds": [FILE_A]}, cookie)
    check("单文件分享仍可用", status == 200 and data["share"]["type"] == "file", status)
    single_id = data["share"]["id"]
    http("DELETE", f"/api/shares/{single_id}", None, cookie)

    print(f"\n通过 {ok_count} / 失败 {fail_count}")
    print(f"合集分享 share_id={share['id']} token={token} folder_id={folder_id}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
