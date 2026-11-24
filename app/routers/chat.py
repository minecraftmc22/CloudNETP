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
from .. import group_service as gs
from ..db import get_db
from ..deps import get_current_user
from ..models import ChatConversation, ChatMessage, User
from ..schemas import (
    ChatAdminIn,
    ChatDirectIn,
    ChatForwardIn,
    ChatGroupIn,
    ChatGroupPatch,
    ChatJoinIn,
    ChatMemberNoteIn,
    ChatMembersIn,
    ChatMuteIn,
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
    """群资料局部更新：群名 / 简介 / 公开私密（只有群主能改公开性）。"""
    check_origin(request)
    gs.update_group(db, user, conversation_id, payload.model_dump(exclude_none=True, by_alias=True))
    db.commit()
    return conversation_detail(conversation_id, db, user)


@router.put("/conversations/{conversation_id}/avatar")
async def upload_group_avatar(
    conversation_id: int, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    """群头像：原始字节上传，服务端裁方图 + 缩到 256px（与用户头像同一套规格）。"""
    check_origin(request)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > gs.AVATAR_MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "群头像不能超过 4 MB")
    raw = await request.body()
    gs.save_avatar(db, user, conversation_id, raw)
    db.commit()
    return conversation_detail(conversation_id, db, user)


@router.get("/group-avatar/{name}")
def group_avatar(name: str):
    """群头像：登录即可读（和用户头像同理，群成员之外的人也能看到分享页里的头像）。"""
    from fastapi.responses import FileResponse

    path = gs.avatar_path(name)
    if not path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "头像不存在")
    return FileResponse(
        path,
        media_type=gs.avatar_media_type(name),
        headers={"Cache-Control": "private, max-age=604800"},
    )


