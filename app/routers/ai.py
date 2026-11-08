"""看板娘 AI 对话接口。

* ``GET/PUT /api/ai/config``    用户自己的接口配置（密钥只回掩码）
* ``POST /api/ai/probe``        测试连接 + 拉模型列表（用页面上填的，不必先保存）
* ``POST /api/ai/chat``         SSE 流式对话（含工具调用 / 图片输入 / 思考模式）
* ``GET/PUT /api/ai/prompt``    系统提示词（仅管理员）
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

from .. import ai_service as ai
from ..db import get_db
from ..deps import get_current_user, require_admin
from ..models import User
from ..schemas import AiChatIn, AiConfigIn, AiProbeIn, AiPromptIn
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
def get_config(db: DbSession, user: CurrentUser, lang: str | None = None) -> dict:
    cfg = ai.public_config(db, user.id)
    is_admin = user.role == 1
    # 语言由前端显式带上（界面语言可能刚切、账号里还没落盘），缺省回落到账号设置
    ui_lang = ai.normalize_language(lang or ai.user_language(db, user))
    return {
        "config": cfg,
        "isAdmin": is_admin,
        "systemPrompt": ai.read_prompt(db) if is_admin else "",
        "defaultPrompt": ai.default_prompt(ui_lang) if is_admin else "",
        "lang": ui_lang,
    }


@router.put("/config")
def update_config(
    payload: AiConfigIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    # 服务层用前端那套 camelCase 键名，这里必须按别名导出
    patch = payload.model_dump(exclude_none=True, by_alias=True)
    cfg = ai.save_config(db, user.id, patch)
    log_action(db, "ai_config_update", user.id, {"model": cfg.get("model")}, request)
    db.commit()
    return {"config": cfg}


@router.post("/probe")
def probe(payload: AiProbeIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
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
    user_id: int, display_name: str, cfg: dict, messages: list[dict], system: str
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
        for event, payload in ai.stream_chat(shell, cfg, messages, system):
            yield _sse(event, payload)
    finally:
        with _cooldown_lock:
            _cooldown[user_id] = time.monotonic()


@router.post("/chat")
def chat(
    payload: AiChatIn, request: Request, db: DbSession, user: CurrentUser
) -> StreamingResponse:
    check_origin(request)
    cfg = ai.load_config(db, user.id)
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
        _stream(snapshot["user_id"], snapshot["display_name"], snapshot["cfg"], messages, system),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # 反向代理（nginx）不加这个会把流攒着一次性吐出来
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/prompt")
def get_prompt(db: DbSession, admin: AdminUser, lang: str | None = None) -> dict:
    ui_lang = ai.normalize_language(lang or ai.user_language(db, admin))
    return {"prompt": ai.read_prompt(db), "defaultPrompt": ai.default_prompt(ui_lang), "lang": ui_lang}


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
