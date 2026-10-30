"""管理后台端到端校验：真实 HTTP + 真实 MySQL。"""

import json
import sys
import urllib.error
import urllib.request

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog, Node, User, utcnow
from app.security import create_token, hash_password
from app.user_service import ensure_settings

BASE = "http://127.0.0.1:8123"
PROBE = "adminprobe"
PROBE_PWD = "ProbePass123"


def call(path, method="GET", body=None, cookie=None):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None, resp.headers
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw.decode("utf-8", "replace")
        return exc.code, payload, exc.headers


def expect(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: {got} (期望 {want})")
    if not ok:
        raise SystemExit(1)


def main():
    with SessionLocal() as db:
        admin = db.get(User, 1)
        token = create_token(admin.id, admin.token_version)
        admin_cookie = f"cloudnetp_session={token}"
        probe = db.scalar(select(User).where(User.username == PROBE))
        if probe:
            from app.admin_service import purge_user

            purge_user(db, probe)
            db.commit()
        probe = User(
            username=PROBE,
            password_hash=hash_password(PROBE_PWD),
            role=0,
            quota_bytes=5 * 1024**3,
            created_at=utcnow(),
        )
        db.add(probe)
        db.commit()
        db.refresh(probe)
        probe_id = probe.id
        ensure_settings(db, probe.id)
    print(f"-- 探针用户 #{probe_id} 已建立")

    # 1. 未登录
    status, body, _ = call("/api/admin/overview")
    expect("未登录访问管理接口", status, 401)

    # 2. 概览
    status, data, _ = call("/api/admin/overview", cookie=admin_cookie)
    expect("概览", status, 200)
    for key in ("users", "nodes", "shares", "storage", "topUsers", "recentLogs", "system"):
        assert key in data, f"概览缺字段 {key}"
    print(f"      用户 {data['users']['total']} · 文件 {data['nodes']['files']} · 版本 {data['system']['version']}")

    # 3. 用户列表 + 搜索
    status, data, _ = call(f"/api/admin/users?pageSize=50&query={PROBE}", cookie=admin_cookie)
    expect("用户搜索", status, 200)
    assert any(item["id"] == probe_id for item in data["items"]), data
    print(f"      总数 {data['total']}，命中探针用户")

    # 4. 调整配额
    new_quota = 8 * 1024**3
    status, data, _ = call(
        f"/api/admin/users/{probe_id}", "PATCH", {"quotaBytes": new_quota}, admin_cookie
    )
    expect("调整配额", status, 200)
    expect("配额已落库", data["user"]["quotaBytes"], new_quota)

    # 5. 越权：普通用户访问管理接口
    status, login, headers = call(
        "/api/auth/login", "POST", {"username": PROBE, "password": PROBE_PWD}
    )
    expect("探针用户登录", status, 200)
    probe_cookie = headers.get("set-cookie").split(";")[0]
    status, _, _ = call("/api/admin/overview", cookie=probe_cookie)
    expect("普通用户访问管理接口（404 不暴露）", status, 404)

    # 6. 重置密码
    status, data, _ = call(f"/api/admin/users/{probe_id}/password", "POST", {}, admin_cookie)
    expect("重置密码", status, 200)
    temp = data["password"]
    expect("临时密码长度", len(temp), 12)
    status, _, _ = call("/api/auth/login", "POST", {"username": PROBE, "password": PROBE_PWD})
    expect("旧密码已失效", status, 401)
    status, login, headers = call("/api/auth/login", "POST", {"username": PROBE, "password": temp})
    expect("新密码可登录", status, 200)
    probe_cookie = headers.get("set-cookie").split(";")[0]

    # 7. 封禁 / 解封
    status, data, _ = call(f"/api/admin/users/{probe_id}", "PATCH", {"banned": True}, admin_cookie)
    expect("封禁", status, 200)
    expect("封禁状态", data["user"]["banned"], True)
    status, body, _ = call("/api/admin/overview", cookie=probe_cookie)
    expect("封禁后旧会话被拦截", status, 403)
    status, _, _ = call("/api/auth/login", "POST", {"username": PROBE, "password": temp})
    expect("封禁后无法登录", status, 403)
    status, data, _ = call(f"/api/admin/users/{probe_id}", "PATCH", {"banned": False}, admin_cookie)
    expect("解封", status, 200)

    # 8. 角色升降
    status, data, _ = call(f"/api/admin/users/{probe_id}", "PATCH", {"role": 1}, admin_cookie)
    expect("设为管理员", status, 200)
    expect("角色", data["user"]["role"], 1)
    status, data, _ = call(f"/api/admin/users/{probe_id}", "PATCH", {"role": 0}, admin_cookie)
    expect("取消管理员", status, 200)
    expect("角色", data["user"]["role"], 0)

    # 9. 自我保护
    status, _, _ = call("/api/admin/users/1", "PATCH", {"banned": True}, admin_cookie)
    expect("封禁自己被拒绝", status, 400)
    status, _, _ = call("/api/admin/users/1", "PATCH", {"role": 0}, admin_cookie)
    expect("给自己降权被拒绝", status, 400)
    status, _, _ = call("/api/admin/users/1", "DELETE", None, admin_cookie)
    expect("删除自己被拒绝", status, 400)

    # 10. 审计日志
    status, data, _ = call("/api/admin/logs?action=admin_user_quota&pageSize=10", cookie=admin_cookie)
    expect("按动作筛选日志", status, 200)
    assert data["items"], "没有 admin_user_quota 记录"
    item = data["items"][0]
    expect("日志标签", item["label"], "调整配额")
    print(f"      detail={item['detail']}")
    status, data, _ = call("/api/admin/logs?query=adminprobe&pageSize=10", cookie=admin_cookie)
    expect("日志按关键词搜索", status, 200)
    assert data["total"] >= 1, data
    status, data, _ = call("/api/admin/logs/actions", cookie=admin_cookie)
    expect("动作字典", status, 200)
    names = [i["action"] for i in data["items"]]
    assert "admin_user_quota" in names and "node_upload" in names
    print(f"      已登记动作 {len(names)} 个")

    # 11. 系统设置
    status, data, _ = call("/api/admin/settings", cookie=admin_cookie)
    expect("读取系统设置", status, 200)
    original = data["settings"]
    status, data, _ = call(
        "/api/admin/settings",
        "PATCH",
        {"siteAnnouncement": "维护测试公告", "trashRetentionDays": 45},
        admin_cookie,
    )
    expect("修改系统设置", status, 200)
    expect("公告已更新", data["settings"]["siteAnnouncement"], "维护测试公告")
    expect("回收站天数已更新", data["settings"]["trashRetentionDays"], 45)
    status, data, _ = call(
        "/api/admin/settings",
        "PATCH",
        {
            "siteAnnouncement": original["siteAnnouncement"],
            "trashRetentionDays": original["trashRetentionDays"],
        },
        admin_cookie,
    )
    expect("恢复系统设置", status, 200)
    status, data, _ = call("/api/meta")
    expect("meta 公告同步", data["announcement"], original["siteAnnouncement"])

    # 12. 磁盘扫描 / 日志清理
    status, data, _ = call("/api/admin/maintenance/blob-gc", "POST", {"apply": False}, admin_cookie)
    expect("孤儿文件扫描", status, 200)
    print(f"      磁盘 {data['diskFiles']} 个文件 / 登记 {data['knownBlobs']} 个 / 孤儿 {data['orphans']} 个")
    status, data, _ = call("/api/admin/logs/purge", "POST", {"days": 3650}, admin_cookie)
    expect("清理旧日志", status, 200)
    print(f"      清理 {data['removed']} 条")

    # 13. 删除用户（级联）
    with SessionLocal() as db:
        node_count = db.query(Node).filter(Node.user_id == probe_id).count()
    status, data, _ = call(f"/api/admin/users/{probe_id}", "DELETE", None, admin_cookie)
    expect("删除用户", status, 200)
    print(f"      清理节点 {data['nodes']} 个 / 分享 {data['shares']} 个")
    with SessionLocal() as db:
        expect("用户已从数据库移除", db.get(User, probe_id), None)
        left = db.query(Node).filter(Node.user_id == probe_id).count()
        expect("该用户节点已清空", left, 0)
        log = db.scalar(
            select(AuditLog).where(AuditLog.action == "admin_user_delete").order_by(AuditLog.id.desc())
        )
        assert log is not None and PROBE in (log.detail or ""), "缺少删除用户审计"
    print(f"      删除前节点数 {node_count}")

    print("\n全部管理后台用例通过")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        sys.exit(1)
