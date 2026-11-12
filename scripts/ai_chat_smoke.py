"""看板娘 AI 对话的离线联调脚本。

不需要真实 API Key：脚本在本机起一个假的 OpenAI / Anthropic 兼容服务，
把 ai_service 的流式解析、工具调用循环、模型探测、写权限与增删改工具全部跑一遍。

    .venv/Scripts/python scripts/ai_chat_smoke.py

（默认用临时目录里的 SQLite，跑完就删；写操作只落在这个临时库里，不碰 dev.db。）
"""

from __future__ import annotations

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

# 必须在 import app.* 之前定好库：写工具会真的写 node 表 / blob
_TMP_DIR = Path(tempfile.mkdtemp(prefix="cloudnetp-ai-smoke-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(_TMP_DIR / 'smoke.db').as_posix()}")
# blob 也落到临时目录，别在真实 storage 里留测试文件
os.environ.setdefault("STORAGE_DIR", str(_TMP_DIR / "storage"))

from app import ai_service as ai  # noqa: E402
from app.db import SessionLocal, engine, ensure_schema  # noqa: E402
from app.models import User  # noqa: E402

ensure_schema(engine)

# 临时库里放一个测试账号，写工具就有地方落
with SessionLocal() as _db:
    _user = User(
        username="smoke",
        password_hash="x",
        display_name="Smoke",
        quota_bytes=64 * 1024 * 1024,
        used_bytes=0,
    )
    _db.add(_user)
    _db.commit()
    USER_ID = int(_user.id)

STATE: dict[str, int] = {"chat_calls": 0, "anthropic_calls": 0, "saw_stream_options": 0}


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

    def _usage(self, prompt: int = 120, completion: int = 45) -> None:
        # 标准 OpenAI 的用量块：choices 是空数组，只有 usage
        self._sse(
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                },
            }
        )

    # ---------------- OpenAI 兼容 ----------------
    def _openai(self, body: dict) -> None:
        STATE["chat_calls"] += 1
        if body.get("model") == "picky-model" and body.get("stream_options"):
            # 模拟「不认 stream_options」的兼容服务：应该被自动降级掉，而不是把对话搞崩
            self._json({"error": {"message": "Unrecognized request argument: stream_options"}}, 400)
            return
        if body.get("stream_options"):
            STATE["saw_stream_options"] += 1
        messages = body.get("messages") or []
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        wants_tools = bool(body.get("tools"))
        loop_mode = any(
            isinstance(m.get("content"), str) and "一直用工具" in m["content"] for m in messages
        )
        self._sse_open()

        # 「一直用工具」模式：有工具就一直要，收走工具才肯说话，用来验证轮次上限
        if loop_mode:
            if wants_tools:
                self._sse({"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": f"loop_{STATE['chat_calls']}", "type": "function",
                     "function": {"name": "storage_usage", "arguments": "{}"}}
                ]}}]})
                self._sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
            else:
                for piece in ["好的", "，先说到这里。"]:
                    self._sse({"choices": [{"index": 0, "delta": {"content": piece}}]})
                self._sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            self._usage()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        if wants_tools and not has_tool_result:
            self._sse({"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                {"index": 0, "id": "call_1", "type": "function", "function": {"name": "list_files", "arguments": ""}}
            ]}}]})
            for piece in ['{"path"', ': ""}']:
                self._sse({"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": piece}}
                ]}}]})
            self._sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
            self._usage()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        self._sse({"choices": [{"index": 0, "delta": {"reasoning_content": "先想想…"}}]})
        for piece in ["你好呀", "，我是CDP娘", "。"]:
            self._sse({"choices": [{"index": 0, "delta": {"content": piece}}]})
        self._sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self._usage()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    # ---------------- Anthropic ----------------
    def _anthropic(self, body: dict) -> None:
        STATE["anthropic_calls"] += 1
        self._sse_open()
        self._sse(
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "role": "assistant",
                    "usage": {"input_tokens": 88, "cache_read_input_tokens": 12, "output_tokens": 0},
                },
            },
            "message_start",
        )
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
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 33},
            },
            "message_delta",
        )
        self._sse({"type": "message_stop"}, "message_stop")


