"""看板娘 AI 对话接口。

* ``GET/PUT /api/ai/config``    用户自己的接口配置（密钥只回掩码）
* ``POST /api/ai/probe``        测试连接 + 拉模型列表（用页面上填的，不必先保存）
* ``POST /api/ai/chat``         SSE 流式对话（含工具调用 / 图片输入 / 思考模式）
* ``GET/PUT /api/ai/prompt``    系统提示词（仅管理员）
* ``GET/POST /api/ai/sessions`` 历史对话：列表 / 新建（保存当前这段对话）
* ``GET/PUT/DELETE /api/ai/sessions/{id}`` 读取 / 整体替换 / 删除
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Annotated, Iterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import ai_defaults, ai_service as ai, ai_memory, chat_history
from ..db import get_db
from ..deps import get_current_user, require_admin
from ..models import User
from ..schemas import (
    AiChatIn,
    AiConfigIn,
    AiHistoryIn,
    AiMemoriesIn,
    AiProbeIn,
    AiPromptIn,
)
from ..security import check_origin
from ..settings_store import set_value
from ..user_service import log_action

router = APIRouter(prefix="/api/ai", tags=["ai"])

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]

# 同一账号两次对话至少隔 1 秒，防止前端连点把上游额度刷爆
_cooldown: dict[int, float] = {}
_cooldown_lock = threading.Lock()
COOLDOWN_SECONDS = 1.0


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("/config")
def get_config(db: DbSession, user: CurrentUser) -> dict:
    cfg = ai.public_config(db, user.id)
    is_admin = user.role == 1
    # 内置提示词只在服务端拼装，不下发；这里只回管理员自己写过的内容
    return {
        "config": cfg,
        "isAdmin": is_admin,
        "systemPrompt": ai.read_prompt(db) if is_admin else "",
    }


@router.put("/config")
def update_config(
    payload: AiConfigIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    if not ai_defaults.ai_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "管理员已关闭 AI 功能")
    # 服务层用前端那套 camelCase 键名，这里必须按别名导出
    patch = payload.model_dump(exclude_none=True, by_alias=True)
    cfg = ai.save_config(db, user.id, patch)
    log_action(db, "ai_config_update", user.id, {"model": cfg.get("model")}, request)
    db.commit()
    return {"config": cfg}


@router.post("/probe")
def probe(payload: AiProbeIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    if not ai_defaults.ai_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "管理员已关闭 AI 功能")
    saved = ai.load_config(db, user.id)
    cfg = {
        "baseUrl": ai.normalize_base(payload.base_url or saved["baseUrl"]),
        "key": (payload.key or "").strip() or saved["key"],
        "model": saved["model"],
        "protocol": payload.protocol if payload.protocol in ai.PROTOCOLS else "auto",
    }
    if not cfg["baseUrl"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请先填写 API 地址")
    if not cfg["key"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请先填写 API 密钥")
    try:
        result = ai.list_models(cfg)
    except ai.AiError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from None
    result["baseUrl"] = cfg["baseUrl"]
    return result


def _stream(
    user_id: int,
    display_name: str,
    cfg: dict,
    messages: list[dict],
    system: str,
    report_usage: bool = True,
) -> Iterator[str]:
    """把 service 的 (event, payload) 包成 SSE 文本。

    注意：这里的 SQLAlchemy 对象一律不跨线程使用，只传纯数据。
    """
    shell = SimpleNamespace(
        id=user_id,
        username=display_name,
        display_name=display_name,
        quota_bytes=0,
        used_bytes=0,
    )
    try:
        for event, payload in ai.stream_chat(shell, cfg, messages, system, report_usage):
            yield _sse(event, payload)
    finally:
        with _cooldown_lock:
            _cooldown[user_id] = time.monotonic()


@router.post("/chat")
def chat(
    payload: AiChatIn, request: Request, db: DbSession, user: CurrentUser
) -> StreamingResponse:
    check_origin(request)
    if not ai_defaults.ai_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "管理员已关闭 AI 功能")
    cfg, source = ai.effective_config(db, user.id)
    if not cfg["baseUrl"] or not cfg["key"]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "还没配置 AI 接口，去「设置 → 看板娘 → 对话设置」填一下地址和密钥",
        )
    if not cfg["model"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "还没选择模型，先在对话设置里选一个")

    now = time.monotonic()
    with _cooldown_lock:
        last = _cooldown.get(user.id, 0.0)
        if now - last < COOLDOWN_SECONDS:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "说慢一点，CDP娘还在想上一句")
        _cooldown[user.id] = now

    messages = [m.model_dump() for m in payload.messages if (m.content or "").strip() or m.images]
    if not messages:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "消息不能为空")
    if messages[-1].get("role") != "user":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "最后一条必须是你说的话")
    total_chars = sum(len(m.get("content") or "") for m in messages)
    if total_chars > 40000:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "这一轮聊得太长了，开个新对话吧")
    # 图片是 base64 data URL，明文塞进请求体很占内存，这里挡一下
    for message in messages:
        images = message.get("images") or []
        if len(images) > 4 or sum(len(img) for img in images) > 16 * 1024 * 1024:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "图片太多或太大了，一次最多 4 张、合计 16MB")

    system = ai.system_prompt(db, user, payload.lang)
    # 先把纯数据取出来：流式响应期间请求会话的生命周期不好把握，别带 ORM 对象进生成器
    snapshot = {
        "user_id": user.id,
        "display_name": user.display_name or user.username,
        "cfg": {k: v for k, v in cfg.items()},
    }

    return StreamingResponse(
        _stream(
            snapshot["user_id"],
            snapshot["display_name"],
            snapshot["cfg"],
            messages,
            system,
            # 走管理员提供的默认接口时账单不归用户，别去索要、也不下发用量
            report_usage=source != "default",
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # 反向代理（nginx）不加这个会把流攒着一次性吐出来
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/prompt")
def get_prompt(db: DbSession, admin: AdminUser) -> dict:
    # 只回自定义内容；内置提示词留在服务端，不通过接口返回
    return {"prompt": ai.read_prompt(db)}


@router.put("/prompt")
def update_prompt(payload: AiPromptIn, request: Request, db: DbSession, admin: AdminUser) -> dict:
    check_origin(request)
    text = payload.prompt.strip()[:8000]
    # 与内置提示词一模一样就存空串：语义仍是「用默认」，以后切语言还能跟着换
    stored = "" if ai.is_builtin_prompt(text) else text
    set_value(db, "ai_system_prompt", stored)
    log_action(db, "ai_prompt_update", admin.id, {"length": len(stored)}, request)
    db.commit()
    return {"prompt": stored}


# --------------------------------------------------------------------------- #
# 历史对话（保存记忆）：设置里关掉「保存对话记录」后只读不写
# --------------------------------------------------------------------------- #
def _require_history_enabled(db: DbSession, user: CurrentUser) -> None:
    if not ai.load_config(db, user.id).get("saveHistory", True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "设置里已关闭「保存对话记录」")


@router.get("/sessions")
def list_history(db: DbSession, user: CurrentUser) -> dict:
    return {"sessions": chat_history.list_sessions(db, user.id)}


@router.post("/sessions")
def create_history(
    payload: AiHistoryIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    _require_history_enabled(db, user)
    try:
        result = chat_history.create_session(
            db, user.id, payload.model_dump(by_alias=True, exclude_none=True)
        )
    except chat_history.HistoryError as exc:
        raise HTTPException(exc.status, exc.message) from None
    log_action(db, "ai_history_create", user.id, {"id": result["id"]}, request)
    return result


@router.get("/sessions/{session_id}")
def read_history(session_id: int, db: DbSession, user: CurrentUser) -> dict:
    try:
        return chat_history.load_session(db, user.id, session_id)
    except chat_history.HistoryError as exc:
        raise HTTPException(exc.status, exc.message) from None


@router.put("/sessions/{session_id}")
def replace_history(
    session_id: int,
    payload: AiHistoryIn,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    check_origin(request)
    _require_history_enabled(db, user)
    try:
        result = chat_history.save_messages(
            db, user.id, session_id, payload.model_dump(by_alias=True, exclude_none=True)
        )
    except chat_history.HistoryError as exc:
        raise HTTPException(exc.status, exc.message) from None
    return result


@router.delete("/sessions/{session_id}")
def delete_history(
    session_id: int, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    try:
        chat_history.delete_session(db, user.id, session_id)
    except chat_history.HistoryError as exc:
        raise HTTPException(exc.status, exc.message) from None
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 长期记忆
# --------------------------------------------------------------------------- #
@router.get("/memories")
def get_memories(db: DbSession, user: CurrentUser) -> dict:
    return {
        "memories": ai_memory.load_memories(db, user.id),
        "enabled": bool(ai.load_config(db, user.id).get("memory", True)),
    }


@router.put("/memories")
def replace_memories(
    payload: AiMemoriesIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    cleaned = ai_memory.save_memories(db, user.id, [dict(m) for m in payload.memories])
    return {"memories": cleaned}


@router.post("/memories/generate")
def generate_memories(
    payload: AiChatIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    """把最近这段对话交给模型提炼长期记忆，去重合并后返回全量列表。"""
    check_origin(request)
    if not ai_defaults.ai_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "管理员已关闭 AI 功能")
    user_cfg = ai.load_config(db, user.id)
    if not user_cfg.get("memory", True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "设置里已关闭「长期记忆」")
    cfg, source = ai.effective_config(db, user.id)
    if not (cfg["baseUrl"] and cfg["key"] and cfg["model"]):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "还没配置 AI 接口，提炼不了")
    messages = [m.model_dump() for m in payload.messages if (m.content or "").strip()]
    if not messages:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "先聊几句再提炼")
    try:
        memories, added = ai_memory.generate_from_messages(db, user, cfg, messages, payload.lang)
    except ai.AiError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from None
    # 走管理员默认接口时账单不归用户，这次调用也要记一笔（量级很小）
    log_action(
        db,
        "ai_memory_generate",
        user.id,
        {"added": added, "from": "default" if source == "default" else "user"},
        request,
    )
    db.commit()
    return {"memories": memories, "added": added}
