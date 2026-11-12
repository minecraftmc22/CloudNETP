"""看板娘长期记忆：自动提炼 + 自定义条目。

存在 ``user_settings.ai_memories``（JSON 数组，[{text, source, createdAt}]）。
- ``source == "auto"``   由「从对话提炼」生成（自动触发或手动点按钮）
- ``source == "custom"`` 用户在记忆面板里手写的

对话时由 ``ai_service.system_prompt`` 注入提示词；这里只管存取与提炼，
注入与生成时才 import ai_service，避免互相 import 打结。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .user_service import ensure_settings

MAX_MEMORIES = 40
MAX_TEXT = 200
MAX_PER_RUN = 5  # 一次提炼最多新增几条

EXTRACT_PROMPT = {
    "zh-CN": (
        "下面是你（云盘助手）和用户的一段对话记录。请从中提炼出 1-5 条"
        "「值得长期记住的用户信息」：用户的偏好、习惯、常用称呼、明确表达过的"
        "事实或要求。每条一句话、不超过 60 个字，用陈述句。"
        "只输出一个 JSON 字符串数组（例如 [\"……\", \"……\"]），"
        "不要输出任何其它文字、解释或代码块标记。没有值得记的就输出 []。"
    ),
    "en-US": (
        "Below is a conversation between you (a cloud-drive assistant) and the user. "
        "Extract 1-5 pieces of \"user info worth remembering long-term\": preferences, "
        "habits, names they like to be called, facts or requirements they clearly stated. "
        "Each item is one short sentence, max 60 characters. "
        "Output only a JSON array of strings (e.g. [\"…\", \"…\"]) — no prose, no code fences. "
        "Output [] if nothing is worth remembering."
    ),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()[:MAX_TEXT]
        if not text:
            continue
        key = re.sub(r"\s+", "", text).lower()
        if key in seen:
            continue
        seen.add(key)
        source = item.get("source") if item.get("source") in ("auto", "custom") else "custom"
        out.append(
            {
                "text": text,
                "source": source,
                "createdAt": str(item.get("createdAt") or "") or _now_iso(),
            }
        )
        if len(out) >= MAX_MEMORIES:
            break
    return out


def load_memories(db: Session, user_id: int) -> list[dict[str, Any]]:
    row = ensure_settings(db, user_id)
    return _normalize(row.ai_memories)


def save_memories(db: Session, user_id: int, items: list[Any]) -> list[dict[str, Any]]:
    """整体替换（含自动生成的条目也可以在这里删）。超上限时丢弃多出来的。"""
    row = ensure_settings(db, user_id)
    cleaned = _normalize(items)
    row.ai_memories = cleaned
    db.commit()
    return cleaned


def merge_memories(db: Session, user_id: int, texts: list[str]) -> int:
    """把提炼出来的条目并进现有记忆（去重），返回实际新增了几条。"""
    if not texts:
        return 0
    row = ensure_settings(db, user_id)
    current = _normalize(row.ai_memories)
    exists = {re.sub(r"\s+", "", item["text"]).lower() for item in current}
    added = 0
    for raw in texts[:MAX_PER_RUN]:
        text = str(raw or "").strip()[:MAX_TEXT]
        if not text:
            continue
        if re.sub(r"\s+", "", text).lower() in exists:
            continue
        if len(current) >= MAX_MEMORIES:
            break
        exists.add(re.sub(r"\s+", "", text).lower())
        current.append({"text": text, "source": "auto", "createdAt": _now_iso()})
        added += 1
    if added:
        row.ai_memories = current
        db.commit()
    return added


def prompt_lines(db: Session, user_id: int, lang: str | None = None) -> str:
    """拼进系统提示词的记忆段；开关关着或没有内容时返回空串。"""
    from . import ai_service as ai

    try:
        if not ai.load_config(db, user_id).get("memory", True):
            return ""
        items = _normalize(ensure_settings(db, user_id).ai_memories)
    except Exception:  # pragma: no cover - 读记忆失败不该挡住对话
        return ""
    if not items:
        return ""
    lines = "\n".join(f"- {item['text']}" for item in items[:30])
    if (lang or "zh-CN").startswith("en"):
        return f"\n\n[Long-term memory about the user — use it naturally, don't read aloud]\n{lines}"
    return f"\n\n[关于用户的长期记忆，回答时自然运用，不要照念]\n{lines}"


def _transcript(messages: list[dict]) -> str:
    """把消息列表压成提词用的纯文本（图片只记一张占位）。"""
    lines = []
    for message in messages[-20:]:
        role = "用户" if message.get("role") == "user" else "助手"
        content = str(message.get("content") or "").strip()
        if not content and message.get("images"):
            content = "（发了一张图）"
        if content:
            lines.append(f"{role}：{content[:1000]}")
    return "\n".join(lines)


def _parse_items(text: str) -> list[str]:
    """从模型回复里抠出 JSON 数组；容错代码块包裹与非数组输出。"""
    raw = re.sub(r"```(?:json)?", "", text or "").strip()
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()][:MAX_PER_RUN]


def generate_from_messages(
    db: Session, user: Any, cfg: dict, messages: list[dict], lang: str | None = None
) -> tuple[list[dict[str, Any]], int]:
    """让模型从对话里提炼记忆并合并存储。返回 (记忆列表, 新增几条)。"""
    from types import SimpleNamespace

    from . import ai_service as ai

    transcript = _transcript(messages)
    if not transcript:
        return load_memories(db, user.id), 0

    extract_cfg = dict(cfg)
    extract_cfg.update(tools=False, vision=False, thinking=False, maxRounds=1, maxOutput=1000)
    prompt = EXTRACT_PROMPT.get(
        "en-US" if (lang or "").startswith("en") else "zh-CN", EXTRACT_PROMPT["zh-CN"]
    )
    shell = SimpleNamespace(
        id=user.id,
        username=user.username,
        display_name=getattr(user, "display_name", None) or user.username,
        quota_bytes=0,
        used_bytes=0,
    )
    chunks: list[str] = []
    for event, payload in ai.stream_chat(
        shell, extract_cfg, [{"role": "user", "content": prompt + "\n\n" + transcript}], "", False
    ):
        if event == "delta":
            chunks.append(payload.get("text") or "")
        elif event == "error":
            raise ai.AiError(payload.get("message") or "提炼失败")
        elif event == "done":
            break
    added = merge_memories(db, user.id, _parse_items("".join(chunks)))
    return load_memories(db, user.id), added
