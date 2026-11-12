"""AI 历史对话 + 长期记忆 + ask_user 问答工具的离线联调脚本。

不碰真实库：在临时目录建 SQLite（写操作只落临时库），起一个假的
OpenAI 兼容服务验证 ask_user 拦截与记忆提炼链路。

    .venv/Scripts/python scripts/ai_history_smoke.py
"""

from __future__ import annotations

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

_TMP_DIR = Path(tempfile.mkdtemp(prefix="cloudnetp-history-smoke-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(_TMP_DIR / 'smoke.db').as_posix()}")
os.environ.setdefault("STORAGE_DIR", str(_TMP_DIR / "storage"))

from fastapi import HTTPException  # noqa: E402

from app import ai_memory, ai_service as ai, chat_history  # noqa: E402
from app.db import SessionLocal, engine, ensure_schema  # noqa: E402
from app.models import User  # noqa: E402
from app.routers import ai as ai_router  # noqa: E402
from app.schemas import AiConfigIn, AiHistoryIn, AiMemoriesIn, AiMemoryItemIn  # noqa: E402

ensure_schema(engine)

with SessionLocal() as _db:
    _u = User(username="smoke", password_hash="x", display_name="Smoke", quota_bytes=1 << 30, used_bytes=0)
    _other = User(username="other", password_hash="x", display_name="Other", quota_bytes=1 << 30, used_bytes=0)
    _db.add_all([_u, _other])
    _db.commit()
    USER_ID, OTHER_ID = int(_u.id), int(_other.id)
    user, other = _u, _other

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def sse(handler, body: bytes) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    handler.end_headers()
    for chunk in body:
        handler.wfile.write(chunk)
        handler.wfile.flush()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # 用例可改写行为
    mode = "ask"

    def log_message(self, *args):
        pass

    def _sse_line(obj: dict) -> bytes:  # 静态工具：整体 dumps，杜绝手拼 JSON 漏转义
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/chat/completions"):
            done = b"data: [DONE]\n\n"
            if Handler.mode == "ask":
                args = json.dumps(
                    {"question": "要整理哪个文件夹？", "options": ["全部整理", "只整理照片"]},
                    ensure_ascii=False,
                )
                body = [
                    Handler._sse_line({"choices": [{"delta": {"tool_calls": [
                        {"index": 0, "function": {"name": "ask_user", "arguments": args}}
                    ]}}]}),
                    done,
                ]
            elif Handler.mode == "ask_empty":
                args = json.dumps({"question": ""}, ensure_ascii=False)
                body = [
                    Handler._sse_line({"choices": [{"delta": {"tool_calls": [
                        {"index": 0, "function": {"name": "ask_user", "arguments": args}}
                    ]}}]}),
                    done,
                ]
            elif Handler.mode == "memory":
                text = json.dumps(["用户喜欢按「日期_主题」命名文件", "说话别太客套"], ensure_ascii=False)
                body = [
                    Handler._sse_line({"choices": [{"delta": {"content": f"```json\n{text}\n```"}}]}),
                    done,
                ]
            else:
                body = [
                    Handler._sse_line({"choices": [{"delta": {"content": "好，这就开始。"}}]}),
                    done,
                ]
            sse(self, body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}/v1"

fake_request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))  # check_origin 无 Origin 直接放行；log_action 要读 client
CFG_PATCH = {
    "baseUrl": BASE,
    "key": "sk-smoke",
    "model": "gpt-smoke",
    "provider": "custom",
    "tools": True,
}

