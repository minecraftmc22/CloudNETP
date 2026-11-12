# -*- coding: utf-8 -*-
"""AI 快传工具离线联调（不经过模型，直接调 run_tool_safe）。

  A 没勾「快传取件码」权限时，工具拒绝并提示去哪开
  B create_pickup_code：按路径建码，逐文件设置生效，返回里带 6 位码与有效期
  C pickup_files list：只列清单，不消耗次数
  D pickup_files save：转存到指定目录（不存在会自动建）
  E pickup_files download：给出下载链接
  F 错误取件码：如实报错，不编造成功

    ./.venv/Scripts/python.exe scripts/ai_quick_tool_smoke.py
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-ai-quick-")).resolve()
DB = TMP / "tool.db"


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


snapshot_db(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB.as_posix()}"
os.environ["STORAGE_DIR"] = str(TMP / "storage")
os.environ["DB_FALLBACK_ENABLED"] = "0"
os.environ["DB_PROBE_INTERVAL"] = "0"
os.environ["DB_MIRROR_INTERVAL"] = "0"
sys.path.insert(0, str(ROOT))

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


def main() -> int:
    from app import ai_service, node_service as ns, storage
    from app.db import SessionLocal, init_db
    from app.models import Node, QuickPickupItem, QuickTransfer, User

    init_db()
    db = SessionLocal()
    admin = db.query(User).filter(User.role == 1).order_by(User.id).first()
    if admin is None:
        raise SystemExit("库里没有管理员")

    stamp = str(int(__import__("time").time()))
    content = f"ai-quick-tool-{stamp}".encode() * 50
    sha = hashlib.sha256(content).hexdigest()
    tmp = storage.new_tmp_path()
    tmp.write_bytes(content)
    storage.commit_tmp(tmp, sha)
    file_node = ns.create_file(db, admin, None, f"AI取件样本-{stamp}.txt", sha, len(content), "text/plain")
    db.commit()

    print("\n[A] 没勾权限")
    out = ai_service.run_tool_safe(
        admin.id, "create_pickup_code", {"paths": [file_node.name]}, {"quick": False}
    )
    check("拒绝执行", "失败" in out.text and "快传" in out.text, out.text[:60])

    print("\n[B] 生成取件码")
    out = ai_service.run_tool_safe(
        admin.id,
        "create_pickup_code",
        {"paths": [file_node.name], "days": 1, "max_uses": 2},
        {"quick": True},
    )
    match = re.search(r"取件码：([A-Z0-9]{6})", out.text)
    check("返回里带 6 位码", bool(match), out.text[:80])
    code = match.group(1) if match else ""
    check("说明了有效期", "有效期" in out.text, out.text[:120])
    check("提醒拿到码就能下载", "都能下载" in out.text, "")

    db.expire_all()
    qt = db.query(QuickTransfer).filter(QuickTransfer.code == code).first()
    check("库里建了取件码", qt is not None, code)
    item = (
        db.query(QuickPickupItem)
        .filter(QuickPickupItem.quick_id == qt.id, QuickPickupItem.node_id == file_node.id)
        .first()
        if qt
        else None
    )
    check("逐文件次数写进去了", item is not None and item.max_uses == 2, str(getattr(item, "max_uses", None)))

    print("\n[C] 只看清单")
    out = ai_service.run_tool_safe(
        admin.id, "pickup_files", {"code": code, "action": "list"}, {"quick": True}
    )
    check("列出文件名", file_node.name in out.text, out.text[:80])
    check("提示接下来选转存或下载", "转存" in out.text and "下载" in out.text, "")

    print("\n[D] 转存到指定目录")
    out = ai_service.run_tool_safe(
        admin.id,
        "pickup_files",
        {"code": code, "action": "save", "target_dir": f"AI收件/{stamp}"},
        {"quick": True},
    )
    check("报告转存成功", "已把" in out.text and "转存" in out.text, out.text[:80])
    db.expire_all()
    folder = (
        db.query(Node)
        .filter(Node.user_id == admin.id, Node.type == "folder", Node.name == stamp)
        .first()
    )
    check("目录被自动建出来", folder is not None, stamp)
    copied = (
        db.query(Node)
        .filter(Node.user_id == admin.id, Node.parent_id == (folder.id if folder else -1))
        .all()
    )
    check("文件复制进去了", any(node.name == file_node.name for node in copied), str([n.name for n in copied]))

    print("\n[E] 下载链接")
    out = ai_service.run_tool_safe(
        admin.id, "pickup_files", {"code": code, "action": "download"}, {"quick": True}
    )
    check("给出 download 链接", f"/download?nodeId={file_node.id}" in out.text, out.text[:100])

    print("\n[F] 错误取件码")
    out = ai_service.run_tool_safe(
        admin.id, "pickup_files", {"code": "ZZZZZZ", "action": "list"}, {"quick": True}
    )
    check("如实报错", "不存在" in out.text and "取件码" in out.text, out.text[:60])

    db.close()
    print(f"\n结果：{passed} 通过，{failed} 失败")
    import shutil
    if failed == 0:
        shutil.rmtree(TMP, ignore_errors=True)
    else:
        print("临时目录保留：", TMP)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
