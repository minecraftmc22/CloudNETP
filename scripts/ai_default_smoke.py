# -*- coding: utf-8 -*-
"""「AI 总开关 + 默认接口」的离线联调脚本。

用 dev.db / 容灾副本起独立实例（不动真实库），把这几件事跑一遍：
  1. .env 给了一套默认接口时，普通用户直接 ready，但看不到地址与密钥
  2. 管理后台能读到地址 / 密钥掩码，能改能清空（清空后回到 .env）
  3. 用户自己填了完整接口就以用户的为准
  4. AI_ENABLED=false 时接口拒绝、前端拿到「已关闭」
  5. 默认接口不会顺手把「查看文件内容」等权限打开

    ./.venv/Scripts/python.exe scripts/ai_default_smoke.py
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
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"

DEFAULT_BASE = "https://default.example.com/v1"
DEFAULT_KEY = "sk-default-1234567890abcdef"
DEFAULT_MODEL = "default-vision-model"

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


def call(base: str, method: str, path: str, cookie: str | None = None, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", f"cloudnetp_session={cookie}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"detail": raw}
        return exc.code, payload


def wait_up(base: str, proc: subprocess.Popen, log_path: Path) -> bool:
    for _ in range(80):
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


def prepare(tmp: Path) -> tuple[Path, int, int]:
    """复制库，取管理员 / 普通用户 id（没有普通用户就在副本里造一个）。"""
    db_copy = tmp / "smoke.db"
    snapshot_db(db_copy)
    con = sqlite3.connect(str(db_copy))
    admin = con.execute("select id, token_version from users where role = 1 order by id limit 1").fetchone()
    plain = con.execute(
        "select id, token_version from users where role != 1 order by id limit 1"
    ).fetchone()
    if not plain:
        con.execute(
            "insert into users (username, password_hash, display_name, quota_bytes, used_bytes, role,"
            " token_version, created_at) values (?,?,?,?,?,?,?,datetime('now'))",
            ("defaultsmoke", "x", "DefaultSmoke", 10 * 1024 ** 3, 0, 0, 0),
        )
        con.commit()
        plain = con.execute(
            "select id, token_version from users where username = 'defaultsmoke'"
        ).fetchone()
    con.close()
    if not admin:
        raise SystemExit("库里没有管理员，先手动建一个再来跑这个脚本")
    return db_copy, int(admin[0]), int(plain[0])


def start(tmp: Path, db_copy: Path, *, ai_enabled: bool = True) -> tuple[subprocess.Popen, str, Path]:
    from app.config import settings

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
            "DB_FALLBACK_ENABLED": "1",
            "SECRET_KEY": settings.SECRET_KEY,
            # 默认接口：这一组就是 .env 里的样子
            "AI_ENABLED": "true" if ai_enabled else "false",
            "AI_DEFAULT_ENABLED": "true",
            "AI_DEFAULT_BASE_URL": DEFAULT_BASE,
            "AI_DEFAULT_API_KEY": DEFAULT_KEY,
            "AI_DEFAULT_MODEL": DEFAULT_MODEL,
            "AI_DEFAULT_PROTOCOL": "openai",
            "AI_DEFAULT_VISION": "true",
            "AI_DEFAULT_THINKING": "false",
            "AI_DEFAULT_TOOLS": "true",
            "AI_DEFAULT_MAX_INPUT": "32000",
            "AI_DEFAULT_MAX_OUTPUT": "2048",
        }
    )
    log_path = tmp / f"server-{port}.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(VENV_PY), "main.py"], cwd=str(ROOT), env=env,
        stdout=log_file, stderr=subprocess.STDOUT,
    )
    if not wait_up(base, proc, log_path):
        raise SystemExit("服务没起来，看日志：" + str(log_path))
    return proc, base, log_path


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    from app.security import create_token

    tmp = Path(tempfile.mkdtemp(prefix="cloudnetp-ai-default-")).resolve()
    db_copy, admin_id, plain_id = prepare(tmp)
    admin_cookie = create_token(admin_id, 0)
    plain_cookie = create_token(plain_id, 0)
    # token_version 不是 0 时用库里的真实值
    con = sqlite3.connect(str(db_copy))
    admin_tv = con.execute("select token_version from users where id = ?", (admin_id,)).fetchone()[0]
    plain_tv = con.execute("select token_version from users where id = ?", (plain_id,)).fetchone()[0]
    con.close()
    admin_cookie = create_token(admin_id, admin_tv)
    plain_cookie = create_token(plain_id, plain_tv)

    proc, base, _ = start(tmp, db_copy)
    try:
        print("\n[A] 默认接口生效，但普通用户看不到地址与密钥")
        status, data = call(base, "GET", "/api/ai/config", plain_cookie)
        cfg = data.get("config") or {}
        check("配置接口 200", status == 200, str(status))
        check("来源是 default", cfg.get("source") == "default", str(cfg.get("source")))
        check("直接可用（ready）", cfg.get("ready") is True, str(cfg.get("ready")))
        check("地址没有下发给用户", not cfg.get("baseUrl"), str(cfg.get("baseUrl")))
        check("密钥掩码为空", not cfg.get("keyMask"), str(cfg.get("keyMask")))
        check("hasKey 为 false", cfg.get("hasKey") is False, str(cfg.get("hasKey")))
        check("默认模型可见", cfg.get("defaultModel") == DEFAULT_MODEL, str(cfg.get("defaultModel")))
        check("图片输入跟随默认配置", cfg.get("vision") is True, str(cfg.get("vision")))
        check("输入上限跟随默认配置", cfg.get("maxInput") == 32000, str(cfg.get("maxInput")))
        check("AI 总开关开着", cfg.get("aiEnabled") is True, str(cfg.get("aiEnabled")))
        check(
            "默认接口没有顺手打开「查看文件内容」",
            cfg.get("readContent") is False,
            str(cfg.get("readContent")),
        )
        blob = json.dumps(data, ensure_ascii=False)
        check("响应里没有默认密钥原文", DEFAULT_KEY not in blob)
        check("响应里没有默认地址", DEFAULT_BASE not in blob)

        print("\n[B] 管理后台能看到默认接口")
        status, data = call(base, "GET", "/api/admin/ai", admin_cookie)
        acfg = data.get("config") or {}
        check("管理接口 200", status == 200, str(status))
        check("地址对管理员可见", acfg.get("baseUrl") == DEFAULT_BASE, str(acfg.get("baseUrl")))
        check("密钥只回掩码", bool(acfg.get("keyMask")) and DEFAULT_KEY not in json.dumps(acfg), str(acfg.get("keyMask")))
        check("模型可见", acfg.get("model") == DEFAULT_MODEL, str(acfg.get("model")))
        check("来源标成 env", acfg.get("baseUrlSource") == "env", str(acfg.get("baseUrlSource")))
        check("ready 为 true", acfg.get("ready") is True)
        check("总开关可见", acfg.get("aiEnabled") is True)

        print("\n[C] 后台覆盖优先于 .env")
        status, data = call(
            base, "PUT", "/api/admin/ai", admin_cookie,
            {"enabled": True, "model": "admin-picked-model", "maxOutput": 4096},
        )
        check("保存成功", status == 200, str(status))
        _, data = call(base, "GET", "/api/ai/config", plain_cookie)
        check(
            "用户侧拿到后台覆盖的模型",
            (data.get("config") or {}).get("defaultModel") == "admin-picked-model",
            str((data.get("config") or {}).get("defaultModel")),
        )
        _, data = call(base, "GET", "/api/admin/ai", admin_cookie)
        acfg = data.get("config") or {}
        check("来源标成 admin", acfg.get("modelSource") == "admin", str(acfg.get("modelSource")))
        check("输出上限被覆盖", acfg.get("maxOutput") == 4096, str(acfg.get("maxOutput")))
        check("地址仍来自 .env", acfg.get("baseUrlSource") == "env", str(acfg.get("baseUrlSource")))

        print("\n[D] 清空覆盖回到 .env")
        status, data = call(base, "DELETE", "/api/admin/ai", admin_cookie)
        check("清空成功", status == 200, str(status))
        _, data = call(base, "GET", "/api/ai/config", plain_cookie)
        check(
            "模型回到 .env 的值",
            (data.get("config") or {}).get("defaultModel") == DEFAULT_MODEL,
            str((data.get("config") or {}).get("defaultModel")),
        )

        print("\n[E] 用户自己填全了就用用户的")
        status, _ = call(
            base, "PUT", "/api/ai/config", plain_cookie,
            {"baseUrl": "https://mine.example.com/v1", "key": "sk-mine-1234567890", "model": "my-model"},
        )
        check("保存用户配置 200", status == 200, str(status))
        _, data = call(base, "GET", "/api/ai/config", plain_cookie)
        cfg = data.get("config") or {}
        check("来源变成 user", cfg.get("source") == "user", str(cfg.get("source")))
        check("显示用户自己的地址", cfg.get("baseUrl") == "https://mine.example.com/v1", str(cfg.get("baseUrl")))
        check("显示用户自己的模型", cfg.get("model") == "my-model", str(cfg.get("model")))
        check("不再带 defaultModel", not cfg.get("defaultModel"), str(cfg.get("defaultModel")))

        print("\n[F] 清空用户配置后回到默认接口")
        call(
            base, "PUT", "/api/ai/config", plain_cookie,
            {"baseUrl": "", "key": "", "model": ""},
        )
        _, data = call(base, "GET", "/api/ai/config", plain_cookie)
        cfg = data.get("config") or {}
        check("来源回到 default", cfg.get("source") == "default", str(cfg.get("source")))
        check("地址仍然不可见", not cfg.get("baseUrl"), str(cfg.get("baseUrl")))
    finally:
        stop(proc)

    print("\n[G] AI 总开关关闭")
    proc, base, _ = start(tmp, db_copy, ai_enabled=False)
    try:
        status, data = call(base, "GET", "/api/ai/config", plain_cookie)
        cfg = data.get("config") or {}
        check("配置接口仍可读（前端要拿状态）", status == 200, str(status))
        check("aiEnabled=false", cfg.get("aiEnabled") is False, str(cfg.get("aiEnabled")))
        check("ready=false", cfg.get("ready") is False, str(cfg.get("ready")))
        status, data = call(
            base, "POST", "/api/ai/chat", plain_cookie,
            {"messages": [{"role": "user", "content": "在吗"}]},
        )
        check("对话被拒绝 403", status == 403, str(status))
        check("提示说 AI 已关闭", "关闭" in str(data.get("detail")), str(data.get("detail")))
        status, _ = call(
            base, "PUT", "/api/ai/config", plain_cookie,
            {"baseUrl": "https://x.example/v1"},
        )
        check("改用户配置也被拒绝 403", status == 403, str(status))
    finally:
        stop(proc)

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if failed:
        print(f"失败 {failed} 项，通过 {passed} 项")
        return 1
    print(f"全部通过（{passed} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
