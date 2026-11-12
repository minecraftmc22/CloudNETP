# -*- coding: utf-8 -*-
"""工具调用消息顺序冒烟：防止上游 400「insufficient tool messages following tool_calls」。

复现的是这个真实故障：一轮里模型同时调了多个工具，其中一个（read_image）返回图片，
旧代码会在每条 tool 回复之间插一条 user 图片消息，把「assistant(tool_calls) → 连续
N 条 tool 消息」打断，OpenAI 兼容接口直接 400。

假服务按上游规则严格校验每次请求，并模拟 400 报错文本。

    ./.venv/Scripts/python.exe scripts/ai_tool_order_smoke.py
"""

from __future__ import annotations

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

_TMP = Path(tempfile.mkdtemp(prefix="cloudnetp-tool-order-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(_TMP / 'smoke.db').as_posix()}")
os.environ.setdefault("STORAGE_DIR", str(_TMP / "storage"))

from PIL import Image  # noqa: E402

from app import ai_service as ai, node_service  # noqa: E402
from app.db import SessionLocal, engine, ensure_schema  # noqa: E402
from app.models import User  # noqa: E402

ensure_schema(engine)
with SessionLocal() as _db:
    _user = User(username="order", password_hash="x", display_name="Order", quota_bytes=1 << 30)
    _db.add(_user)
    _db.commit()
    USER_ID = int(_user.id)

# 造一张能被 read_image 读出来的小图
buf = io.BytesIO()
Image.new("RGBA", (8, 8), (220, 40, 60, 255)).save(buf, "PNG")
with SessionLocal() as db:
    sha, size = ai._store_bytes(buf.getvalue())
    node_service.create_file(db, db.get(User, USER_ID), None, "pic.png", sha, size, "image/png")
    node_service.recalc_usage(db, db.get(User, USER_ID))
    db.commit()

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def pairs_ok(messages: list[dict]) -> bool:
    """上游规则：assistant(tool_calls) 后面必须紧跟同等数量、id 一一对应的 tool 消息。"""
    i = 0
    while i < len(messages):
        msg = messages[i]
        calls = msg.get("tool_calls") if msg.get("role") == "assistant" else None
        if not calls:
            i += 1
            continue
        ids = [c.get("id") for c in calls if isinstance(c, dict) and c.get("id")]
        following = messages[i + 1 : i + 1 + len(ids)]
        if len(following) != len(ids):
            return False
        if any(f.get("role") != "tool" for f in following):
            return False
        if {f.get("tool_call_id") for f in following} != set(ids):
            return False
        i += 1 + len(ids)
    return True


BAD_REQUEST = (
    "An assistant message with 'tool_calls' must be followed by tool messages "
    "responding to each 'tool_call_id'. (insufficient tool messages following "
    "tool_calls message)"
)

STATE: dict = {"requests": [], "mode": "image+many", "bad": 0}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, obj: dict) -> None:
        self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        STATE["requests"].append(body)
        messages = body.get("messages") or []
        if not pairs_ok(messages):
            STATE["bad"] += 1
            self._send({"error": {"message": BAD_REQUEST}}, 400)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        first_request = len(STATE["requests"]) == 1
        if STATE["mode"] == "image+many" and first_request:
            # 一轮里同时调「读图片」与「查容量」：图片消息不能插进 tool 回复中间
            self._sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_img", "function": {"name": "read_image",
                                                             "arguments": '{"path":"pic.png"}'}},
                {"index": 1, "id": "call_quota", "function": {"name": "storage_usage",
                                                               "arguments": "{}"}},
            ]}}]})
        elif STATE["mode"] == "ask" and first_request:
            # 问答工具与别的工具同轮：应该直接结束本轮，不发第二次请求
            self._sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_quota", "function": {"name": "storage_usage",
                                                               "arguments": "{}"}},
                {"index": 1, "id": "call_ask", "function": {"name": "ask_user",
                                                             "arguments": json.dumps(
                                                                 {"question": "要整理哪个文件夹？",
                                                                  "options": ["全部整理", "只整理照片"]},
                                                                 ensure_ascii=False)}},
            ]}}]})
        else:
            self._sse({"choices": [{"delta": {"content": "好，处理完了。"}}]})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}/v1"

with SessionLocal() as db:
    ai.save_config(db, USER_ID, {
        "baseUrl": BASE, "key": "sk-smoke", "model": "mock", "provider": "custom",
        "tools": True, "readContent": True, "vision": True, "maxRounds": 3,
    })
    cfg = ai.load_config(db, USER_ID)
    user = db.get(User, USER_ID)
    shell = SimpleNamespace(id=USER_ID, username="order", display_name="Order",
                            quota_bytes=0, used_bytes=0)

    print("[1] 同一轮多工具 + 图片：请求顺序必须合法")
    STATE.update(requests=[], mode="image+many", bad=0)
    events = list(ai.stream_chat(shell, cfg, [{"role": "user", "content": "看看这张图还剩多少空间"}], "sys", False))
    check("上游没有判 400", STATE["bad"] == 0, f"bad={STATE['bad']}")
    check("没有 error 事件", not any(e == "error" for e, _ in events), str(events[:3]))
    check("收到最终文字", "好，处理完了。" in "".join(p.get("text", "") for e, p in events if e == "delta"))
    check("确实发了两次请求（一次工具往返）", len(STATE["requests"]) == 2, str(len(STATE["requests"])))

    second = STATE["requests"][1]["messages"] if len(STATE["requests"]) > 1 else []
    roles = [m.get("role") for m in second]
    check("第二次请求配对合法", pairs_ok(second), str(roles))
    check("两条 tool 回复紧跟 assistant", roles[-4:-1] == ["assistant", "tool", "tool"], str(roles))
    check("图片 user 消息排在所有 tool 回复之后", roles[-1] == "user", str(roles))
    image_msg = second[-1] if second and second[-1].get("role") == "user" else {}
    parts = image_msg.get("content") if isinstance(image_msg.get("content"), list) else []
    check("图片仍然递给了模型", any(p.get("type") == "image_url" for p in parts), str(parts)[:100])

    print("[2] ask_user 与普通工具同轮：直接结束，不留半截工具调用")
    STATE.update(requests=[], mode="ask", bad=0)
    events = list(ai.stream_chat(shell, cfg, [{"role": "user", "content": "帮我整理"}], "sys", False))
    asks = [p for e, p in events if e == "ask"]
    check("发出 ask 事件", len(asks) == 1 and asks[0]["question"] == "要整理哪个文件夹？", str(asks))
    check("本轮只发一次请求", len(STATE["requests"]) == 1, str(len(STATE["requests"])))
    check("没有 error 事件", not any(e == "error" for e, _ in events))
    check("以 done 收尾", events[-1][0] == "done", str(events[-1]))

    print("[3] 兜底修复：历史里混入不配对片段也能自愈")
    broken = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "y", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": "结果 A"},
        {"role": "user", "content": "（图片）"},
        {"role": "tool", "tool_call_id": "b", "content": "结果 B"},
    ]
    check("修复前确实不合法", not pairs_ok(broken))
    ai._repair_tool_pairs(broken)
    check("修复后合法", pairs_ok(broken), str([m.get("role") for m in broken]))
    check("图片 user 消息没有丢", any(m.get("role") == "user" and m.get("content") == "（图片）" for m in broken))
    good = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "结果"},
        {"role": "user", "content": "谢谢"},
    ]
    snapshot = json.dumps(good, ensure_ascii=False)
    ai._repair_tool_pairs(good)
    check("正常序列不会被改动", json.dumps(good, ensure_ascii=False) == snapshot)

server.shutdown()
print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
