"""站内聊天接口。

权限模型：所有接口都先过一次「是不是这个会话的成员」（``require_member``），
消息里的文件下载再多一道「这条消息是本人发的」的校验，防止拿到别人的 node id
顺着 /api/chat/messages/<id>/files/0 把私人文件拖走。
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from .. import chat_service as cs
from ..db import get_db
from ..deps import get_current_user
from ..models import ChatMessage, User
from ..schemas import (
    ChatDirectIn,
    ChatForwardIn,
    ChatGroupIn,
    ChatGroupPatch,
    ChatMembersIn,
    ChatReadIn,
    ChatReportIn,
    ChatSendIn,
    StickerIn,
)
from ..security import check_origin
from ..user_service import log_action
from .. import storage

router = APIRouter(prefix="/api/chat", tags=["chat"])

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
@router.get("/conversations")
def list_conversations(db: DbSession, user: CurrentUser) -> dict:
    return cs.list_conversations(db, user)


@router.post("/conversations/direct", status_code=status.HTTP_201_CREATED)
def open_direct(payload: ChatDirectIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    conv = cs.get_or_create_direct(db, user, payload.user_id)
    db.commit()
    data = cs.list_conversations(db, user)
    hit = next((item for item in data["items"] if item["id"] == conv.id), None)
    return {"conversation": hit, "items": data["items"]}


@router.post("/conversations/group", status_code=status.HTTP_201_CREATED)
def create_group(payload: ChatGroupIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    conv = cs.create_group(db, user, payload.title, payload.member_ids)
    db.commit()
    log_action(db, "chat_group_create", user.id, {"id": conv.id, "n": len(payload.member_ids)}, request)
    db.commit()
    data = cs.list_conversations(db, user)
    hit = next((item for item in data["items"] if item["id"] == conv.id), None)
    return {"conversation": hit}


@router.get("/conversations/{conversation_id}")
def conversation_detail(conversation_id: int, db: DbSession, user: CurrentUser) -> dict:
    conv, _ = cs.require_member(db, conversation_id, user)
    data = cs.list_conversations(db, user)
    hit = next((item for item in data["items"] if item["id"] == conv.id), None)
    return {"conversation": hit}


@router.patch("/conversations/{conversation_id}")
def patch_conversation(
    conversation_id: int, payload: ChatGroupPatch, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    cs.rename_group(db, user, conversation_id, payload.title)
    db.commit()
    return conversation_detail(conversation_id, db, user)


@router.post("/conversations/{conversation_id}/members")
def invite_members(
    conversation_id: int, payload: ChatMembersIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    cs.add_members(db, user, conversation_id, payload.user_ids)
    db.commit()
    return conversation_detail(conversation_id, db, user)


@router.delete("/conversations/{conversation_id}/members/{member_id}")
def drop_member(
    conversation_id: int, member_id: int, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    result = cs.remove_member(db, user, conversation_id, member_id)
    db.commit()
    return result


@router.post("/conversations/{conversation_id}/leave")
def leave_conversation(
    conversation_id: int, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    result = cs.leave_group(db, user, conversation_id)
    db.commit()
    return result


# --------------------------------------------------------------------------- #
# 消息
# --------------------------------------------------------------------------- #
@router.get("/conversations/{conversation_id}/messages")
def list_messages(
    conversation_id: int,
    db: DbSession,
    user: CurrentUser,
    before: int | None = Query(default=None, ge=0),
    after: int | None = Query(default=None, ge=0),
    limit: int = Query(default=cs.HISTORY_LIMIT, ge=1, le=cs.HISTORY_MAX),
) -> dict:
    return cs.list_messages(
        db, user, conversation_id, before_id=before, after_id=after, limit=limit
    )


@router.post("/conversations/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
def send_message(
    conversation_id: int, payload: ChatSendIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    msg = cs.send_message(
        db,
        user,
        conversation_id,
        kind=payload.kind,
        body=payload.body,
        payload=payload.payload,
    )
    db.commit()
    return {"message": cs.message_payload(db, msg)}


@router.post("/conversations/{conversation_id}/read")
def read_conversation(
    conversation_id: int, payload: ChatReadIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    result = cs.mark_read(db, user, conversation_id, payload.message_id)
    db.commit()
    return result


@router.get("/unread")
def unread(user: CurrentUser, db: DbSession) -> dict:
    """导航角标用：所有会话的未读合计。"""
    return {"unread": cs.total_unread(db, user)}


@router.post("/messages/{message_id}/recall")
def recall_message(message_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    msg = cs.recall(db, user, message_id)
    db.commit()
    return {"message": cs.message_payload(db, msg)}


@router.post("/messages/{message_id}/forward", status_code=status.HTTP_201_CREATED)
def forward_message(
    message_id: int, payload: ChatForwardIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    result = cs.forward(db, user, message_id, payload.conversation_ids)
    db.commit()
    return result


@router.post("/messages/{message_id}/save")
def save_files(message_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    result = cs.save_files(db, user, message_id)
    db.commit()
    log_action(db, "chat_files_save", user.id, result, request)
    db.commit()
    return result


@router.post("/messages/{message_id}/report", status_code=status.HTTP_201_CREATED)
def report_message(
    message_id: int, payload: ChatReportIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    row = cs.report(db, user, message_id, payload.reason)
    db.commit()
    log_action(db, "chat_report", user.id, {"message": message_id, "target": row.target_id}, request)
    db.commit()
    return {"ok": True, "id": row.id}


@router.get("/messages/{message_id}/files/{index}")
def message_file(
    message_id: int, index: int, request: Request, db: DbSession, user: CurrentUser
):
    """下载聊天里的一条文件消息。只有会话成员能取，且文件必须是发送者自己的。"""
    msg = db.get(ChatMessage, int(message_id))
    if not msg or msg.recalled_at:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "消息不存在或已撤回")
    cs.require_member(db, msg.conversation_id, user)
    if msg.kind != "file" or not msg.payload:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "这条消息里没有文件")
    try:
        nodes = json.loads(msg.payload).get("nodes") or []
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件信息已损坏") from None
    if index < 0 or index >= len(nodes):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有这个文件")
    node_id = int(nodes[index].get("id") or 0)
    from ..models import Node

    node = db.get(Node, node_id)
    if not node or node.user_id != msg.sender_id or node.deleted_at:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件已被发送者删除")
    if node.type != "file" or not node.blob_sha256:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件夹请点「保存到我的文件」")
    path = storage.blob_path(node.blob_sha256)
    return storage.file_response(path, node.name, node.mime, request)


# --------------------------------------------------------------------------- #
# 图片 / 表情包
# --------------------------------------------------------------------------- #
async def _raw_body(request: Request, max_bytes: int, label: str) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"{label}不能超过 {max_bytes // 1024 // 1024} MB"
        )
    raw = await request.body()
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有收到图片数据")
    if len(raw) > max_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"{label}不能超过 {max_bytes // 1024 // 1024} MB"
        )
    return raw


@router.put("/images")
async def upload_image(request: Request, db: DbSession, user: CurrentUser) -> dict:
    """聊天图片：原始字节上传（与头像一致，省一个 multipart 依赖），服务端压到 1600px webp。"""
    check_origin(request)
    raw = await _raw_body(request, cs.IMAGE_MAX_BYTES, "图片")
    return cs.save_chat_image(user, raw)


@router.get("/media/{name}")
def chat_media(name: str, request: Request, user: CurrentUser):
    path = cs.media_path(name)
    from fastapi.responses import FileResponse

    return FileResponse(
        path,
        media_type="image/png" if name.endswith(".png") else "image/webp",
        headers={"Cache-Control": "private, max-age=604800"},
    )


@router.get("/stickers")
def list_stickers(db: DbSession, user: CurrentUser) -> dict:
    return cs.list_stickers(db, user)


@router.post("/stickers", status_code=status.HTTP_201_CREATED)
async def add_sticker(
    request: Request, db: DbSession, user: CurrentUser, name: str = Query(default="", max_length=40)
) -> dict:
    check_origin(request)
    raw = await _raw_body(request, cs.STICKER_MAX_BYTES, "表情图")
    row = cs.add_sticker(db, user, raw, name)
    db.commit()
    return {
        "sticker": {
            "id": row.id,
            "name": row.name,
            "url": f"/api/chat/media/{row.file}",
            "size": row.size,
        },
        "limit": cs.sticker_max(db),
    }


@router.delete("/stickers/{sticker_id}")
def delete_sticker(sticker_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    file = cs.delete_sticker(db, user, sticker_id)
    db.commit()
    cs.drop_sticker_file(file)
    return {"ok": True, "limit": cs.sticker_max(db)}


@router.get("/friends")
def chat_friends(db: DbSession, user: CurrentUser) -> dict:
    """选人面板：好友列表 + 表情包上限，一次取全（省往返）。"""
    return {
        "friends": cs.friends_for_picker(db, user),
        "stickerLimit": cs.sticker_max(db),
        "groupLimit": cs.group_max(db),
    }
