"""群管理：群资料、管理员、加入方式、群分享、群文件。

与 ``chat_service`` 的分工：那边管会话与消息（发消息、未读、撤回），
这边管「群本身」——改名换头像、公开/私密、管理员、群号与二维码、群文件。

群文件是**持久副本**，不是聊天里那种「只记元数据、回源到发送者网盘」的引用：
发送者删掉自己的文件，群里的副本照旧能下载。代价是占磁盘，
所以单独算一份群配额（默认每群 10 GB，后台可调），不占任何人的个人配额。
"""

from __future__ import annotations

import base64
import io
import mimetypes
import re
import secrets
import shutil
from pathlib import Path
from typing import Any

import qrcode
from fastapi import HTTPException, status
from PIL import Image, ImageOps
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import chat_service as cs
from .config import settings
from .models import ChatConversation, ChatMember, GroupFile, User, utcnow
from .settings_store import get_int
from .user_service import iso

GROUP_AVATAR_RE = re.compile(r"^g([1-9]\d{0,17})-[0-9a-f]{12}\.(webp|png)$")
AVATAR_SIDE = 256
AVATAR_MAX_BYTES = 4 * 1024 * 1024

DEFAULT_QUOTA_GB = 10
QUOTA_GB_MAX = 1024
# 单次上传的硬上限：再大就该走网盘 + 分享链接了，浏览器流式上传也顶不住
FILE_MAX_BYTES = 2 * 1024 * 1024 * 1024
NAME_MAX = 200
SEARCH_LIMIT = 20


# --------------------------------------------------------------------------- #
# 配置项
# --------------------------------------------------------------------------- #
def file_quota_gb(db: Session) -> int:
    """每个群的群文件配额（GB）。后台可调，读不到就用默认 10。"""
    value = get_int(db, "group_file_quota_gb") or DEFAULT_QUOTA_GB
    return max(1, min(QUOTA_GB_MAX, value))


def file_quota_bytes(db: Session) -> int:
    return file_quota_gb(db) * 1024**3


# --------------------------------------------------------------------------- #
# 磁盘布局
# --------------------------------------------------------------------------- #
def _group_root(conv_id: int) -> Path:
    return settings.group_dir / str(int(conv_id))


def avatar_path(name: str | None) -> Path | None:
    if not name or not GROUP_AVATAR_RE.fullmatch(name):
        return None
    path = settings.group_dir / name
    return path if path.is_file() else None


def avatar_media_type(name: str) -> str:
    return "image/png" if name.endswith(".png") else "image/webp"


def _normalize_avatar(raw: bytes) -> tuple[bytes, str]:
    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            canvas = Image.new("RGB", img.size, (255, 255, 255))
            canvas.paste(img, mask=img.split()[-1])
            img = canvas
        else:
            img = img.convert("RGB")
        w, h = img.size
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        img = img.crop((left, top, left + side, top + side)).resize(
            (AVATAR_SIDE, AVATAR_SIDE), Image.LANCZOS
        )
        buf = io.BytesIO()
        try:
            img.save(buf, "WEBP", quality=90, method=4)
            return buf.getvalue(), "webp"
        except Exception:  # Pillow 没编 webp
            buf = io.BytesIO()
            img.save(buf, "PNG", optimize=True)
            return buf.getvalue(), "png"


def save_avatar(db: Session, me: User, conv_id: int, raw: bytes) -> str:
    conv, _ = require_manage(db, conv_id, me)
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有收到图片数据")
    if len(raw) > AVATAR_MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "群头像不能超过 4 MB")
    try:
        data, ext = _normalize_avatar(raw)
    except Exception:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "无法识别这张图片，请用 png / jpg / webp"
        ) from None
    settings.group_dir.mkdir(parents=True, exist_ok=True)
    name = f"g{conv.id}-{secrets.token_hex(6)}.{ext}"
    target = settings.group_dir / name
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    old = conv.avatar
    conv.avatar = name
    conv.updated_at = utcnow()
    db.flush()
    if old and old != name:
        drop_avatar(old)
    cs._system_message(db, conv, f"{cs._name_of(me)} 更新了群头像")
    return name


def drop_avatar(name: str | None) -> None:
    path = avatar_path(name)
    if path:
        path.unlink(missing_ok=True)


def purge_storage(conv_id: int) -> None:
    """群解散：删掉这个群的整个目录（群文件 + 群文件临时文件）。"""
    root = _group_root(conv_id)
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 权限
# --------------------------------------------------------------------------- #
def require_manage(db: Session, conv_id: int, me: User) -> tuple[ChatConversation, ChatMember]:
    """群主或管理员才能做的操作（改资料、邀请、设管理员等）。"""
    conv, member = cs.require_member(db, conv_id, me)
    if conv.kind != "group":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "单聊没有群设置")
    if conv.owner_id != me.id and not member.admin_at:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有群主和管理员能改群设置")
    return conv, member


