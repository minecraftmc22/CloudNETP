"""看板娘 AI 历史对话的存取。

前端是唯一的「真相来源」：每轮问答结束 / 删消息 / 改消息后，把整段对话
（role + content + images）整体提交上来，服务端按 session 整体替换。
这样编辑、重发、半截回答这些前端才有的状态不用在服务端复刻。

限制：
- 每人最多 ``MAX_SESSIONS`` 段对话，新建超了就删最旧的（按 updated_at）
- 单条消息正文 ≤ 20000 字（与聊天接口一致），单张图 data URL ≤ 2M 字符
- 一次提交的正文总长与图片总长都有上限，防止把 MEDIUMTEXT 撑爆
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .models import AiChatMessage, AiChatSession, utcnow

MAX_SESSIONS = 100
MAX_TOTAL_CONTENT = 200_000  # 一段对话的正文总字数上限
MAX_TOTAL_IMAGES = 24 * 1024 * 1024  # 一段对话的图片 data URL 总字符上限
MAX_IMAGE_CHARS = 2 * 1024 * 1024  # 单张图（base64 之后）的上限


class HistoryError(Exception):
    """给前端看的错误：message 直接展示。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _validate(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验并规整前端提交的消息列表。"""
    total_content = 0
    total_images = 0
    cleaned: list[dict[str, Any]] = []
    for message in messages:
        content = str(message.get("content") or "")
        if len(content) > 20000:
            # 正常请求走 pydantic（max_length=20000），这里兜直接调用服务层的情况
            raise HistoryError("一条消息太长了", 413)
        total_content += len(content)
        if total_content > MAX_TOTAL_CONTENT:
            raise HistoryError("这段对话太长了，开个新对话继续聊吧", 413)
        images = [str(url) for url in (message.get("images") or []) if str(url)]
        if len(images) > 4:
            raise HistoryError("一条消息最多带 4 张图", 413)
        for url in images:
            if not url.startswith("data:image/"):
                raise HistoryError("历史记录里只能存图片附件", 400)
            total_images += len(url)
            if len(url) > MAX_IMAGE_CHARS:
                raise HistoryError("图片太大了，历史记录存不下", 413)
        if total_images > MAX_TOTAL_IMAGES:
            raise HistoryError("这段对话里的图片太多，历史记录存不下", 413)
        cleaned.append({"role": message.get("role") or "user", "content": content, "images": images})
    return cleaned


def derive_title(messages: list[dict[str, Any]]) -> str:
    """标题 = 第一句提问的前 40 个字；没有提问就取第一条消息。"""
    for message in messages:
        if message["role"] == "user" and message["content"].strip():
            return message["content"].strip().replace("\n", " ")[:40]
    first = next((m["content"] for m in messages if m["content"].strip()), "")
    return first.strip().replace("\n", " ")[:40] if first else "新对话"


def list_sessions(db: Session, user_id: int) -> list[dict[str, Any]]:
    rows = (
        db.execute(
            select(
                AiChatSession.id,
                AiChatSession.title,
                AiChatSession.updated_at,
                func.count(AiChatMessage.id).label("count"),
            )
            .outerjoin(AiChatMessage, AiChatMessage.session_id == AiChatSession.id)
            .where(AiChatSession.user_id == user_id)
            .group_by(AiChatSession.id)
            .order_by(AiChatSession.updated_at.desc())
            .limit(200)
        )
        .all()
    )
    return [
        {
            "id": int(row.id),
            "title": row.title or "新对话",
            "updatedAt": row.updated_at.isoformat() + "Z" if row.updated_at else "",
            "count": int(row.count or 0),
        }
        for row in rows
    ]


def create_session(db: Session, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    messages = _validate(payload.get("messages") or [])
    if not messages:
        raise HistoryError("消息不能为空")
    title = str(payload.get("title") or "").strip() or derive_title(messages)

    # 超过上限就把最旧的几段删掉（连同消息，外键级联）
    keep = (
        db.execute(
            select(AiChatSession.id)
            .where(AiChatSession.user_id == user_id)
            .order_by(AiChatSession.updated_at.desc())
            .offset(MAX_SESSIONS - 1)
        )
        .scalars()
        .all()
    )
    if keep:
        db.execute(delete(AiChatSession).where(AiChatSession.id.in_(keep)))

    session = AiChatSession(user_id=user_id, title=title[:200])
    db.add(session)
    db.flush()  # 拿自增 id
    _insert_messages(db, session.id, messages)
    db.commit()
    return {"id": int(session.id), "title": session.title, "count": len(messages)}


def _insert_messages(db: Session, session_id: int, messages: list[dict[str, Any]]) -> None:
    db.execute(
        AiChatMessage.__table__.insert(),
        [
            {
                "session_id": session_id,
                "role": message["role"],
                "content": message["content"],
                "images": json.dumps(message["images"], ensure_ascii=False) if message["images"] else None,
                "created_at": utcnow(),
            }
            for message in messages
        ],
    )


def _owned_session(db: Session, user_id: int, session_id: int) -> AiChatSession:
    session = db.get(AiChatSession, session_id)
    if session is None or session.user_id != user_id:
        # 别人的对话不存在与无权访问返回同一个错误，不给枚举口子
        raise HistoryError("没有这段对话", 404)
    return session


def save_messages(db: Session, user_id: int, session_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """整体替换一段对话的消息；空列表 = 清空（对话本体保留，便于接着聊）。"""
    session = _owned_session(db, user_id, session_id)
    messages = _validate(payload.get("messages") or [])
    db.execute(delete(AiChatMessage).where(AiChatMessage.session_id == session_id))
    if messages:
        _insert_messages(db, session_id, messages)
    patch = {"updated_at": utcnow()}
    title = str(payload.get("title") or "").strip()
    if title:
        patch["title"] = title[:200]
    db.execute(update(AiChatSession).where(AiChatSession.id == session_id).values(**patch))
    db.commit()
    return {"id": session_id, "title": session.title, "count": len(messages)}


def load_session(db: Session, user_id: int, session_id: int) -> dict[str, Any]:
    session = _owned_session(db, user_id, session_id)
    rows = (
        db.execute(
            select(AiChatMessage.role, AiChatMessage.content, AiChatMessage.images)
            .where(AiChatMessage.session_id == session_id)
            .order_by(AiChatMessage.id)
        )
        .all()
    )
    messages = []
    for row in rows:
        try:
            images = json.loads(row.images) if row.images else []
        except (TypeError, ValueError):
            images = []
        messages.append(
            {
                "role": row.role,
                "content": row.content or "",
                "images": images if isinstance(images, list) else [],
            }
        )
    return {
        "id": session_id,
        "title": session.title,
        "updatedAt": session.updated_at.isoformat() + "Z" if session.updated_at else "",
        "messages": messages,
    }


def delete_session(db: Session, user_id: int, session_id: int) -> bool:
    _owned_session(db, user_id, session_id)
    db.delete(db.get(AiChatSession, session_id))
    db.commit()
    return True