def start_server() -> ThreadingHTTPServer:
    # 端口交给系统分配：本机可能正跑着别的 mock（或上一次的残留），别抢 8899
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def collect(
    cfg: dict, messages: list[dict], system: str, want_usage: bool = True
) -> list[tuple[str, dict]]:
    shell = SimpleNamespace(
        id=0,
        username="smoke",
        display_name="smoke",
        quota_bytes=0,
        used_bytes=0,
    )
    return list(ai.stream_chat(shell, cfg, messages, system, want_usage))


def usage_of(events: list[tuple[str, dict]]) -> dict | None:
    return dict(events[-1][1]).get("usage") if events and events[-1][0] == "done" else None


def run_tool(user_id: int, name: str, args: dict, perms: dict | None = None) -> str:
    """工具结果取文本部分（图片工具会额外带 images，这里只关心文字）。"""
    return ai.run_tool_safe(user_id, name, args, perms).text


def main() -> int:
    server = start_server()
    port = int(server.server_address[1])
    failures: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        if cond:
            print(f"  ok   {name}")
        else:
            print(f"  FAIL {name} {extra}")
            failures.append(name)

    base = f"http://127.0.0.1:{port}/v1"
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
    # 这一轮调了一次工具 = 两次请求，用量按轮累加
    use = usage_of(events)
    check("工具往返后累计用量", use == {"input": 240, "output": 90, "total": 330}, str(use))

    print("3. 关掉工具后不再请求工具")
    before = STATE["chat_calls"]
    cfg_plain = dict(cfg, tools=False)
    events = collect(cfg_plain, [{"role": "user", "content": "在吗"}], "你是CDP娘")
    check("单轮结束", [k for k, _ in events].count("delta") >= 1 and STATE["chat_calls"] == before + 1)
    use = usage_of(events)
    check("单轮用量就是一次请求", use == {"input": 120, "output": 45, "total": 165}, str(use))

    print("3.5 不要用量时不发 stream_options")
    before_opts = STATE["saw_stream_options"]
    events = collect(cfg_plain, [{"role": "user", "content": "在吗"}], "你是CDP娘", want_usage=False)
    check("没带 stream_options", STATE["saw_stream_options"] == before_opts, str(STATE["saw_stream_options"]))
    check("done 里 usage 为空", usage_of(events) is None, str(usage_of(events)))
    check("meta 标明不统计", events[0][1].get("usage") is False, str(events[0][1]))

    print("3.6 服务商不认 stream_options 时自动降级")
    cfg_picky = dict(cfg_plain, model="picky-model")
    events = collect(cfg_picky, [{"role": "user", "content": "在吗"}], "你是CDP娘")
    body = "".join(p["text"] for k, p in events if k == "delta")
    check("降级后照样出正文", "CDP娘" in body, repr(body))
    check("降级后没有用量可显示", usage_of(events) is None, str(usage_of(events)))
    check("降级只发生在本轮，不会报错", not any(k == "error" for k, _ in events), str([k for k, _ in events]))

    print("4. Anthropic 协议")
    cfg_a = dict(cfg, protocol="anthropic", baseUrl=f"http://127.0.0.1:{port}")
    events = collect(cfg_a, [{"role": "user", "content": "在吗"}], "你是CDP娘")
    kinds = [k for k, _ in events]
    check("走的是 /v1/messages", STATE["anthropic_calls"] == 1, str(STATE))
    check("解析出正文", "".join(p["text"] for k, p in events if k == "delta").startswith("在的"), str(events))
    check("解析出思考", any(k == "think" for k in kinds), str(kinds))
    use = usage_of(events)
    check("Anthropic 输入含缓存命中", (use or {}).get("input") == 100, str(use))
    check("Anthropic 输出用量", (use or {}).get("output") == 33, str(use))

    print("5. 历史裁剪")
    long_history = [{"role": "user", "content": "很长" * 2000} for _ in range(5)]
    trimmed = ai.trim_history(long_history, "系统提示", 4000)
    check("按输入上限裁掉旧消息", len(trimmed) < len(long_history), f"{len(trimmed)}/{len(long_history)}")

    print("6. 密钥加解密")
    cipher = ai.encrypt_key("sk-abcdefghijklmn")
    check("密文有前缀", cipher.startswith("enc:"), cipher[:12])
    check("能解回原文", ai.decrypt_key(cipher) == "sk-abcdefghijklmn")
    check("掩码只露头尾", ai.mask_key("sk-abcdefghijklmn") == "sk-ab…klmn", ai.mask_key("sk-abcdefghijklmn"))

    print("7. 配置入参不丢字段")
    # Pydantic 对没声明的字段是静默丢弃的：前端发了、后端没这个字段，
    # 表现就是「点了保存、看着勾上了，刷新又变回去」，所以这里逐键护航。
    from app.schemas import AiConfigIn

    sample = {
        "provider": "openai",
        "baseUrl": "https://api.example.com/v1",
        "model": "gpt-test",
        "tools": True,
        "readContent": True,
        "writeFiles": True,
        "moveFiles": True,
        "quickTransfer": True,
        "vision": True,
        "thinking": True,
        "protocol": "openai",
        "maxInput": 4096,
        "maxOutput": 2048,
        "maxRounds": 12,
        "showTokens": True,
    }
    uncovered = [k for k in ai.DEFAULT_CONFIG if k not in sample and k != "key"]
    check("样例覆盖了全部配置键", not uncovered, f"没覆盖 {uncovered}" if uncovered else "")
    dumped = AiConfigIn.model_validate(sample).model_dump(exclude_none=True, by_alias=True)
    dropped = [k for k in sample if k not in dumped]
    check("配置入参一个字段都不丢", not dropped, f"丢了 {dropped}" if dropped else "")
    check(
        "开关值原样落库",
        all(
            dumped.get(k) is True
            for k in ("tools", "readContent", "writeFiles", "moveFiles", "quickTransfer", "showTokens")
        ),
        str(dumped),
    )

    print("8. 写权限与增删改工具")
    denied = run_tool(
        USER_ID, "create_folder", {"path": "不该出现的目录"}, {"write": False, "move": False}
    )
    check("没授权时拒绝写操作", "没有开启" in denied, denied)

    allowed = {"read": True, "write": True, "move": True}
    made = run_tool(USER_ID, "create_folder", {"path": "AI测试/子目录"}, allowed)
    check("能建多级文件夹", "已创建" in made, made)

    wrote = run_tool(
        USER_ID, "write_file", {"path": "AI测试/子目录/hello.md", "content": "第一版内容"}, allowed
    )
    check("能写文本文件", "新建" in wrote, wrote)
    read_back = run_tool(USER_ID, "read_text", {"path": "AI测试/子目录/hello.md"}, allowed)
    check("写进去的内容读得回来", "第一版内容" in read_back, read_back)

    updated = run_tool(
        USER_ID, "write_file", {"path": "AI测试/子目录/hello.md", "content": "第二版，更长一点点"}, allowed
    )
    check("同名文件是覆盖不是新建", "覆盖" in updated, updated)

    renamed = run_tool(
        USER_ID, "rename_item", {"path": "AI测试/子目录/hello.md", "new_name": "note.md"}, allowed
    )
    check("能改名", "note.md" in renamed, renamed)

    copied = run_tool(USER_ID, "copy_item", {"source": "AI测试", "target_dir": "备份"}, allowed)
    check("能复制目录", "复制到" in copied, copied)
    listing = run_tool(USER_ID, "list_files", {"path": "备份/AI测试/子目录"}, allowed)
    check("副本里能看到改名后的文件", "note.md" in listing, listing)

    moved = run_tool(USER_ID, "move_item", {"source": "备份/AI测试", "target_dir": ""}, allowed)
    check("能移动到根目录", "移动到" in moved, moved)

    dup = run_tool(USER_ID, "create_folder", {"path": "AI测试"}, allowed)
    check("重复建同名文件夹会提示已存在", "已经存在" in dup, dup)

    denied_read = run_tool(USER_ID, "read_text", {"path": "AI测试/子目录/hello.md"}, {"read": False})
    check("没勾「查看文件内容」时拒绝读正文", "查看文件内容" in denied_read, denied_read)

    names = [s["function"]["name"] for s in ai.tool_specs({"tools": True})]
    check("默认不下发读内容工具", "read_text" not in names, names)
    check("默认仍能列目录 / 搜文件", {"list_files", "search_files"} <= set(names), names)
    names = [s["function"]["name"] for s in ai.tool_specs({"tools": True, "readContent": True})]
    check("勾选后才出现读内容工具", "read_text" in names, names)

    specs = ai.tool_specs({"tools": True, "writeFiles": False, "moveFiles": False})
    check("未授权时工具列表只有只读工具", all(s["function"]["name"] not in ai.WRITE_TOOL_NAMES for s in specs))
    specs = ai.tool_specs(
        {"tools": True, "readContent": True, "vision": True, "writeFiles": True, "moveFiles": True}
    )
    check(
        "全部授权后工具数量对得上",
        len(specs)
        == len(ai.TOOL_SPECS)
        + len(ai.READ_TOOL_SPECS)
        + len(ai.WRITE_TOOL_SPECS)
        + len(ai.MOVE_TOOL_SPECS),
        str(len(specs)),
    )
    specs_no_vision = ai.tool_specs(
        {"tools": True, "readContent": True, "writeFiles": True, "moveFiles": True}
    )
    check(
        "没开图片输入就少一个看图工具",
        len(specs_no_vision) == len(specs) - len(ai.VISION_TOOL_NAMES),
        f"{len(specs_no_vision)} vs {len(specs)}",
    )

    print("8. 工具轮次上限：到上限后要收尾而不是断掉")
    before = STATE["chat_calls"]
    events = collect(cfg, [{"role": "user", "content": "一直用工具"}], "你是CDP娘")
    kinds = [k for k, _ in events]
    tool_done = [p for k, p in events if k == "tool" and p.get("status") == "done"]
    check("最多来回 8 轮工具", len(tool_done) == ai.MAX_TOOL_ROUNDS, str(len(tool_done)))
    check("到上限会提示用户", any(k == "notice" for k in kinds), str(kinds[:6]))
    tail_text = "".join(p["text"] for k, p in events if k == "delta")
    check("收尾时仍然有正文", "先说到这里" in tail_text, repr(tail_text))
    check(
        "请求次数 = 轮次 + 收尾",
        STATE["chat_calls"] - before == ai.MAX_TOOL_ROUNDS + 1,
        str(STATE["chat_calls"] - before),
    )
    check("最后一条是 done", kinds[-1] == "done", str(kinds[-3:]))
    ids = [p.get("id") for k, p in events if k == "tool" and p.get("status") == "running"]
    check("每轮工具事件都带唯一 id", len(set(ids)) == len(ids) == ai.MAX_TOOL_ROUNDS, str(ids))

    print("9. 轮次上限可配置")
    events = collect(dict(cfg, maxRounds=2), [{"role": "user", "content": "一直用工具"}], "你是CDP娘")
    rounds_done = [p for k, p in events if k == "tool" and p.get("status") == "done"]
    check("maxRounds=2 只跑两轮", len(rounds_done) == 2, str(len(rounds_done)))
    check(
        "提示里写的是实际轮次",
        any("2 轮" in (p.get("message") or "") for k, p in events if k == "notice"),
        str([p for k, p in events if k == "notice"]),
    )

    with SessionLocal() as db:
        saved = ai.save_config(db, USER_ID, {"maxRounds": 12})
        check("配置能存轮次", saved.get("maxRounds") == 12, str(saved.get("maxRounds")))
        check("能读回轮次", ai.load_config(db, USER_ID)["maxRounds"] == 12)
        ai.save_config(db, USER_ID, {"maxRounds": 99})
        check("超范围会被夹到 50", ai.load_config(db, USER_ID)["maxRounds"] == 50)

    server.shutdown()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)
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