def require_owner(db: Session, conv_id: int, me: User) -> ChatConversation:
    conv, _ = cs.require_member(db, conv_id, me)
    if conv.kind != "group":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "单聊没有群设置")
    if conv.owner_id != me.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有群主能做这个操作")
    return conv


# --------------------------------------------------------------------------- #
# 群资料
# --------------------------------------------------------------------------- #
def update_group(db: Session, me: User, conv_id: int, patch: dict[str, Any]) -> ChatConversation:
    conv, _ = require_manage(db, conv_id, me)
    changed: list[str] = []

    if patch.get("title") is not None:
        name = " ".join(str(patch["title"]).split())[:60]
        if not name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "群名不能为空")
        if name != conv.title:
            conv.title = name
            changed.append(f"群名改为「{name}」")

    if patch.get("description") is not None:
        text = str(patch["description"]).strip()[:200]
        if text != conv.description:
            conv.description = text
            changed.append("更新了群简介")

    if patch.get("isPublic") is not None:
        # 公开性决定「谁能搜到并直接加入」，只给群主改，管理员改不了
        if conv.owner_id != me.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "只有群主能设置公开/私密")
        wanted = bool(patch["isPublic"])
        if wanted != conv.is_public:
            conv.is_public = wanted
            changed.append("把群设为公开群" if wanted else "把群设为私密群")

    conv.updated_at = utcnow()
    db.flush()
    for text in changed:
        cs._system_message(db, conv, f"{cs._name_of(me)} {text}")
    return conv


# --------------------------------------------------------------------------- #
# 管理员
# --------------------------------------------------------------------------- #
def set_admin(db: Session, me: User, conv_id: int, target_id: int, on: bool) -> dict[str, Any]:
    conv = require_owner(db, conv_id, me)
    target_id = int(target_id)
    if target_id == conv.owner_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "群主不需要设为管理员")
    row = cs.member_of(db, conv.id, target_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "他不在这个群里")
    if bool(row.admin_at) == bool(on):
        return {"ok": True, "changed": False}
    row.admin_at = utcnow() if on else None
    db.flush()
    name = cs._name_of(db.get(User, target_id))
    cs._system_message(db, conv, f"{name} {'成为管理员' if on else '被取消了管理员'}")
    from . import notice_service as notice

    notice.notify(
        db,
        target_id,
        "chat",
        f"你在群「{conv.title}」里{'成为管理员' if on else '被取消了管理员'}",
        "",
        f"#/chat/{conv.id}",
    )
    return {"ok": True, "changed": True}


# --------------------------------------------------------------------------- #
# 群号 / 分享
# --------------------------------------------------------------------------- #
def generate_no(db: Session) -> str:
    """9 位数字群号（不以 0 开头），唯一。撞了就再抽一次。"""
    for _ in range(20):
        no = str(secrets.randbelow(9 * 10**8) + 10**8)
        if db.scalar(select(ChatConversation.id).where(ChatConversation.group_no == no).limit(1)) is None:
            return no
    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "群号生成失败，请重试")


def ensure_group_no(db: Session, conv: ChatConversation) -> str:
    if conv.group_no:
        return conv.group_no
    conv.group_no = generate_no(db)
    db.flush()
    return conv.group_no


def ensure_share_token(db: Session, conv: ChatConversation) -> str:
    if conv.share_token:
        return conv.share_token
    conv.share_token = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:18]
    db.flush()
    return conv.share_token


def qr_data_url(text: str) -> str:
    img = qrcode.make(text, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def share_info(db: Session, me: User, conv_id: int, base_url: str = "") -> dict[str, Any]:
    """群号 + 分享链接 + 二维码。成员都能看（转发邀请是正常需求）。

    邀请链接一直有效，直到有权限的人点「重置链接」：这样分享面板一打开就有东西可复制，
    不会出现「先点一下开启才有链接」的迷惑状态；要收回也能收回（旧链接立刻失效）。
    """
    conv, _ = cs.require_member(db, conv_id, me)
    if conv.kind != "group":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "单聊没有群分享")
    created = not (conv.group_no and conv.share_token)
    no = ensure_group_no(db, conv)
    token = ensure_share_token(db, conv)
    if created:
        # 老群第一次打开面板时才补群号/链接：这里必须落库。
        # 只在会话里生成而不提交，请求结束就回滚 —— 返回给人的链接自己去访问会 404。
        db.commit()
        db.refresh(conv)
    path = f"/g/{token}"
    url = f"{base_url.rstrip('/')}{path}" if base_url else path
    return {
        "groupNo": no,
        "isPublic": bool(conv.is_public),
        "shareEnabled": True,
        "path": path,
        "url": url,
        "qr": qr_data_url(url),
    }


