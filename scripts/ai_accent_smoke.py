# -*- coding: utf-8 -*-
"""AI 取色（设置 → 主题色）的离线联调：图片规整、提示词、颜色解析与路由门禁。

不碰真实库：临时 SQLite + 假 OpenAI 兼容服务，模型只回一段 JSON 文本。

    .venv/Scripts/python scripts/ai_accent_smoke.py
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="cloudnetp-accent-smoke-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(_TMP_DIR / 'smoke.db').as_posix()}")
os.environ.setdefault("STORAGE_DIR", str(_TMP_DIR / "storage"))

from fastapi import HTTPException  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from app import ai_defaults, ai_service as ai  # noqa: E402
from app.db import SessionLocal, engine, ensure_schema  # noqa: E402
from app.models import User  # noqa: E402
from app.routers import ai as ai_router  # noqa: E402
from app.schemas import AiAccentIn  # noqa: E402

ensure_schema(engine)

with SessionLocal() as _db:
    _u = User(username="accent", password_hash="x", display_name="Accent", quota_bytes=1 << 30, used_bytes=0)
    _db.add(_u)
    _db.commit()
    USER_ID = int(_u.id)
    user = _u

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def png_data_url(width: int = 2000, height: int = 1200) -> str:
    """造一张左右双色的大图，用来验「压缩到 768」与宽高比。"""
    picture = Image.new("RGB", (width, height), (226, 88, 148))
    ImageDraw.Draw(picture).rectangle([width // 2, 0, width, height], fill=(72, 140, 232))
    buffer = io.BytesIO()
    picture.save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    reply = '{"colors": ["#8a5cf6", "#f472b6"]}'
    seen: list[dict] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) or b"{}"
        try:
            Handler.seen.append(json.loads(raw))
        except json.JSONDecodeError:
            Handler.seen.append({})
        line = json.dumps({"choices": [{"delta": {"content": Handler.reply}}]}, ensure_ascii=False)
        body = f"data: {line}\n\ndata: [DONE]\n\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=SERVER.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{SERVER.server_address[1]}/v1"


def shell_user():
    return SimpleNamespace(
        id=USER_ID,
        username="accent",
        display_name="Accent",
        quota_bytes=0,
        used_bytes=0,
    )


def fake_request():
    # check_origin：没有 Origin 头直接放行；log_action 只取 IP
    return SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))


def configured_cfg(**extra):
    cfg = dict(ai.DEFAULT_CONFIG)
    cfg.update(baseUrl=BASE, key="sk-test", model="test-model", tools=False, vision=True, thinking=False)
    cfg.update(extra)
    return cfg


def main() -> int:
    with SessionLocal() as db:
        ai.save_config(
            db,
            USER_ID,
            {"baseUrl": BASE, "key": "sk-test", "model": "test-model", "tools": False, "vision": True},
        )
        cfg = ai.load_config(db, USER_ID)
        image = png_data_url()

        print("[1] 图片规整")
        small = ai.normalize_accent_image(image)
        check("输出成 JPEG data URL", small.startswith("data:image/jpeg;base64,"), small[:32])
        with Image.open(io.BytesIO(base64.b64decode(small.split(",", 1)[1]))) as out:
            check("长边压到 768", max(out.size) == 768, str(out.size))
            check("宽高比保持", abs(out.size[0] / out.size[1] - 2000 / 1200) < 0.02, str(out.size))
        try:
            ai.normalize_accent_image("data:image/png;base64,bm90LWFuLWltYWdl")
            check("非位图要报错", False)
        except ai.AiError as exc:
            check("非位图要报错", "读不出来" in exc.message, exc.message)
        check("空串直接放行（无图场景）", ai.normalize_accent_image("") == "")

        print("[2] 单色：只要第一个颜色")
        Handler.reply = '{"colors": ["#8a5cf6", "#f472b6"]}'
        Handler.seen.clear()
        result = ai.suggest_accent(shell_user(), cfg, mode="solid", image=small, lang="zh-CN")
        check("mode 回 solid", result["mode"] == "solid", str(result))
        check("取第一个颜色", result["colors"][0] == "#8a5cf6", str(result["colors"]))
        check(
            "单色只问一个主色",
            "1 个最有代表性的主色" in json.dumps(Handler.seen[-1], ensure_ascii=False),
        )
        check(
            "开了视觉就真的把图递过去",
            any(
                isinstance(m.get("content"), list)
                and any(part.get("type") == "image_url" for part in m["content"])
                for m in Handler.seen[-1].get("messages", [])
            ),
            "no image part",
        )

        print("[3] 渐变：多个颜色、去重、上限 5")
        Handler.reply = '颜色是 #8a5cf6 和 #F472B6，再加 #8a5cf6、#22d3ee、#fbbf24、#34d399、#60a5fa'
        result = ai.suggest_accent(shell_user(), cfg, mode="gradient", image=small, lang="zh-CN")
        check("mode 回 gradient", result["mode"] == "gradient", str(result))
        check("大小写统一并去重", result["colors"][:2] == ["#8a5cf6", "#f472b6"], str(result["colors"]))
        check("最多 5 个", len(result["colors"]) == 5, str(result["colors"]))

        print("[4] 渐变只给一个色 → 降级单色")
        Handler.reply = '{"colors": ["#123456"]}'
        result = ai.suggest_accent(shell_user(), cfg, mode="gradient", image=small, lang="zh-CN")
        check("降级成 solid", result["mode"] == "solid", str(result))
        check("颜色保留", result["colors"] == ["#123456"], str(result["colors"]))

        print("[5] 模型没给颜色")
        Handler.reply = "这张图很好看，我挑不出来。"
        try:
            ai.suggest_accent(shell_user(), cfg, mode="solid", image=small, lang="zh-CN")
            check("没颜色要报错", False)
        except ai.AiError as exc:
            check("没颜色要报错", "没挑出" in exc.message, exc.message)

        print("[6] 非视觉模型：退回候选色")
        Handler.reply = '{"colors": ["#e2589e"]}'
        Handler.seen.clear()
        vision_off = dict(cfg)
        vision_off["vision"] = False
        result = ai.suggest_accent(
            shell_user(), vision_off, mode="solid", image=small, candidates=["#e2589e", "#488ce8"], lang="zh-CN"
        )
        check("照样能出颜色", result["colors"] == ["#e2589e"], str(result))
        body = json.dumps(Handler.seen[-1], ensure_ascii=False)
        check("请求里没有图片", "image_url" not in body, body[:120])
        check("候选色写进了提示词", "#e2589e" in body and "#488ce8" in body)

        print("[7] 路由：没配置就不给用")
        ai.save_config(db, USER_ID, {"baseUrl": "", "key": "", "model": ""})
        try:
            ai_router.pick_accent(
                AiAccentIn.model_validate({"mode": "solid", "image": small}),
                fake_request(),
                db,
                user,
            )
            check("未配置 → 409", False)
        except HTTPException as exc:
            check("未配置 → 409", exc.status_code == 409, str(exc.status_code))
            check("文案提示去填配置", "AI 配置" in str(exc.detail), str(exc.detail))

        print("[8] 路由：管理员关了 AI 也不给用")
        ai.save_config(
            db,
            USER_ID,
            {"baseUrl": BASE, "key": "sk-test", "model": "test-model", "vision": True},
        )
        original = ai_defaults.ai_enabled
        ai_defaults.ai_enabled = lambda: False
        try:
            ai_router.pick_accent(
                AiAccentIn.model_validate({"mode": "solid", "image": small}),
                fake_request(),
                db,
                user,
            )
            check("关闭 AI → 403", False)
        except HTTPException as exc:
            check("关闭 AI → 403", exc.status_code == 403, str(exc.status_code))
        finally:
            ai_defaults.ai_enabled = original

        print("[9] 路由：正常出颜色")
        Handler.reply = '{"colors": ["#7c3aed", "#ec4899", "#f59e0b"]}'
        result = ai_router.pick_accent(
            AiAccentIn.model_validate({"mode": "gradient", "image": small, "candidates": ["#7c3aed"]}),
            fake_request(),
            db,
            user,
        )
        check("路由返回颜色", result["colors"][:2] == ["#7c3aed", "#ec4899"], str(result))
        check("路由回 gradient", result["mode"] == "gradient", str(result))

        print("[9b] 取色用的是「看板娘 · 对话设置」里那套接口")
        from sqlalchemy import select

        from app.models import AuditLog

        eff, source = ai.effective_config(db, USER_ID)
        check("来源是用户自己的配置", source == "user", source)
        check("地址跟着对话设置", eff["baseUrl"] == BASE, eff["baseUrl"])
        check("模型跟着对话设置", eff["model"] == "test-model", eff["model"])
        last = db.scalars(
            select(AuditLog)
            .where(AuditLog.action == "ai_accent_pick")
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).first()
        detail = json.loads(last.detail) if last and last.detail else {}
        check("审计里注明用的哪套接口", detail.get("from") == "user", str(detail))

        print("[10] 路由：坏图算输入错误")
        try:
            ai_router.pick_accent(
                AiAccentIn.model_validate({"mode": "solid", "image": "data:image/png;base64,bm90LWFuLWltYWdl"}),
                fake_request(),
                db,
                user,
            )
            check("坏图 → 400", False)
        except HTTPException as exc:
            check("坏图 → 400", exc.status_code == 400, str(exc.status_code))

        print("[11] 路由：没图直接拒绝")
        try:
            ai_router.pick_accent(
                AiAccentIn.model_validate({"mode": "solid", "image": ""}), fake_request(), db, user
            )
            check("空图 → 400", False)
        except HTTPException as exc:
            check("空图 → 400", exc.status_code == 400, str(exc.status_code))

    SERVER.shutdown()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
