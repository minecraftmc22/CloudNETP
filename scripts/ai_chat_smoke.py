"""看板娘 AI 对话的离线联调脚本。

不需要真实 API Key：脚本在本机起一个假的 OpenAI / Anthropic 兼容服务，
把 ai_service 的流式解析、工具调用循环、模型探测全部跑一遍。

    DATABASE_URL=sqlite:///./dev.db .venv/Scripts/python scripts/ai_chat_smoke.py

（用 SQLite 是因为脚本会执行工具，需要有库；本地 MySQL 没起也能跑。）
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///./dev.db")

from app import ai_service as ai  # noqa: E402
from app.db import engine, ensure_schema  # noqa: E402

# dev.db 可能是旧版本建的：先补齐缺的列，工具（列目录/搜索）才不会因为缺列报错
ensure_schema(engine)

PORT = 8899
STATE: dict[str, int] = {"chat_calls": 0, "anthropic_calls": 0}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    # ---------------- 基础 ----------------
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

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    # ---------------- 路由 ----------------
    def do_GET(self):
        if self.path.endswith("/models"):
            self._json({"object": "list", "data": [{"id": "mock-large"}, {"id": "mock-small"}]})
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
        STATE["chat_calls"] += 1
        has_tool_result = any(m.get("role") == "tool" for m in body.get("messages") or [])
        wants_tools = bool(body.get("tools"))
        self._sse_open()

        if wants_tools and not has_tool_result:
            self._sse({"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                {"index": 0, "id": "call_1", "type": "function", "function": {"name": "list_files", "arguments": ""}}
            ]}}]})
            for piece in ['{"path"', ': ""}']:
                self._sse({"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": piece}}
                ]}}]})
            self._sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        self._sse({"choices": [{"index": 0, "delta": {"reasoning_content": "先想想…"}}]})
        for piece in ["你好呀", "，我是CDP娘", "。"]:
            self._sse({"choices": [{"index": 0, "delta": {"content": piece}}]})
        self._sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    # ---------------- Anthropic ----------------
    def _anthropic(self, body: dict) -> None:
        STATE["anthropic_calls"] += 1
        self._sse_open()
        self._sse({"type": "message_start", "message": {"id": "msg_1", "role": "assistant"}}, "message_start")
        self._sse(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            "content_block_start",
        )
        self._sse(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "想一下"}},
            "content_block_delta",
        )
        self._sse(
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            "content_block_start",
        )
        for piece in ["在的", "，需要帮忙吗？"]:
            self._sse(
                {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": piece}},
                "content_block_delta",
            )
        self._sse(
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}},
            "message_delta",
        )
        self._sse({"type": "message_stop"}, "message_stop")


def start_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def collect(cfg: dict, messages: list[dict], system: str) -> list[tuple[str, dict]]:
    shell = SimpleNamespace(
        id=0,
        username="smoke",
        display_name="smoke",
        quota_bytes=0,
        used_bytes=0,
    )
    return list(ai.stream_chat(shell, cfg, messages, system))


def main() -> int:
    server = start_server()
    failures: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        if cond:
            print(f"  ok   {name}")
        else:
            print(f"  FAIL {name} {extra}")
            failures.append(name)

    base = f"http://127.0.0.1:{PORT}/v1"
    print("1. 模型探测")
    probe = ai.list_models({"baseUrl": base, "key": "sk-test", "model": "mock-large", "protocol": "openai"})
    check("列出 2 个模型", probe["models"] == ["mock-large", "mock-small"], str(probe))

    print("2. OpenAI 协议：工具调用 + 正文 + 思考")
    cfg = {
        "baseUrl": base,
        "key": "sk-test",
        "model": "mock-large",
        "protocol": "openai",
        "tools": True,
        "vision": False,
        "thinking": False,
        "maxInput": 0,
        "maxOutput": 512,
    }
    events = collect(cfg, [{"role": "user", "content": "帮我看看网盘里有什么"}], "你是CDP娘")
    kinds = [e[0] for e in events]
    check("有 meta", kinds[0] == "meta", str(kinds[:3]))
    check("触发了工具", "tool" in kinds, str(kinds))
    check("工具先 running 后 done", _tool_statuses(events) == ["running", "done"], str(_tool_statuses(events)))
    text = "".join(p["text"] for k, p in events if k == "delta")
    check("拿到流式正文", "CDP娘" in text, repr(text))
    check("有思考内容", any(k == "think" for k in kinds))
    check("有 done", kinds[-1] == "done", str(kinds[-2:]))

    print("3. 关掉工具后不再请求工具")
    before = STATE["chat_calls"]
    cfg_plain = dict(cfg, tools=False)
    events = collect(cfg_plain, [{"role": "user", "content": "在吗"}], "你是CDP娘")
    check("单轮结束", [k for k, _ in events].count("delta") >= 1 and STATE["chat_calls"] == before + 1)

    print("4. Anthropic 协议")
    cfg_a = dict(cfg, protocol="anthropic", baseUrl=f"http://127.0.0.1:{PORT}")
    events = collect(cfg_a, [{"role": "user", "content": "在吗"}], "你是CDP娘")
    kinds = [k for k, _ in events]
    check("走的是 /v1/messages", STATE["anthropic_calls"] == 1, str(STATE))
    check("解析出正文", "".join(p["text"] for k, p in events if k == "delta").startswith("在的"), str(events))
    check("解析出思考", any(k == "think" for k in kinds), str(kinds))

    print("5. 历史裁剪")
    long_history = [{"role": "user", "content": "很长" * 2000} for _ in range(5)]
    trimmed = ai.trim_history(long_history, "系统提示", 4000)
    check("按输入上限裁掉旧消息", len(trimmed) < len(long_history), f"{len(trimmed)}/{len(long_history)}")

    print("6. 密钥加解密")
    cipher = ai.encrypt_key("sk-abcdefghijklmn")
    check("密文有前缀", cipher.startswith("enc:"), cipher[:12])
    check("能解回原文", ai.decrypt_key(cipher) == "sk-abcdefghijklmn")
    check("掩码只露头尾", ai.mask_key("sk-abcdefghijklmn") == "sk-ab…klmn", ai.mask_key("sk-abcdefghijklmn"))

    server.shutdown()
    print()
    if failures:
        print(f"失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("全部通过")
    return 0


def _tool_statuses(events: list[tuple[str, dict]]) -> list[str]:
    return [p.get("status") for k, p in events if k == "tool"]


if __name__ == "__main__":
    raise SystemExit(main())
