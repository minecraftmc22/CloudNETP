# -*- coding: utf-8 -*-
"""模糊搜索的接口回归：我的文件 / 后台用户 / 审计日志。

  [A] 文件搜索：忽略分隔符、多词 AND、子序列兜底、顺序敏感
  [B] 后台用户：昵称 / 邮箱命中，纯数字走 ID
  [C] 审计日志：中文动作名反查、详情命中、多词收窄

    ./.venv/Scripts/python.exe scripts/fuzzy_search_smoke.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
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


def wait_up(base: str, proc: subprocess.Popen, log_path: Path) -> bool:
    for _ in range(90):
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


def http(base: str, method: str, path: str, body=None, cookie: str = ""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Origin", base)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except Exception:
            return exc.code, {"raw": raw[:200].decode("utf-8", "replace")}


def upload(base: str, cookie: str, name: str, content: bytes, parent_id=None) -> dict:
    status, data = http(
        base, "POST", "/api/uploads/init",
        {"name": name, "size": len(content), "parentId": parent_id}, cookie,
    )
    assert status == 200, (status, data)
    sid = data["sessionId"]
    chunk = data.get("chunkSize") or len(content)
    for index in data.get("missing") or []:
        start = index * chunk
        req = urllib.request.Request(
            f"{base}/api/uploads/{sid}/{index}", data=content[start : start + chunk], method="PUT"
        )
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("Cookie", cookie)
        urllib.request.urlopen(req, timeout=30).read()
    status, done = http(base, "POST", f"/api/uploads/{sid}/complete", None, cookie)
    assert status == 200, (status, done)
    return done["node"]


def q(text: str) -> str:
    return urllib.parse.quote(text)


def search_names(base: str, cookie: str, keyword: str) -> list[str]:
    status, data = http(base, "GET", f"/api/search?q={q(keyword)}", None, cookie)
    assert status == 200, (status, data)
    return [item["name"] for item in data.get("items") or []]


def main() -> int:
    from app.config import settings
    from app.security import hash_password

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-fuzzysmoke-")).resolve()
    db_copy = tmp / "fuzzy.db"
    snapshot_db(db_copy)

    con = sqlite3.connect(str(db_copy))
    row = con.execute(
        "select id, username, token_version from users order by role desc, id limit 1"
    ).fetchone()
    if row is None:
        raise SystemExit("副本库里没有用户")
    admin_id, admin_name, admin_ver = row
    con.execute(
        "update users set password_hash=?, role=1, is_super=1, display_name=? where id=?",
        (hash_password(PASSWORD), "模糊测试管理员", admin_id),
    )
    # 两个对照账号：昵称与邮箱都不含登录名，用来验「不只搜 username」
    for username, display, mail in [
        ("fuzzyprobe001", "张三丰", "zhang@example.com"),
        ("fuzzyprobe002", "李四", "lisi@corp.cn"),
    ]:
        con.execute(
            "insert into users (username, password_hash, display_name, email, quota_bytes, used_bytes,"
            " role, banned, token_version, created_at) values (?,?,?,?,?,?,?,?,?,datetime('now'))",
            (username, "x", display, mail, 10 * 1024**3, 0, 0, 0, 0),
        )
    # 审计记录：一条拒绝快传、一条修改收件目录
    for action, detail in [
        ("quick_send_reject", '{"name": "拒收的合同.pdf"}'),
        ("quick_inbox_folder", '{"path": "我的文件 / 收件箱"}'),
    ]:
        con.execute(
            "insert into audit_logs (user_id, action, detail, ip, created_at)"
            " values (?,?,?,?,datetime('now'))",
            (admin_id, action, detail, "127.0.0.1"),
        )
    con.commit()
    con.close()

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
            "SECRET_KEY": settings.SECRET_KEY,
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
            raise SystemExit("服务没起来：" + str(log_path))

        status, data = http(base, "POST", "/api/auth/login", {"username": admin_name, "password": PASSWORD})
        if status != 200:
            raise SystemExit(f"登录失败 {status} {data}")
        login = data["user"]
        cookie = ""
        # 重新登录一次拿 cookie（上一个响应体里只有用户资料）
        req = urllib.request.Request(
            base + "/api/auth/login",
            data=json.dumps({"username": admin_name, "password": PASSWORD}).encode(),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Origin", base)
        with urllib.request.urlopen(req, timeout=30) as resp:
            for item in resp.headers.get_all("Set-Cookie") or []:
                if item.startswith("cloudnetp_session="):
                    cookie = item.split(";", 1)[0]
        if not cookie:
            raise SystemExit("登录没拿到 cookie")

        # ---------------- A 我的文件 ----------------
        print("\n[A] 文件搜索")
        for name in [
            "2024-年度_报告.pdf",
            "2024-合同草案.docx",
            "report-final.txt",
            "会议记录.docx",
        ]:
            upload(base, cookie, name, b"fuzzy-body-" + name.encode("utf-8"))

        names = search_names(base, cookie, "年度报告")
        check("忽略分隔符：年度报告 → 2024-年度_报告.pdf", "2024-年度_报告.pdf" in names, str(names[:5]))

        names = search_names(base, cookie, "2024 报告")
        check("多词 AND：2024 报告 命中报告", "2024-年度_报告.pdf" in names, str(names[:5]))
        check("多词 AND：2024 报告 不含合同", "2024-合同草案.docx" not in names, str(names[:5]))

        names = search_names(base, cookie, "2024 合同")
        check("多词收窄：2024 合同 命中合同", "2024-合同草案.docx" in names, str(names[:5]))
        check("多词收窄：2024 合同 不含报告", "2024-年度_报告.pdf" not in names, str(names[:5]))

        names = search_names(base, cookie, "报 告")
        check("拆词：报 告 也能命中报告", "2024-年度_报告.pdf" in names, str(names[:5]))

        names = search_names(base, cookie, "rpt")
        check("子序列兜底：rpt → report-final.txt", "report-final.txt" in names, str(names[:5]))

        names = search_names(base, cookie, "录记")
        check("顺序敏感：录记 不命中会议记录", "会议记录.docx" not in names, str(names[:5]))

        names = search_names(base, cookie, "会议记录")
        check("顺序正确：会议记录 命中", "会议记录.docx" in names, str(names[:5]))

        status, data = http(base, "GET", "/api/search?q=", None, cookie)
        check("空关键词不返回任何文件", (data.get("items") or []) == [], str(len(data.get("items") or [])))

        # ---------------- B 后台用户 ----------------
        print("\n[B] 后台用户搜索")
        status, data = http(base, "GET", f"/api/admin/users?query={q('三丰')}", None, cookie)
        users = [item["username"] for item in data.get("items") or []]
        check("按昵称搜到 fuzzyprobe001", "fuzzyprobe001" in users, str(users[:5]))

        status, data = http(base, "GET", f"/api/admin/users?query={q('lisi')}", None, cookie)
        users = [item["username"] for item in data.get("items") or []]
        check("按邮箱搜到 fuzzyprobe002", "fuzzyprobe002" in users, str(users[:5]))

        status, data = http(base, "GET", f"/api/admin/users?query={q('张 三')}", None, cookie)
        users = [item["username"] for item in data.get("items") or []]
        check("多词：张 三 命中张三丰", "fuzzyprobe001" in users, str(users[:5]))

        status, data = http(base, "GET", f"/api/admin/users?query={q('不存在的名字xyz')}", None, cookie)
        check("乱输不误命中", (data.get("items") or []) == [], str(len(data.get("items") or [])))

        # ---------------- C 审计日志 ----------------
        print("\n[C] 审计日志搜索")
        status, data = http(base, "GET", f"/api/admin/logs?query={q('拒绝快传')}", None, cookie)
        actions = [item["action"] for item in data.get("items") or []]
        check("中文动作名反查：拒绝快传", actions and actions[0] == "quick_send_reject", str(actions[:3]))

        status, data = http(base, "GET", f"/api/admin/logs?query={q('收件箱')}", None, cookie)
        details = [item.get("detail") or "" for item in data.get("items") or []]
        check("详情命中：收件箱", any("收件箱" in d for d in details), str(details[:2]))

        status, data = http(base, "GET", f"/api/admin/logs?query={q('拒绝 快传')}", None, cookie)
        actions = [item["action"] for item in data.get("items") or []]
        check("多词也能定位同一个动作", actions and all(a == "quick_send_reject" for a in actions), str(actions[:3]))

        status, data = http(base, "GET", f"/api/admin/logs?query={q('quick_send_reject')}", None, cookie)
        actions = [item["action"] for item in data.get("items") or []]
        check("原始 action 名照样能搜", actions and actions[0] == "quick_send_reject", str(actions[:3]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    print(f"\n结果：{passed} 项通过，{failed} 项失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