def reset_share(db: Session, me: User, conv_id: int, base_url: str = "") -> dict:
    """重置邀请链接：旧链接立即失效，新链接可用。"""
    conv, _ = require_manage(db, conv_id, me)
    conv.share_token = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:18]
    conv.updated_at = utcnow()
    db.flush()
    return share_info(db, me, conv_id, base_url)


# --------------------------------------------------------------------------- #
# 找群 / 加群
# --------------------------------------------------------------------------- #
def _join_check(db: Session, conv: ChatConversation, me: User) -> None:
    if cs.member_of(db, conv.id, me.id):
        return
    limit = cs.group_max(db)
    count = int(
        db.scalar(
            select(func.count())
            .select_from(ChatMember)
            .where(ChatMember.conversation_id == conv.id)
        )
        or 0
    )
    if count >= limit:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"群成员已满（{limit} 人）")
    joined = int(
        db.scalar(
            select(func.count())
            .select_from(ChatMember)
            .join(ChatConversation, ChatConversation.id == ChatMember.conversation_id)
            .where(ChatMember.user_id == me.id, ChatConversation.kind == "group")
        )
        or 0
    )
    if joined >= cs.MAX_GROUPS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"最多加入 {cs.MAX_GROUPS} 个群聊")


def join(db: Session, me: User, conv: ChatConversation, via: str = "link") -> dict[str, Any]:
    _join_check(db, conv, me)
    if cs.member_of(db, conv.id, me.id):
        data = cs.list_conversations(db, me)
        hit = next((item for item in data["items"] if item["id"] == conv.id), None)
        return {"joined": False, "conversation": hit}
    db.add(ChatMember(conversation_id=conv.id, user_id=me.id, role="member"))
    db.flush()
    conv.updated_at = utcnow()
    cs._system_message(db, conv, f"{cs._name_of(me)} 通过{via}加入了群聊")
    data = cs.list_conversations(db, me)
    hit = next((item for item in data["items"] if item["id"] == conv.id), None)
    return {"joined": True, "conversation": hit}


def search_public(db: Session, me: User, query: str) -> list[dict[str, Any]]:
    """公开群搜索：只在 is_public 的群里找，排除我已经在的。"""
    text = (query or "").strip()
    if not text:
        return []
    mine = set(
        db.scalars(select(ChatMember.conversation_id).where(ChatMember.user_id == me.id)).all()
    )
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    rows = db.scalars(
        select(ChatConversation)
        .where(
            ChatConversation.kind == "group",
            ChatConversation.is_public.is_(True),
            or_(
                ChatConversation.title.ilike(pattern, escape="\\"),
                ChatConversation.group_no == text,
            ),
        )
        .order_by(ChatConversation.updated_at.desc())
        .limit(SEARCH_LIMIT)
    ).all()
    out = []
    for conv in rows:
        if conv.id in mine:
            continue
        count = int(
            db.scalar(
                select(func.count())
                .select_from(ChatMember)
                .where(ChatMember.conversation_id == conv.id)
            )
            or 0
        )
        out.append(
            {
                "id": conv.id,
                "title": conv.title,
                "description": conv.description or "",
                "avatarUrl": f"/api/chat/group-avatar/{conv.avatar}" if conv.avatar else None,
                "groupNo": conv.group_no,
                "memberCount": count,
            }
        )
    return out


def find_by_no(db: Session, no: str) -> ChatConversation:
    text = (no or "").strip()
    conv = db.scalar(
        select(ChatConversation)
        .where(ChatConversation.kind == "group", ChatConversation.group_no == text)
        .limit(1)
    )
    if not conv:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有找到这个群号")
    if not conv.is_public:
        # 私密群不认群号：否则「私密」等于没有
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "这个群没有公开，需要群主或管理员发邀请链接"
        )
    return conv


def find_by_token(db: Session, token: str) -> ChatConversation:
    text = (token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{6,24}", text):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "邀请链接无效")
    conv = db.scalar(
        select(ChatConversation)
        .where(ChatConversation.kind == "group", ChatConversation.share_token == text)
        .limit(1)
    )
    if not conv:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "邀请链接已失效")
    return conv


def public_preview(db: Session, conv: ChatConversation) -> dict[str, Any]:
    """未登录也能看的群摘要（邀请页用）：只有名字/头像/简介/人数。"""
    count = int(
        db.scalar(
            select(func.count())
            .select_from(ChatMember)
            .where(ChatMember.conversation_id == conv.id)
        )
        or 0
    )
    return {
        "id": conv.id,
        "title": conv.title,
        "description": conv.description or "",
        "avatarUrl": f"/api/public/groups/{conv.share_token}/avatar" if conv.avatar else None,
        "memberCount": count,
        "isPublic": bool(conv.is_public),
    }