with SessionLocal() as db:
    db_user = db.get(User, USER_ID)
    db_other = db.get(User, OTHER_ID)
    ai.save_config(db, USER_ID, dict(CFG_PATCH))

    # ---------------- 历史对话：服务层 ----------------
    print("[1] chat_history 服务层")
    made = chat_history.create_session(db, USER_ID, {
        "messages": [
            {"role": "user", "content": "帮我看看文件夹乱不乱"},
            {"role": "assistant", "content": "还好，照片目录有点乱。"},
        ]
    })
    check("创建会话返回 id", made["id"] > 0)
    check("标题取自第一句提问", made["title"] == "帮我看看文件夹乱不乱", made["title"])

    rows = chat_history.list_sessions(db, USER_ID)
    check("列表有 1 条", len(rows) == 1 and rows[0]["count"] == 2, str(rows))

    data = chat_history.load_session(db, USER_ID, made["id"])
    check("读取消息数与角色", len(data["messages"]) == 2 and data["messages"][1]["role"] == "assistant")

    saved = chat_history.save_messages(db, USER_ID, made["id"], {
        "messages": [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "第二问"},
            {"role": "assistant", "content": "第二答", "images": ["data:image/jpeg;base64,QUJD"]},
        ]
    })
    check("整体替换后 count=4", saved["count"] == 4)
    data = chat_history.load_session(db, USER_ID, made["id"])
    check("图片 data URL 原样取回", data["messages"][3]["images"] == ["data:image/jpeg;base64,QUJD"])

    saved = chat_history.save_messages(db, USER_ID, made["id"], {"messages": []})
    check("清空消息保留会话", saved["count"] == 0)

    try:
        chat_history.load_session(db, USER_ID, 99999)
        check("不存在的会话 404", False)
    except chat_history.HistoryError as exc:
        check("不存在的会话 404", exc.status == 404)
    try:
        chat_history.load_session(db, OTHER_ID, made["id"])
        check("别人的会话 404", False)
    except chat_history.HistoryError as exc:
        check("别人的会话 404", exc.status == 404)

    try:
        chat_history.create_session(db, USER_ID, {"messages": [
            {"role": "user", "content": "x" * 20001}]})
        check("超长内容 413", False)
    except chat_history.HistoryError as exc:
        check("超长内容 413", exc.status == 413)
    try:
        chat_history.create_session(db, USER_ID, {"messages": [
            {"role": "user", "content": "x", "images": ["a", "b", "c", "d", "e"]}]})
        check("图片超 4 张 413", False)
    except chat_history.HistoryError as exc:
        check("图片超 4 张 413", exc.status == 413)
    try:
        chat_history.create_session(db, USER_ID, {"messages": [
            {"role": "user", "content": "x", "images": ["https://evil.example/a.png"]}]})
        check("非 data:image 附件 400", False)
    except chat_history.HistoryError as exc:
        check("非 data:image 附件 400", exc.status == 400)

    # 上限裁剪
    old_max = chat_history.MAX_SESSIONS
    chat_history.MAX_SESSIONS = 3
    for i in range(4):
        chat_history.create_session(db, USER_ID, {
            "title": f"s{i}",
            "messages": [{"role": "user", "content": f"问题 {i}"}],
        })
    rows = chat_history.list_sessions(db, USER_ID)
    check("超上限删最旧（剩 3 条）", len(rows) == 3, str([r["title"] for r in rows]))
    check("最旧的确实没了", all(r["title"] != "s0" for r in rows))
    chat_history.MAX_SESSIONS = old_max

    check("删除会话", chat_history.delete_session(db, USER_ID, rows[0]["id"]))
    check("删除后列表不含", all(r["id"] != rows[0]["id"] for r in chat_history.list_sessions(db, USER_ID)))

    # ---------------- 历史对话：路由层 ----------------
    print("[2] 历史对话路由（含 saveHistory 门禁）")
    payload = AiHistoryIn.model_validate({
        "messages": [
            {"role": "user", "content": "路由层的第一句提问，看看能不能自动出标题"},
            {"role": "assistant", "content": "能。"},
        ]
    })
    result = ai_router.create_history(payload, fake_request, db, db_user)
    rid = result["id"]
    check("路由创建会话", rid > 0 and "count" in result)
    result = ai_router.replace_history(
        rid, AiHistoryIn.model_validate({"messages": [{"role": "user", "content": "更新"}]}),
        fake_request, db, db_user,
    )
    check("路由整体替换", result["count"] == 1)

    ai.save_config(db, USER_ID, {"saveHistory": False})
    try:
        ai_router.create_history(payload, fake_request, db, db_user)
        check("saveHistory 关闭后创建 403", False)
    except HTTPException as exc:
        check("saveHistory 关闭后创建 403", exc.status_code == 403)
    # 只读不受开关影响
    check("关闭后仍可读取", len(ai_router.read_history(rid, db, db_user)["messages"]) == 1)
    check("关闭后仍可列表", len(ai_router.list_history(db, db_user)["sessions"]) >= 1)
    ai.save_config(db, USER_ID, {"saveHistory": True})

    # ---------------- 配置四件套 ----------------
    print("[3] saveHistory / memory 配置往返")
    cfg = ai.load_config(db, USER_ID)
    check("saveHistory 默认开", cfg["saveHistory"] is True)
    check("memory 默认开", cfg["memory"] is True)
    pub = ai.public_config(db, USER_ID)
    check("public_config 下发两键", "saveHistory" in pub and "memory" in pub)
    inbound = AiConfigIn.model_validate({"saveHistory": False, "memory": False})
    patch = inbound.model_dump(exclude_none=True, by_alias=True)
    ai.save_config(db, USER_ID, patch)
    cfg = ai.load_config(db, USER_ID)
    check("AiConfigIn 驼峰往返保存", cfg["saveHistory"] is False and cfg["memory"] is False)
    ai.save_config(db, USER_ID, {"saveHistory": True, "memory": True})

    # ---------------- 长期记忆 ----------------
    print("[4] 长期记忆：存取 / 去重 / 提示词注入")
    check("初始无记忆", ai_memory.load_memories(db, USER_ID) == [])
    cleaned = ai_memory.save_memories(db, USER_ID, [
        {"text": "  文件按日期命名  ", "source": "custom"},
        {"text": "文件按日期命名", "source": "custom"},  # 去空白后重复
        {"text": "", "source": "custom"},
    ])
    check("保存去重 + 空条目丢弃", len(cleaned) == 1 and cleaned[0]["text"] == "文件按日期命名")

    added = ai_memory.merge_memories(db, USER_ID, ["文件按日期命名", "喜欢粤语歌"])
    check("合并去重只加新的", added == 1)
    added = ai_memory.merge_memories(db, USER_ID, ["文件按日期命名"])
    check("重复条目不再计入", added == 0)
    mem = ai_memory.load_memories(db, USER_ID)
    check("自动条目 source=auto", any(m["source"] == "auto" for m in mem))

    prompt = ai.system_prompt(db, db_user, "zh-CN")
    check("系统提示词注入记忆", "关于用户的长期记忆" in prompt and "文件按日期命名" in prompt)
    ai.save_config(db, USER_ID, {"memory": False})
    prompt = ai.system_prompt(db, db_user, "zh-CN")
    check("关掉开关不注入", "关于用户的长期记忆" not in prompt)
    ai.save_config(db, USER_ID, {"memory": True})

    check("JSON 数组解析（带围栏）", ai_memory._parse_items("```json\n[\"a\",\"b\"]\n```") == ["a", "b"])
    check("空数组解析", ai_memory._parse_items("[]") == [])
    check("垃圾输出解析为空", ai_memory._parse_items("我想想……没有") == [])

    cfg, source = ai.effective_config(db, USER_ID)
    Handler.mode = "memory"
    memories, n = ai_memory.generate_from_messages(db, db_user, cfg, [
        {"role": "user", "content": "帮我把照片都按日期命名一下"},
        {"role": "assistant", "content": "好的，都改好了。"},
    ], "zh-CN")
    check("提炼新增 2 条", n == 2, f"n={n}")
    check("提炼入库 source=auto", any(m["text"] == "用户喜欢按「日期_主题」命名文件" for m in memories))
    memories, n = ai_memory.generate_from_messages(db, db_user, cfg, [
        {"role": "user", "content": "帮我把照片都按日期命名一下"},
        {"role": "assistant", "content": "好的，都改好了。"},
    ], "zh-CN")
    check("二次提炼全去重", n == 0, f"n={n}")

    print("[5] 长期记忆路由")
    result = ai_router.get_memories(db, db_user)
    check("GET memories", isinstance(result["memories"], list) and result["enabled"] is True)
    result = ai_router.replace_memories(
        AiMemoriesIn.model_validate({
            "memories": [{"text": "路由写入的记忆", "source": "custom"}]
        }),
        fake_request, db, db_user,
    )
    check("PUT memories 整体替换", [m["text"] for m in result["memories"]] == ["路由写入的记忆"])
    try:
        ai_router.generate_memories(
            SimpleNamespace(messages=[], lang=None),
            fake_request, db, db_user,
        )
        check("空消息提炼 400", False)
    except HTTPException as exc:
        check("空消息提炼 400", exc.status_code == 400)

    ai.save_config(db, USER_ID, {"memory": False})
    try:
        ai_router.generate_memories(
            SimpleNamespace(messages=[SimpleNamespace(role="user", content="x", images=[])],
                            lang=None),
            fake_request, db, db_user,
        )
        check("memory 关闭后提炼 403", False)
    except HTTPException as exc:
        check("memory 关闭后提炼 403", exc.status_code == 403)
    ai.save_config(db, USER_ID, {"memory": True})

    # ---------------- ask_user 问答工具 ----------------
    print("[6] ask_user 问答工具")
    Handler.mode = "ask"
    cfg = ai.load_config(db, USER_ID)
    specs = ai.tool_specs(cfg)
    names = {s["function"]["name"] for s in specs}
    check("tools 开着时下发 ask_user", "ask_user" in names)
    check("tools 关着时不下发", "ask_user" not in {s["function"]["name"] for s in ai.tool_specs({**cfg, "tools": False})})

    events = list(ai.stream_chat(db_user, cfg, [{"role": "user", "content": "帮我整理一下"}], "sys", True))
    kinds = [e for e, _ in events]
    asks = [p for e, p in events if e == "ask"]
    check("发出 ask 事件", len(asks) == 1, str(events[:2]))
    check("ask 带问题与选项", asks and asks[0]["question"] == "要整理哪个文件夹？" and asks[0]["options"] == ["全部整理", "只整理照片"], str(asks))
    check("ask 之后本轮以 done 结束", "done" in kinds and kinds.index("ask") < kinds.index("done"))

    Handler.mode = "ask_empty"
    events = list(ai.stream_chat(db_user, cfg, [{"role": "user", "content": "整理"}], "sys", True))
    tool_results = [p for e, p in events if e == "tool" and p.get("status") == "done"]
    check("空问题的 ask 走普通工具报错", tool_results and "没有这个工具" in tool_results[0]["result"], str(tool_results))
    check("报错后对话继续到 done", events[-1][0] == "done")

    Handler.mode = "text"
    events = list(ai.stream_chat(db_user, cfg, [
        {"role": "user", "content": "帮我整理一下"},
        {"role": "assistant", "content": "要整理哪个文件夹？"},
        {"role": "user", "content": "全部整理"},
    ], "sys", True))
    text = "".join(p.get("text", "") for e, p in events if e == "delta")
    check("用户点选后正常续聊", text == "好，这就开始。", text)

    server.shutdown()

print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