@router.put("/conversations/{conversation_id}/members/{member_id}/note")
def set_member_note(
    conversation_id: int,
    member_id: int,
    payload: ChatMemberNoteIn,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """群内备注：只对我生效，别人看到的还是他自己的备注/昵称。"""
    check_origin(request)
    if int(member_id) != int(payload.user_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "成员 id 对不上")
    result = cs.set_member_note(db, user, conversation_id, member_id, payload.alias)
    db.commit()
    return result


@router.post("/conversations/{conversation_id}/admins")
def set_group_admin(
    conversation_id: int, payload: ChatAdminIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    """设 / 取消群管理员（只有群主能操作）。"""
    check_origin(request)
    result = gs.set_admin(db, user, conversation_id, payload.user_id, payload.admin)
    db.commit()
    return result


@router.post("/conversations/{conversation_id}/members/{member_id}/mute")
def mute_group_member(
    conversation_id: int,
    member_id: int,
    payload: ChatMuteIn,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """禁言 / 解除禁言（群主与管理员可用；minutes<=0 为解除）。"""
    check_origin(request)
    if int(member_id) != int(payload.user_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "成员 id 对不上")
    result = gs.mute_member(db, user, conversation_id, member_id, payload.minutes)
    db.commit()
    return result


@router.post("/conversations/{conversation_id}/dissolve")
def dissolve_group(
    conversation_id: int, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    """解散群聊：只有群主能做，成员与消息一起删掉，不可恢复。"""
    check_origin(request)
    result = gs.dissolve_group(db, user, conversation_id)
    db.commit()
    return result


# --------------------------------------------------------------------------- #
# 群分享 / 找群
# --------------------------------------------------------------------------- #
@router.get("/conversations/{conversation_id}/share")
def group_share(conversation_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    """群号 + 邀请链接 + 二维码（成员都能看，转发邀请是正常需求）。"""
    return gs.share_info(db, user, conversation_id, str(request.base_url))


@router.post("/conversations/{conversation_id}/share")
def group_share_reset(
    conversation_id: int, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    """重置邀请链接：旧链接立即失效（群主 / 管理员可用）。"""
    check_origin(request)
    result = gs.reset_share(db, user, conversation_id, str(request.base_url))
    db.commit()
    return result


@router.get("/groups/search")
def search_groups(
    db: DbSession, user: CurrentUser, q: str = Query(default="", max_length=60)
) -> dict:
    return {"items": gs.search_public(db, user, q)}


@router.post("/groups/join")
def join_group(payload: ChatJoinIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    """加群：公开群点进来 / 输群号 / 点邀请链接。私密群只认邀请链接。"""
    check_origin(request)
    via = "群号"
    if payload.conversation_id:
        conv = db.get(ChatConversation, int(payload.conversation_id))
        if not conv or conv.kind != "group":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "群不存在")
        if not conv.is_public:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "这个群没有公开，需要群主或管理员发邀请链接"
            )
        via = "公开群"
    elif payload.token:
        conv = gs.find_by_token(db, payload.token)
        via = "邀请链接"
    elif payload.no:
        conv = gs.find_by_no(db, payload.no)
        via = "群号"
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请提供群号或邀请链接")
    result = gs.join(db, user, conv, via=via)
    db.commit()
    return result


# --------------------------------------------------------------------------- #
# 群文件
# --------------------------------------------------------------------------- #
@router.get("/conversations/{conversation_id}/files")
def list_group_files(conversation_id: int, db: DbSession, user: CurrentUser) -> dict:
    return gs.list_files(db, user, conversation_id)


@router.put("/conversations/{conversation_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_group_file(
    conversation_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    name: str = Query(..., min_length=1, max_length=200),
) -> dict:
    """上传群文件：流式落盘（不把整个文件读进内存），边写边核配额。"""
    check_origin(request)
    declared = request.headers.get("content-length")
    size = int(declared) if declared and declared.isdigit() else 0
    conv, path, stored = gs.prepare_upload(db, user, conversation_id, name, size)
    total = 0
    try:
        with path.open("wb") as fh:
            async for chunk in request.stream():
                total += len(chunk)
                if total > gs.FILE_MAX_BYTES:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"单个群文件不能超过 {gs.FILE_MAX_BYTES // 1024**3} GB",
                    )
                if total > gs.check_room(db, conv):
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "群文件空间已满"
                    )
                fh.write(chunk)
        if total == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有收到文件内容")
        row = gs.commit_file(db, conv, user, name, stored, total)
        db.commit()
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {
        "file": {
            "id": row.id,
            "name": row.name,
            "size": row.size,
            "mime": row.mime,
            "uploaderId": row.uploader_id,
            "uploaderName": cs._name_of(user),
            "canDelete": True,
        },
        "used": int(conv.file_bytes or 0),
        "quota": gs.file_quota_bytes(db),
    }


@router.get("/group-files/{file_id}/download")
def download_group_file(file_id: int, request: Request, db: DbSession, user: CurrentUser):
    row, conv = gs.file_row(db, user, file_id)
    path = gs.file_path(conv.id, row.file)
    return storage.file_response(path, row.name, row.mime, request)


@router.delete("/group-files/{file_id}")
def delete_group_file(
    file_id: int, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    result = gs.delete_file(db, user, file_id)
    db.commit()
    return result


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


@router.post("/conversations/{conversation_id}/clear")
def clear_conversation(
    conversation_id: int, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    """清空聊天记录：只清我这边，对方的历史不受影响。"""
    check_origin(request)
    result = cs.clear_history(db, user, conversation_id)
    db.commit()
    return result


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: int, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    """删除聊天：单聊只在我这边隐藏（对方再发消息会恢复），群聊等于退群。"""
    check_origin(request)
    conv, _ = cs.require_member(db, conversation_id, user)
    if conv.kind == "group":
        result = cs.leave_group(db, user, conversation_id)
    else:
        result = cs.hide_conversation(db, user, conversation_id)
    db.commit()
    return result


# --------------------------------------------------------------------------- #
# 拉黑
# --------------------------------------------------------------------------- #
@router.get("/blocks")
def list_blocks(db: DbSession, user: CurrentUser) -> dict:
    return {"items": cs.list_blocks(db, user)}


@router.post("/blocks", status_code=status.HTTP_201_CREATED)
def block_peer(payload: ChatDirectIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    """拉黑并删除：同时断好友关系，并把这边的聊天从列表里删掉。"""
    check_origin(request)
    result = cs.block_peer(db, user, payload.user_id)
    db.commit()
    log_action(db, "chat_block", user.id, {"peer": payload.user_id}, request)
    db.commit()
    return result


@router.delete("/blocks/{user_id}")
def unblock_peer(user_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    result = cs.unblock(db, user, user_id)
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
        reply_to=payload.reply_to,
    )
    db.commit()
    return {"message": cs.sent_payload(db, msg)}


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


@router.get("/alerts")
def alerts(
    db: DbSession,
    user: CurrentUser,
    after: int = Query(default=0, ge=0),
) -> dict:
    """消息提醒：``after``（上次提醒到的消息 id）之后的新消息摘要。

    前端全局轮询这个接口：页面可见时弹站内小卡片，页面在后台时发系统通知。
    """
    return cs.alert_summary(db, user, after_id=after)


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

    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    media_type = {"png": "image/png", "gif": "image/gif"}.get(suffix, "image/webp")
    return FileResponse(
        path,
        media_type=media_type,
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
    # 先按「动图上限」收字节：静态图的更严上限由 cs.add_sticker 按实际类型判断，
    # 否则同一张 GIF 在不同用户的设置下会拿到两种错误码，提示也说不清。
    raw = await _raw_body(request, cs.sticker_gif_max_bytes(db), "表情图")
    row, created = cs.add_sticker(db, user, raw, name)
    db.commit()
    return {
        "sticker": {
            "id": row.id,
            "name": row.name,
            "url": f"/api/chat/media/{row.file}",
            "size": row.size,
        },
        # false = 同样的表情已经在我的表情里了（按 sha256 判定）
        "created": created,
        "limit": cs.sticker_max(db),
    }


@router.post("/messages/{message_id}/sticker", status_code=status.HTTP_201_CREATED)
def save_sticker_from_message(
    message_id: int, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    """把聊天里别人发的表情收进我的表情（哈希相同就不重复存）。"""
    check_origin(request)
    row, created = cs.add_sticker_from_message(db, user, message_id)
    db.commit()
    return {
        "sticker": {
            "id": row.id,
            "name": row.name,
            "url": f"/api/chat/media/{row.file}",
            "size": row.size,
        },
        "created": created,
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
        # 撤回时限（分钟，0 = 不限）：前端据此决定菜单里给不给「撤回」
        "recallMinutes": cs.recall_minutes(db),
    }