# --------------------------------------------------------------------------- #
# 群文件
# --------------------------------------------------------------------------- #
def _safe_name(raw: str) -> str:
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ord(ch) >= 32).strip(". ")
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件名不能为空")
    return name[:NAME_MAX]


def list_files(db: Session, me: User, conv_id: int) -> dict[str, Any]:
    conv, _ = cs.require_member(db, conv_id, me)
    if conv.kind != "group":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "单聊没有群文件")
    rows = list(
        db.scalars(
            select(GroupFile)
            .where(GroupFile.conversation_id == conv.id)
            .order_by(GroupFile.id.desc())
            .limit(200)
        ).all()
    )
    uploaders = cs._users(db, [row.uploader_id for row in rows])
    mine = cs.member_of(db, conv.id, me.id)
    admin = bool(mine and mine.admin_at)
    items = []
    for row in rows:
        uploader = uploaders.get(row.uploader_id)
        items.append(
            {
                "id": row.id,
                "name": row.name,
                "size": row.size,
                "mime": row.mime,
                "createdAt": iso(row.created_at),
                "uploaderId": row.uploader_id,
                "uploaderName": cs._name_of(uploader),
                "canDelete": me.id in (row.uploader_id, conv.owner_id) or admin,
            }
        )
    quota = file_quota_bytes(db)
    return {
        "items": items,
        "used": int(conv.file_bytes or 0),
        "quota": quota,
        "fileMax": FILE_MAX_BYTES,
    }


def check_room(db: Session, conv: ChatConversation, declared: int = 0) -> int:
    """返回还能再写多少字节；超限直接 413。"""
    quota = file_quota_bytes(db)
    used = int(conv.file_bytes or 0)
    room = quota - used
    if declared and declared > FILE_MAX_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"单个群文件不能超过 {FILE_MAX_BYTES // 1024**3} GB",
        )
    if declared and declared > room:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"群空间不够了，这个群只剩 {max(0, room) // 1024**2} MB",
        )
    if room <= 0:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"群文件空间已满（{file_quota_gb(db)} GB）",
        )
    return room


def prepare_upload(db: Session, me: User, conv_id: int, name: str, size: int) -> tuple[ChatConversation, Path, str]:
    """建目录 + 生成落盘名，返回 (群, 目标路径, 落盘文件名)。调用方负责流式写入。"""
    conv, _ = cs.require_member(db, conv_id, me)
    if conv.kind != "group":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "单聊没有群文件")
    clean = _safe_name(name)
    room = check_room(db, conv, size)
    if size > room:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"群空间不够了，这个群只剩 {max(0, room) // 1024**2} MB",
        )
    suffix = Path(clean).suffix.lower()[:12]
    if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix or ""):
        suffix = ""
    stored = f"{secrets.token_hex(8)}{suffix}"
    root = _group_root(conv.id)
    root.mkdir(parents=True, exist_ok=True)
    return conv, root / stored, stored


def commit_file(
    db: Session, conv: ChatConversation, me: User, name: str, stored: str, size: int
) -> GroupFile:
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    row = GroupFile(
        conversation_id=conv.id,
        uploader_id=me.id,
        name=_safe_name(name),
        file=stored,
        size=int(size),
        mime=mime,
        created_at=utcnow(),
    )
    db.add(row)
    conv.file_bytes = int(conv.file_bytes or 0) + int(size)
    conv.updated_at = utcnow()
    db.flush()
    return row


def file_row(db: Session, me: User, file_id: int) -> tuple[GroupFile, ChatConversation]:
    row = db.get(GroupFile, int(file_id))
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    conv, _ = cs.require_member(db, row.conversation_id, me)
    return row, conv


def file_path(conv_id: int, stored: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{16}(\.[a-z0-9]{1,12})?", stored or ""):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    path = _group_root(conv_id) / stored
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件已被删除")
    return path


def delete_file(db: Session, me: User, file_id: int) -> dict[str, Any]:
    row, conv = file_row(db, me, file_id)
    member = cs.member_of(db, conv.id, me.id)
    if me.id not in (row.uploader_id, conv.owner_id) and not (member and member.admin_at):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有上传者、群主或管理员能删除")
    path = _group_root(conv.id) / row.file
    db.delete(row)
    conv.file_bytes = max(0, int(conv.file_bytes or 0) - int(row.size or 0))
    conv.updated_at = utcnow()
    db.flush()
    path.unlink(missing_ok=True)
    return {"ok": True, "used": int(conv.file_bytes)}
