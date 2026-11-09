"""「查看文件内容」也能看图片的离线联调脚本。

不需要真实 API Key：本机起一个假的 OpenAI / Anthropic 兼容服务，把
工具下发规则、权限拦截、缩图 / 转码 / 透明底、两种协议把图片递给模型的位置、
事件里的 images 计数与审计日志全部跑一遍。

    .venv/Scripts/python scripts/ai_image_smoke.py

（数据库与 blob 都落在临时目录，跑完就删，不碰 dev.db 和真实 storage。）
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="cloudnetp-ai-image-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(_TMP_DIR / 'smoke.db').as_posix()}")
os.environ.setdefault("STORAGE_DIR", str(_TMP_DIR / "storage"))

from PIL import Image  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import ai_service as ai, node_service  # noqa: E402
from app.db import SessionLocal, engine, ensure_schema  # noqa: E402
from app.models import AuditLog, User  # noqa: E402

ensure_schema(engine)

with SessionLocal() as _db:
    _user = User(
        username="img",
        password_hash="x",
        display_name="Img",
        quota_bytes=512 * 1024 * 1024,
        used_bytes=0,
    )
    _db.add(_user)
    _db.commit()
    USER_ID = int(_user.id)

STATE: dict[str, list] = {"openai": [], "anthropic": []}


def png_bytes(size: tuple[int, int], color) -> bytes:
    image = Image.new("RGBA", size, color)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def bmp_bytes(size: tuple[int, int], color) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, "BMP")
    return buffer.getvalue()


def add_file(name: str, data: bytes, mime: str) -> None:
    with SessionLocal() as db:
        user = db.get(User, USER_ID)
        sha, size = ai._store_bytes(data)
        node_service.create_file(db, user, None, name, sha, size, mime)
        node_service.recalc_usage(db, user)
        db.commit()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_open(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _sse(self, obj: dict, event: str | None = None) -> None:
        chunk = (f"event: {event}\n" if event else "") + "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
        self.wfile.write(chunk.encode())
        self.wfile.flush()

    def do_GET(self):
        if self.path.endswith("/models"):
            self._json({"object": "list", "data": [{"id": "mock-vision"}]})
            return
        self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        body = self._body()
        if self.path.endswith("/chat/completions"):
            self._openai(body)
        elif self.path.endswith("/messages"):
            self._anthropic(body)
        else:
            self._json({"error": {"message": "not found"}}, 404)

    # ---------------- OpenAI 兼容 ----------------
    def _openai(self, body: dict) -> None:
        STATE["openai"].append(body)
        messages = body.get("messages") or []
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        self._sse_open()
        if not has_tool_result:
            self._sse(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_img",
                                        "type": "function",
                                        "function": {
                                            "name": "read_image",
                                            "arguments": '{"path": "pic.png"}',
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                }
            )
            self._sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        else:
            for piece in ["看到啦", "，是一张红色的图。"]:
                self._sse({"choices": [{"index": 0, "delta": {"content": piece}}]})
            self._sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    # ---------------- Anthropic ----------------
    def _anthropic(self, body: dict) -> None:
        STATE["anthropic"].append(body)
        messages = body.get("messages") or []
        has_tool_result = any(m.get("role") == "user" and isinstance(m.get("content"), list) for m in messages)
        self._sse_open()
        self._sse({"type": "message_start", "message": {"id": "msg_1", "role": "assistant"}}, "message_start")
        if not has_tool_result:
            self._sse(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "tool_img", "name": "read_image"},
                },
                "content_block_start",
            )
            self._sse(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"path": "pic.png"}'},
                },
                "content_block_delta",
            )
            self._sse(
                {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}},
                "message_delta",
            )
        else:
            self._sse(
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                "content_block_start",
            )
            for piece in ["看到啦", "，是一张红色的图。"]:
                self._sse(
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}},
                    "content_block_delta",
                )
            self._sse(
                {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}},
                "message_delta",
            )
        self._sse({"type": "message_stop"}, "message_stop")


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        if cond:
            print(f"  ok   {name}")
        else:
            print(f"  FAIL {name} {extra}")
            failures.append(name)

    # ---------------- 素材 ----------------
    pic_raw = png_bytes((320, 240), (220, 40, 60, 255))
    big_raw = png_bytes((4000, 3000), (30, 120, 200, 255))
    transparent_raw = png_bytes((3000, 2000), (0, 0, 0, 0))
    bmp_raw = bmp_bytes((800, 600), (10, 200, 90))
    add_file("pic.png", pic_raw, "image/png")
    add_file("big.png", big_raw, "image/png")
    add_file("alpha.png", transparent_raw, "image/png")
    add_file("pic.bmp", bmp_raw, "image/bmp")
    add_file("note.txt", "这是文本，不是图。".encode(), "text/plain")
    add_file("vector.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>', "image/svg+xml")
    add_file("huge.png", b"\0" * (21 * 1024 * 1024), "image/png")

    print("1. 工具按权限下发")
    on = {s["function"]["name"] for s in ai.tool_specs({"tools": True, "readContent": True, "vision": True})}
    check("读内容 + 图片输入 → 有 read_image", "read_image" in on, str(sorted(on)))
    check("同时保留 read_text", "read_text" in on, str(sorted(on)))
    no_vision = {s["function"]["name"] for s in ai.tool_specs({"tools": True, "readContent": True, "vision": False})}
    check("没开图片输入 → 不给 read_image", "read_image" not in no_vision, str(sorted(no_vision)))
    check("read_text 仍然在", "read_text" in no_vision, str(sorted(no_vision)))
    no_read = {s["function"]["name"] for s in ai.tool_specs({"tools": True, "readContent": False, "vision": True})}
    check("没开查看内容 → 读的两个都不给", not ({"read_text", "read_image"} & no_read), str(sorted(no_read)))
    check(
        "总开关关掉 → 一个都不给",
        ai.tool_specs({"tools": False, "readContent": True, "vision": True}) == [],
    )

    print("2. 未授权时拒绝")
    blocked = ai.run_tool_safe(USER_ID, "read_image", {"path": "pic.png"}, {"read": False})
    check("提示去开「查看文件内容」", "查看文件内容" in blocked.text, blocked.text)
    check("没有带出图片", blocked.images == [], str(blocked.images))
    no_vision = ai.run_tool_safe(
        USER_ID, "read_image", {"path": "pic.png"}, {"read": True, "vision": False}
    )
    check("开了读但没开图片输入也会拒绝", "图片输入" in no_vision.text, no_vision.text)
    check("同样没带出图片", no_vision.images == [], str(no_vision.images))

    print("3. 小 PNG 原样返回")
    got = ai.run_tool_safe(USER_ID, "read_image", {"path": "pic.png"}, {"read": True, "vision": True})
    check("返回了一张图", len(got.images) == 1, str(len(got.images)))
    block = got.images[0] if got.images else {}
    check("mime 是 png", block.get("media_type") == "image/png", str(block.get("media_type")))
    decoded = base64.b64decode(block.get("data") or "")
    check("小图不重编码（字节一致）", decoded == pic_raw, f"{len(decoded)} vs {len(pic_raw)}")
    check("文本里带尺寸", "320×240" in got.text and "pic.png" in got.text, got.text)

    print("4. 超大尺寸自动缩小并转 JPEG")
    got = ai.run_tool_safe(USER_ID, "read_image", {"path": "big.png"}, {"read": True, "vision": True})
    block = got.images[0] if got.images else {}
    data = base64.b64decode(block.get("data") or b"")
    with Image.open(io.BytesIO(data)) as shrunk:
        edge = max(shrunk.size)
        fmt = shrunk.format
    check("长边缩到 1568", edge == ai.MAX_IMAGE_EDGE, str(edge))
    check("转成 JPEG", block.get("media_type") == "image/jpeg" and fmt == "JPEG", f"{block.get('media_type')} {fmt}")
    check("体积可控", len(data) < 1_500_000, str(len(data)))

    print("5. BMP 转成模型认的格式")
    got = ai.run_tool_safe(USER_ID, "read_image", {"path": "pic.bmp"}, {"read": True, "vision": True})
    block = got.images[0] if got.images else {}
    check("BMP → JPEG", block.get("media_type") == "image/jpeg", str(block.get("media_type")))

    print("6. 透明底铺白，不会变黑块")
    got = ai.run_tool_safe(USER_ID, "read_image", {"path": "alpha.png"}, {"read": True, "vision": True})
    block = got.images[0] if got.images else {}
    with Image.open(io.BytesIO(base64.b64decode(block.get("data") or b""))) as flat:
        pixel = flat.convert("RGB").getpixel((max(1, flat.width // 2), max(1, flat.height // 2)))
    check("中心像素接近白色", all(channel > 240 for channel in pixel), str(pixel))

    print("7. 非图片 / SVG / 超大文件")
    text_node = ai.run_tool_safe(USER_ID, "read_image", {"path": "note.txt"}, {"read": True, "vision": True})
    check("文本文件当图片读会报错", "不是图片" in text_node.text, text_node.text)
    svg_node = ai.run_tool_safe(USER_ID, "read_image", {"path": "vector.svg"}, {"read": True, "vision": True})
    check("SVG 提示转 PNG", "SVG" in svg_node.text and "PNG" in svg_node.text, svg_node.text)
    huge = ai.run_tool_safe(USER_ID, "read_image", {"path": "huge.png"}, {"read": True, "vision": True})
    check("超过 20 MB 直接拒绝", "20 MB" in huge.text, huge.text)
    missing = ai.run_tool_safe(USER_ID, "read_image", {"path": "不存在.png"}, {"read": True, "vision": True})
    check("找不到文件有提示", "找不到" in missing.text, missing.text)

    print("8. 读操作写审计")
    with SessionLocal() as db:
        rows = db.scalars(
            select(AuditLog).where(AuditLog.user_id == USER_ID, AuditLog.action == "ai_tool")
        ).all()
        details = " | ".join(row.detail or "" for row in rows)
    check("审计里有 read_image", "read_image" in details, details[:200])
    check("审计里带文件路径", "pic.png" in details, details[:200])
    read_text = ai.run_tool_safe(USER_ID, "read_text", {"path": "note.txt"}, {"read": True, "vision": True})
    check("文本也能读", "这是文本" in read_text.text, read_text.text)
    with SessionLocal() as db:
        rows = db.scalars(
            select(AuditLog).where(AuditLog.user_id == USER_ID, AuditLog.action == "ai_tool")
        ).all()
        details = " | ".join(row.detail or "" for row in rows)
    check("阅读正文也记了审计", "read_text" in details and "note.txt" in details, details[:200])

    # ---------------- 端到端：图片真的递给模型 ----------------
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = int(server.server_address[1])
    shell = SimpleNamespace(
        id=USER_ID,
        username="img",
        display_name="Img",
        quota_bytes=0,
        used_bytes=0,
    )
    base_cfg = {
        "baseUrl": f"http://127.0.0.1:{port}/v1",
        "key": "sk-test",
        "model": "mock-vision",
        "protocol": "openai",
        "tools": True,
        "readContent": True,
        "writeFiles": False,
        "moveFiles": False,
        "vision": True,
        "thinking": False,
        "maxInput": 0,
        "maxOutput": 512,
        "maxRounds": 8,
    }

    print("9. OpenAI：tool 消息后补一条带图的 user 消息")
    events = list(ai.stream_chat(shell, base_cfg, [{"role": "user", "content": "帮我看看 pic.png"}], "你是CDP娘"))
    done_events = [p for k, p in events if k == "tool" and p.get("status") == "done"]
    check("工具事件带 images=1", bool(done_events) and done_events[0].get("images") == 1, str(done_events))
    check("事件里的文本不带 base64", bool(done_events) and "base64" not in (done_events[0].get("result") or ""))
    check("两轮请求", len(STATE["openai"]) == 2, str(len(STATE["openai"])))
    second = STATE["openai"][-1]
    messages = second.get("messages") or []
    tool_msg = next((m for m in messages if m.get("role") == "tool"), {})
    check("tool 消息是纯文本", isinstance(tool_msg.get("content"), str), str(type(tool_msg.get("content"))))
    last = messages[-1] if messages else {}
    check("最后一条是 user", last.get("role") == "user", str(last.get("role")))
    parts = last.get("content") or []
    check("user 消息是多段内容", isinstance(parts, list), str(type(parts)))
    image_parts = [part for part in parts if isinstance(part, dict) and part.get("type") == "image_url"]
    check("带一个 image_url", len(image_parts) == 1, str(len(image_parts)))
    url = (image_parts[0].get("image_url") or {}).get("url") if image_parts else ""
    check("data URL 前缀正确", str(url).startswith("data:image/png;base64,"), str(url)[:40])
    check("图片数据可解码", base64.b64decode(str(url).split(",", 1)[-1]) == pic_raw)
    text = "".join(p["text"] for k, p in events if k == "delta")
    check("模型回答了", "看到了" in text or "看到啦" in text, text)

    print("10. Anthropic：tool_result 直接带 image block")
    cfg_a = dict(base_cfg, protocol="anthropic", baseUrl=f"http://127.0.0.1:{port}")
    events = list(ai.stream_chat(shell, cfg_a, [{"role": "user", "content": "帮我看看 pic.png"}], "你是CDP娘"))
    done_events = [p for k, p in events if k == "tool" and p.get("status") == "done"]
    check("工具事件带 images=1", bool(done_events) and done_events[0].get("images") == 1, str(done_events))
    check("两轮请求", len(STATE["anthropic"]) == 2, str(len(STATE["anthropic"])))
    second = STATE["anthropic"][-1]
    last = (second.get("messages") or [])[-1]
    blocks = last.get("content") or []
    results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
    check("tool_result 存在", len(results) == 1, str([b.get("type") for b in blocks]))
    inner = results[0].get("content") if results else []
    check("tool_result 内容是多段", isinstance(inner, list), str(type(inner)))
    images = [b for b in inner if isinstance(b, dict) and b.get("type") == "image"] if isinstance(inner, list) else []
    check("内容里有一个 image", len(images) == 1, str(len(images)))
    source = images[0].get("source") if images else {}
    check("image 用 base64 source", source.get("type") == "base64", str(source.get("type")))
    check("media_type 正确", source.get("media_type") == "image/png", str(source.get("media_type")))
    check("image 数据可解码", base64.b64decode(source.get("data") or "") == pic_raw)
    text = "".join(p["text"] for k, p in events if k == "delta")
    check("模型回答了", "看到" in text, text)

    server.shutdown()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)
    print()
    if failures:
        print(f"失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
