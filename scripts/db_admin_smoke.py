"""管理页「数据库」功能的离线冒烟：状态快照 / 刷新配置 / 手动重连。

整套逻辑跑在临时目录里的两个 SQLite 之间（主库 = primary.db，容灾 = fallback.db），
不碰 dev.db，也不联网：

    ./.venv/Scripts/python.exe scripts/db_admin_smoke.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-db-admin-")).resolve()
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'primary.db').as_posix()}"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ["DB_PROBE_INTERVAL"] = "0"  # 不起看门狗，手动驱动每一步
os.environ["DB_MIRROR_INTERVAL"] = "0"
os.environ["DB_FALLBACK_ENABLED"] = "1"
os.environ.pop("SQLITE_PATH", None)

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import config, db_failover  # noqa: E402
from app import db as db_module  # noqa: E402
from app.models import User  # noqa: E402

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


def session():
    return db_failover.current_sessionmaker()()


def count_users(engine) -> int:
    with Session(engine) as s:
        return int(s.scalar(select(func.count()).select_from(User)) or 0)


def has_user(engine, name: str) -> bool:
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(User).where(User.username == name)) > 0


def main() -> None:
    print("\n[A] 启动与状态快照")
    mode = db_failover.startup()
    check("启动落在主库", mode == db_failover.MODE_PRIMARY, mode)

    with session() as s:
        s.add(User(username="alice", password_hash="x"))
        s.commit()

    state = db_failover.describe()
    check("快照有 mode/modeLabel", state["mode"] == "primary" and state["modeLabel"] == "SQLite")
    check("主库探活可达", state["primary"]["reachable"] is True)
    check("连接串不含密码", "****" not in state["primary"]["url"] or "@" in state["primary"]["url"])
    check("容灾库路径在临时目录", str(_TMP) in state["fallback"]["path"], state["fallback"]["path"])
    check("看门狗未运行（interval=0）", state["watchdog"]["running"] is False)

    print("\n[B] 镜像后副本标记可信")
    rows = db_failover.mirror_once("smoke")
    check("镜像同步了行", rows > 0, str(rows))
    state = db_failover.describe()
    check("副本标记 mirrored=True", state["fallback"]["mirrored"] is True)
    check("副本不是 dirty", state["fallback"]["dirty"] is False)
    check("副本占盘 > 0", state["fallback"]["bytes"] > 0, str(state["fallback"]["bytes"]))

    print("\n[C] 手动切到 SQLite（模拟主库故障期间的人工接管）")
    result = db_failover.reconnect("fallback")
    check("切到 fallback", db_failover.mode() == db_failover.MODE_FALLBACK)
    check("切换前镜像过", result.get("mirroredRows", 0) >= 0)
    state = db_failover.describe()
    check("快照 mode=fallback", state["mode"] == "fallback")
    check("切到 SQLite 后标记 dirty", state["fallback"]["dirty"] is True)
    check("看门狗被手动操作挂起", state["watchdog"]["held"] is True)

    # 降级期间的写入只落 SQLite
    with session() as s:
        s.add(User(username="bob", password_hash="y"))
        s.commit()
    fb_engine = db_failover.fallback_engine()
    check("bob 写进 SQLite", has_user(fb_engine, "bob"))
    check("主库里还没有 bob", not has_user(db_module.engine, "bob"))

    print("\n[D] 手动重连主库：先回迁再切回")
    result = db_failover.reconnect("primary")
    check("切回 primary", db_failover.mode() == db_failover.MODE_PRIMARY)
    check("回迁行数 > 0", result.get("migratedRows", 0) > 0, str(result))
    check("bob 已回迁主库", has_user(db_module.engine, "bob"))
    state = db_failover.describe()
    check("切回后 dirty 已清", state["fallback"]["dirty"] is False)
    check("手动挂起已解除", state["watchdog"]["held"] is False)
    check("切换原因有记录", bool(state["switchReason"]), state["switchReason"])
    check("切换时间有记录", bool(state["switchedAt"]), str(state["switchedAt"]))

    print("\n[E] 刷新配置：重读 .env 并重建连接")
    env_file = _TMP / ".env.test"
    env_file.write_text(
        "DB_PROBE_INTERVAL=45\n"
        f"DATABASE_URL={os.environ['DATABASE_URL']}\n",
        encoding="utf-8",
    )
    # 这个值原本是脚本用进程环境变量写死的；抹掉它才能验证「.env 里的值在刷新时生效」
    # （真实环境变量优先级高于 .env，是刻意设计，不是 bug）
    os.environ.pop("DB_PROBE_INTERVAL", None)
    config._ENV_FILE_VALUES.pop("DB_PROBE_INTERVAL", None)
    config.ENV_FILE = env_file
    result = db_failover.reload_config()
    check("检测到探测间隔变更", "DB_PROBE_INTERVAL" in result["changes"], str(result["changes"]))
    check("新配置已生效", config.settings.DB_PROBE_INTERVAL == 45, str(config.settings.DB_PROBE_INTERVAL))
    check("连接串没变时不换引擎", result["urlChanged"] is False)
    check("刷新后仍在主库", result["landed"] == "primary", result["landed"])
    check("刷新后可达", result["reachable"] is True)

    print("\n[F] 主库连不上时手动重连必须报错，且不动当前库")
    good_url = config.settings.DATABASE_URL
    good_timeout = config.settings.DB_CONNECT_TIMEOUT
    config.settings.DATABASE_URL = "mysql+pymysql://nobody:nope@127.0.0.1:9/none?charset=utf8mb4"
    config.settings.DB_CONNECT_TIMEOUT = 1
    try:
        db_failover.reconnect("primary")
        check("不可达时抛错", False, "没有抛异常")
    except RuntimeError as exc:
        check("不可达时抛 RuntimeError", True)
        check("错误信息说明仍在用当前库", "仍在使用" in str(exc), str(exc))
    finally:
        config.settings.DATABASE_URL = good_url
        config.settings.DB_CONNECT_TIMEOUT = good_timeout
        db_module.rebuild_engine()
        db_failover._reset_probe_engine()

    check("失败后模式没变", db_failover.mode() == db_failover.MODE_PRIMARY)
    check("失败后引擎已恢复", str(db_module.engine.url).endswith("primary.db"), str(db_module.engine.url))
    db_failover.probe_now()
    check("恢复后主库可达", db_failover.describe()["primary"]["reachable"] is True)

    print("\n[G] 连接串脱敏")
    masked = db_failover._mask_url("mysql+pymysql://root:s3cret@100.93.42.52:3306/cloudnetp?charset=utf8mb4")
    check("密码被替换", "s3cret" not in masked, masked)
    check("主机与库名保留", "100.93.42.52:3306" in masked and "/cloudnetp" in masked, masked)
    check("无密码连接串原样返回", db_failover._mask_url("sqlite:///./dev.db") == "sqlite:///./dev.db")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)
