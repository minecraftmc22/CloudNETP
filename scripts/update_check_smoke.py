# -*- coding: utf-8 -*-
"""检查更新冒烟：版本比较 / 缓存 / 错误文案 / 接口鉴权 / 自动检查开关。

不依赖真实 GitHub 网络：服务逻辑用桩数据验证；接口只验证 401、200 与开关往返。

用法：python scripts/update_check_smoke.py [port]
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from app import db_failover, update_service as us  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import SystemSetting, User  # noqa: E402
from app.security import create_token  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
BASE = f"http://127.0.0.1:{PORT}"
ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {label} {extra}")
    else:
        fail += 1
        print(f"  FAIL {label} {extra}")


def http(method, path, body=None, cookie=""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Origin", BASE)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {}


def fake_remote(version: str):
    def fake(url: str):
        if "commits/main" in url:
            return {
                "sha": "abcdef1234567890",
                "html_url": "https://github.com/minecraftmc22/CloudNETP/commit/abcdef1",
                "commit": {"message": "feat: 新版本\n\n正文", "author": {"date": "2026-11-05T00:00:00Z"}},
            }
        content = f'    VERSION: str = "{version}"\n'.encode()
        return {"content": base64.b64encode(content).decode(), "encoding": "base64"}

    return fake


def main():
    print("== 服务逻辑（桩数据） ==")
    original = us._fetch_json
    try:
        for ver, expected, label in [
            ("9.9.9", True, "远端更新"),
            (settings.VERSION, False, "版本相同"),
            ("0.0.1", False, "远端更旧"),
        ]:
            us._cache = {"at": 0, "ttl": 0, "payload": None}
            us._last_force = 0
            us._fetch_json = fake_remote(ver)
            result = us.check(force=True)
            check(label, result["hasUpdate"] is expected, f"latest={result['latest']}")
        check("提交信息解析", result["commit"]["sha"] == "abcdef123456", result["commit"]["sha"])
        check("提交说明取首行", result["commit"]["message"] == "feat: 新版本", result["commit"]["message"])
        cached = us.check(force=False)
        check("二次读取走缓存", cached.get("cached") is True, "")

        def not_found(url):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        us._cache = {"at": 0, "ttl": 0, "payload": None}
        us._last_force = 0
        us._fetch_json = not_found
        err = us.check(force=True)["error"]
        check("404 文案友好", "UPDATE_TOKEN" in err, err)
    finally:
        us._fetch_json = original
        us._cache = {"at": 0, "ttl": 0, "payload": None}

    print("== 接口与开关 ==")
    if not db_failover._started:
        db_failover.startup()
    db = db_failover.current_sessionmaker()()
    admin = db.query(User).filter(User.role == 1).order_by(User.id).first()
    token = create_token(admin.id, admin.token_version)
    cookie = f"{settings.COOKIE_NAME}={token}"
    row = db.get(SystemSetting, "auto_update_check")
    backup = row.v if row else None
    db.close()

    status, _ = http("GET", "/api/version/check")
    check("未登录 401", status == 401, status)
    status, data = http("GET", "/api/version/check", cookie=cookie)
    check("登录后可检查", status == 200 and "current" in data, status)
    status, data = http("PATCH", "/api/admin/settings", {"autoUpdateCheck": True}, cookie=cookie)
    check("开关可开启", status == 200 and data.get("settings", {}).get("autoUpdateCheck") is True, "")
    status, meta = http("GET", "/api/meta")
    check("meta 同步开关", meta.get("autoUpdateCheck") is True, meta.get("autoUpdateCheck"))
    http("PATCH", "/api/admin/settings", {"autoUpdateCheck": False}, cookie=cookie)

    db = db_failover.current_sessionmaker()()
    row = db.get(SystemSetting, "auto_update_check")
    if row:
        if backup is None:
            db.delete(row)
        else:
            row.v = backup
        db.commit()
    db.close()
    print("开关已还原:", backup)

    print(f"\n通过 {ok} 项，失败 {fail} 项")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
