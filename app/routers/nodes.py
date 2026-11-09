"""文件与目录路由：浏览、增删改、移动、搜索、回收站、下载、上传。"""

from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import node_service as ns
from .. import storage
from .. import lyrics_service
from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import AudioPref, Node, User
from ..schemas import FolderIn, LyricsPrefIn, MoveIn, RenameIn
from ..security import check_origin, normalize_name
from ..settings_store import get_int
from ..user_service import ensure_settings, log_action

router = APIRouter(prefix="/api", tags=["nodes"])

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


# --------------------------------------------------------------------------- #
# 浏览
# --------------------------------------------------------------------------- #
@router.get("/nodes")
def list_nodes(
    db: DbSession,
    user: CurrentUser,
    parentId: int | None = Query(default=None, alias="parentId"),
    sort: str = Query(default="name", pattern="^(name|size|updated|created)$"),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
) -> dict:
    if parentId is not None:
        ns.require_parent(db, user.id, parentId)
    items = ns.list_children(db, user.id, parentId, sort, order, is_admin=user.role == 1)
    return {
        "parentId": parentId,
        "breadcrumb": ns.breadcrumb(db, user.id, parentId),
        "items": items,
        "total": len(items),
    }


@router.get("/nodes/{node_id}")
def node_detail(node_id: int, db: DbSession, user: CurrentUser) -> dict:
    node = ns.get_node(db, user.id, node_id, allow_deleted=True)
    return {"node": ns.node_payload(node, is_admin=user.role == 1)}


@router.get("/search")
def search_nodes(
    db: DbSession,
    user: CurrentUser,
    q: str = Query(default="", max_length=128),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    items = ns.search(db, user.id, q, limit, is_admin=user.role == 1)
    return {"query": q, "items": items, "total": len(items)}


# --------------------------------------------------------------------------- #
# 目录与重命名
# --------------------------------------------------------------------------- #
@router.post("/nodes/folder", status_code=status.HTTP_201_CREATED)
def create_folder(payload: FolderIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    name = normalize_name(payload.name)
    node = ns.create_folder(db, user, payload.parent_id, name)
    log_action(db, "folder_create", user.id, {"name": node.name, "parentId": payload.parent_id}, request)
    db.commit()
    db.refresh(node)
    return {"node": ns.node_payload(node, 0, is_admin=user.role == 1)}


@router.patch("/nodes/{node_id}")
def patch_node(
    node_id: int, payload: RenameIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    node = ns.get_node(db, user.id, node_id)
    ns.rename_node(db, user, node, normalize_name(payload.name))
    log_action(db, "node_rename", user.id, {"id": node_id, "name": node.name}, request)
    db.commit()
    db.refresh(node)
    return {"node": ns.node_payload(node, is_admin=user.role == 1)}


@router.post("/nodes/{node_id}/move")
def move_node(node_id: int, payload: MoveIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    node = ns.get_node(db, user.id, node_id)
    ns.move_node(db, user, node, payload.parent_id)
    log_action(db, "node_move", user.id, {"id": node_id, "parentId": payload.parent_id}, request)
    db.commit()
    db.refresh(node)
    return {"node": ns.node_payload(node, is_admin=user.role == 1)}


# --------------------------------------------------------------------------- #
# 删除 / 回收站
# --------------------------------------------------------------------------- #
@router.delete("/nodes/{node_id}")
def delete_node(node_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    node = ns.get_node(db, user.id, node_id)

    # 回收站被用户关掉时，删除即彻底删除（用户在设置里已用验证码确认过这个后果）
    if not ensure_settings(db, user.id).trash_enabled:
        removed = ns.purge_node(db, user, node)
        ns.recalc_usage(db, user)
        log_action(db, "node_delete_permanent", user.id, {"id": node_id, "name": node.name}, request)
        db.commit()
        return {"deleted": removed, "purged": True}

    count = ns.soft_delete(db, user, node)
    log_action(db, "node_delete", user.id, {"id": node_id, "name": node.name, "count": count}, request)
    db.commit()
    return {"deleted": count, "purged": False}


@router.post("/nodes/{node_id}/restore")
def restore_node(node_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    node = ns.get_node(db, user.id, node_id, allow_deleted=True)
    if not node.deleted_at:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该文件不在回收站里")
    ns.restore_node(db, user, node)
    log_action(db, "node_restore", user.id, {"id": node_id, "name": node.name}, request)
    db.commit()
    db.refresh(node)
    return {"node": ns.node_payload(node, is_admin=user.role == 1)}


@router.delete("/nodes/{node_id}/purge")
def purge_node(node_id: int, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    node = ns.get_node(db, user.id, node_id, allow_deleted=True)
    removed = ns.purge_node(db, user, node)
    ns.recalc_usage(db, user)
    log_action(db, "node_purge", user.id, {"id": node_id, "name": node.name}, request)
    db.commit()
    return {"purged": removed}


@router.get("/trash")
def trash(db: DbSession, user: CurrentUser) -> dict:
    items = ns.trash_list(db, user.id, is_admin=user.role == 1)
    return {"items": items, "total": len(items), "usedBytes": user.used_bytes}


@router.post("/trash/empty")
def empty_trash(request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    items = ns.trash_list(db, user.id, is_admin=user.role == 1)
    removed = 0
    for item in items:
        node = db.get(Node, item["id"])
        if node:
            removed += ns.purge_node(db, user, node)
    ns.recalc_usage(db, user)
    log_action(db, "trash_empty", user.id, {"removed": removed}, request)
    db.commit()
    return {"removed": removed}


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
@router.get("/nodes/{node_id}/download")
def download(node_id: int, request: Request, db: DbSession, user: CurrentUser):
    node = ns.get_node(db, user.id, node_id)
    if node.type != "file" or not node.blob_sha256:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件夹暂不支持直接下载")
    path = storage.blob_path(node.blob_sha256)
    return storage.file_response(path, node.name, node.mime, request)


# --------------------------------------------------------------------------- #
# 在线预览
# --------------------------------------------------------------------------- #
@router.get("/nodes/{node_id}/preview")
def preview(node_id: int, request: Request, db: DbSession, user: CurrentUser):
    """内嵌预览的二进制流（图片/音视频/PDF），支持 Range，浏览器可直接解码。

    文本不走这里，改走 /text 返回 JSON，避免中文编码问题。
    """
    node = ns.get_node(db, user.id, node_id)
    if node.type != "file" or not node.blob_sha256:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件夹不支持在线预览")

    kind = ns.preview_kind(node)
    if kind == "none":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "该类型暂不支持在线预览")

    path = storage.blob_path(node.blob_sha256)
    # Office / 旧版二进制一律按二进制流下发：Chrome 会把
    # application/vnd.ms-powerpoint 这类「插件 MIME」的 fetch 响应吞成 204 空响应，
    # 预览端只关心字节，不需要浏览器去识别类型。
    # 顺带对这批类型禁用缓存：万一把旧版本带插件 MIME 的响应留在缓存里，
    # 重新校验拿到 304 后会继续沿用旧头，预览依旧打不开。
    media = ns.preview_mime(kind, node.mime)
    return storage.file_response(
        path,
        node.name,
        media,
        request,
        inline=True,
        cache="no-store" if media == "application/octet-stream" else "private, max-age=0",
        # PDF 交给浏览器内置查看器，sandbox 会让它渲染不出来
        csp="" if kind == "pdf" else None,
    )


_TEXT_PREVIEW_DEFAULT = 1_000_000


@router.get("/nodes/{node_id}/text")
def preview_text(
    node_id: int,
    db: DbSession,
    user: CurrentUser,
    max_bytes: int = Query(default=_TEXT_PREVIEW_DEFAULT, ge=1024, le=4_000_000),
) -> dict:
    node = ns.get_node(db, user.id, node_id)
    if node.type != "file" or not node.blob_sha256:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件夹不支持在线预览")
    if ns.preview_kind(node) not in ("text", "markdown"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "该文件不是文本类型")

    try:
        return storage.text_payload(storage.blob_path(node.blob_sha256), max_bytes)
    except ValueError:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "这是二进制文件，无法按文本预览"
        ) from None


# --------------------------------------------------------------------------- #
# 音频元数据 + 歌词（全屏歌词播放器的数据源）
# --------------------------------------------------------------------------- #
def _audio_node(db: Session, user: User, node_id: int) -> Node:
    node = ns.get_node(db, user.id, node_id)
    if node.type != "file" or not node.blob_sha256:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件夹没有音频元数据")
    if ns.preview_kind(node) != "audio":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "该文件不是音频")
    return node


@router.get("/nodes/{node_id}/audio-meta")
def audio_meta(node_id: int, db: DbSession, user: CurrentUser) -> dict:
    """读取音频文件内嵌的 ID3/MP4/FLAC 元数据（标题/歌手/专辑/时长/是否有封面）。"""
    node = _audio_node(db, user, node_id)
    meta = lyrics_service.audio_meta(storage.blob_path(node.blob_sha256), _stem(node.name))
    return {
        "name": node.name,
        "title": meta["title"],
        "artist": meta["artist"],
        "album": meta["album"],
        "duration": meta["duration"],
        "hasCover": bool(meta["cover"]),
    }


@router.get("/nodes/{node_id}/cover")
def audio_cover(node_id: int, db: DbSession, user: CurrentUser) -> Response:
    """音频内嵌封面原图。没有内嵌封面时 404，前端回退到默认插画。"""
    node = _audio_node(db, user, node_id)
    meta = lyrics_service.audio_meta(storage.blob_path(node.blob_sha256), _stem(node.name))
    if not meta["cover"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "该音频没有内嵌封面")
    return Response(
        content=meta["cover"],
        media_type=meta["coverMime"] or "image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/lyrics/providers")
def lyrics_providers(user: CurrentUser) -> dict:
    """可用的歌词来源（管理后台的「歌词来源」优先，其次 .env）。

    前端「搜索指定」用它渲染分类切换；凭据没配齐的来源会带 ready=false 和原因。
    """
    items = lyrics_service.providers_info()
    return {
        "items": items,
        "default": lyrics_service.default_provider(),
        "total": len(items),
    }


@router.get("/lyrics")
def lyrics_proxy(
    db: DbSession,
    user: CurrentUser,
    songId: int | None = Query(default=None),
    title: str = Query(default="", max_length=120),
    artist: str = Query(default="", max_length=120),
    provider: str = Query(default="", max_length=24),
) -> dict:
    """歌词库代理：songId 指定歌曲，否则按「标题 + 歌手」自动匹配。

    provider 为空时用默认来源；指定了就必须是该来源里的歌曲 id。
    服务端代理，密钥不出内网。
    """
    try:
        if songId is not None:
            return lyrics_service.fetch_lyrics(songId, provider)
        if not title.strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "缺少 songId 或 title")
        return lyrics_service.find_and_fetch(title, artist)
    except lyrics_service.LyricsApiError as exc:
        http_status = status.HTTP_424_FAILED_DEPENDENCY
        if exc.status == 404:
            http_status = status.HTTP_404_NOT_FOUND
        elif exc.code in {"bad_provider", "provider_disabled"}:
            http_status = status.HTTP_400_BAD_REQUEST
        elif exc.code in {"quota_exceeded", "network", "upstream"}:
            http_status = status.HTTP_503_SERVICE_UNAVAILABLE
        raise HTTPException(http_status, exc.message or "歌词获取失败") from None


@router.get("/lyrics/search")
def lyrics_search(
    user: CurrentUser,
    q: str = Query(min_length=1, max_length=120),
    provider: str = Query(default="", max_length=24),
) -> dict:
    """歌词库搜索（手动给某首歌挑歌词用），只返回条目不取正文。

    provider 指定搜哪个来源（网易云 / 歌词库），留空用默认来源。
    """
    try:
        items = lyrics_service.search_songs(q.strip(), provider=provider)
    except lyrics_service.LyricsApiError as exc:
        http_status = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if exc.code in {"quota_exceeded", "network", "upstream"}
            else status.HTTP_424_FAILED_DEPENDENCY
        )
        if exc.code in {"bad_provider", "provider_disabled"}:
            http_status = status.HTTP_400_BAD_REQUEST
        raise HTTPException(http_status, exc.message or "歌词搜索失败") from None
    return {
        "query": q,
        "provider": lyrics_service.normalize_provider(provider)
        or lyrics_service.default_provider(),
        "items": items,
        "total": len(items),
    }


def _pref_payload(pref: AudioPref | None) -> dict:
    return {
        "mode": pref.mode if pref else "auto",
        "song": (
            {
                "id": pref.song_id,
                "title": pref.song_title,
                "artist": pref.song_artist,
                "provider": pref.provider or "lyriclib",
            }
            if pref and pref.song_id
            else None
        ),
        "customLyrics": (pref.custom_lyrics or "") if pref else "",
    }


@router.get("/nodes/{node_id}/lyrics-pref")
def get_lyrics_pref(node_id: int, db: DbSession, user: CurrentUser) -> dict:
    """读取这首歌当前的歌词来源偏好。"""
    node = _audio_node(db, user, node_id)
    pref = db.get(AudioPref, node.id)
    return _pref_payload(pref)


@router.put("/nodes/{node_id}/lyrics-pref")
def set_lyrics_pref(
    node_id: int,
    payload: LyricsPrefIn,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """设置这首歌的歌词来源：auto / song(指定在线歌曲) / custom(粘贴文本) / none。"""
    check_origin(request)
    node = _audio_node(db, user, node_id)
    pref = db.get(AudioPref, node.id)
    if pref is None:
        pref = AudioPref(node_id=node.id)
        db.add(pref)

    pref.mode = payload.mode
    if payload.mode == "song":
        if not payload.song_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "指定歌曲需要 songId")
        raw_provider = (payload.provider or "").strip()
        if raw_provider:
            provider = lyrics_service.normalize_provider(raw_provider)
            if not provider:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "未知的歌词来源")
        else:
            # 旧前端不会带 provider，按 0.2.x 的老行为算歌词库
            provider = "lyriclib"
        # 先取一次歌词校验 id 在该来源里有效，顺便预热缓存
        try:
            lyrics_service.fetch_lyrics(payload.song_id, provider)
        except lyrics_service.LyricsApiError as exc:
            http_status = (
                status.HTTP_404_NOT_FOUND
                if exc.status == 404
                else status.HTTP_503_SERVICE_UNAVAILABLE
            )
            raise HTTPException(http_status, exc.message or "该歌曲歌词不可用") from None
        pref.provider = provider
        pref.song_id = payload.song_id
        pref.song_title = (payload.song_title or "").strip()[:200]
        pref.song_artist = (payload.song_artist or "").strip()[:200]
    elif payload.mode == "custom":
        if not (payload.custom_lyrics or "").strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "自定义歌词内容不能为空")
        pref.custom_lyrics = payload.custom_lyrics
    else:  # auto / none：清掉旧选择
        pref.song_id = None
        pref.song_title = ""
        pref.song_artist = ""
        pref.provider = ""
    db.commit()
    log_action(db, "lyrics_pref", user.id, {"nodeId": node.id, "mode": payload.mode}, request)
    db.refresh(pref)
    return _pref_payload(pref)


@router.get("/nodes/{node_id}/lyrics")
def node_lyrics(node_id: int, db: DbSession, user: CurrentUser) -> dict:
    """解析这首歌最终生效的歌词。

    优先级（auto 模式）：音频内嵌 → 在线搜索 → 无歌词；
    用户也可显式指定在线歌曲 / 粘贴自定义文本 / 关闭歌词。
    """
    node = _audio_node(db, user, node_id)
    return lyrics_service.resolve_lyrics(
        db, node, storage.blob_path(node.blob_sha256), _stem(node.name)
    )


# --------------------------------------------------------------------------- #
# 同名字幕 / 缩略图 / 章节 / 弹幕（播放器插件的数据来源）
# --------------------------------------------------------------------------- #
SUB_EXT = {".vtt", ".srt", ".ass"}
_THUMB_SUFFIX = (".thumbs.vtt", ".thumbnail.vtt")
_CHAPTER_SUFFIX = (".chapters.json",)
_DANMAKU_SUFFIX = (".danmaku.json",)


def _stem(name: str) -> str:
    dot = name.rfind(".")
    return name[:dot] if dot > 0 else name


@router.get("/nodes/{node_id}/sidecars")
def sidecars(node_id: int, db: DbSession, user: CurrentUser) -> dict:
    """找同目录下与视频同名的伴生文件，供播放器插件自动挂载。

    约定（<同名> 指去掉扩展名的文件名）：
    - `<同名>.vtt` / `<同名>.srt` / `<同名>.ass`，或 `<同名>.zh.vtt` → 字幕
    - `<同名>.thumbs.vtt`  → 进度条缩略图（VTT 需带 #xywh）
    - `<同名>.chapters.json` → 章节，格式 [{"start":0,"end":60,"title":"开场"}]
    - `<同名>.danmaku.json`  → 弹幕，格式 [{"time":12.5,"text":"前排"}]
    """
    node = ns.get_node(db, user.id, node_id)
    if node.type != "file":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有文件才有伴生资源")

    stem = _stem(node.name)
    escaped = stem.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = db.scalars(
        select(Node)
        .where(
            Node.user_id == user.id,
            Node.parent_id == node.parent_id if node.parent_id is not None else Node.parent_id.is_(None),
            Node.type == "file",
            Node.deleted_at.is_(None),
            Node.id != node.id,
            Node.name.like(f"{escaped}.%", escape="\\"),
        )
        .order_by(Node.name)
    ).all()

    subtitles: list[dict] = []
    thumbnails = None
    chapters = None
    danmaku = None
    for row in rows:
        lower = row.name.lower()
        url = f"/api/nodes/{row.id}/preview"
        if lower.endswith(_THUMB_SUFFIX):
            thumbnails = thumbnails or url
        elif lower.endswith(_CHAPTER_SUFFIX):
            chapters = chapters or url
        elif lower.endswith(_DANMAKU_SUFFIX):
            danmaku = danmaku or url
        else:
            ext = lower[lower.rfind(".") :]
            if ext in SUB_EXT:
                middle = lower[len(stem) + 1 : -len(ext)]
                subtitles.append(
                    {
                        "name": row.name,
                        "url": url,
                        "type": ext.lstrip("."),
                        "lang": middle.strip(".") or "",
                    }
                )

    return {
        "subtitles": subtitles,
        "thumbnails": thumbnails,
        "chapters": chapters,
        "danmaku": danmaku,
        # 缩略图 VTT 里写的是相对文件名，前端据此改写成可访问的预览地址
        "siblings": {row.name: row.id for row in rows},
    }


# --------------------------------------------------------------------------- #
# 上传（单次流式 PUT，无需 python-multipart）
# --------------------------------------------------------------------------- #
@router.put("/nodes/upload", status_code=status.HTTP_201_CREATED)
async def upload(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    name: str = Query(..., min_length=1, max_length=255),
    parentId: int | None = Query(default=None),
) -> dict:
    check_origin(request)
    filename = normalize_name(name)
    target_parent = parentId
    ns.require_parent(db, user.id, target_parent)

    declared = request.headers.get("content-length")
    expected = int(declared) if declared and declared.isdigit() else 0
    max_size = get_int(db, "max_file_size_bytes") or settings.max_file_size_bytes
    if expected and expected > max_size:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"文件超过单文件上限（{max_size // 1024 // 1024} MB）",
        )
    if expected and user.used_bytes + expected > user.quota_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "存储空间不足，请清理回收站或联系管理员扩容"
        )

    tmp = storage.new_tmp_path()
    digest = hashlib.sha256()
    written = 0
    try:
        with tmp.open("wb") as fh:
            async for block in request.stream():
                if not block:
                    continue
                written += len(block)
                if written > max_size:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"文件超过单文件上限（{max_size // 1024 // 1024} MB）",
                    )
                if user.used_bytes + written > user.quota_bytes:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "存储空间不足")
                digest.update(block)
                fh.write(block)
        if written == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能上传空文件")
        sha = digest.hexdigest()
        dedup = storage.blob_exists(sha, written)
        storage.commit_tmp(tmp, sha)
    finally:
        tmp.unlink(missing_ok=True)

    header_mime = (request.headers.get("x-file-mime") or "").strip()[:127] or None
    node = ns.create_file(db, user, target_parent, filename, sha, written, header_mime)
    ns.recalc_usage(db, user)
    log_action(
        db,
        "node_upload",
        user.id,
        {"name": node.name, "size": written, "dedup": dedup},
        request,
    )
    db.commit()
    db.refresh(node)
    return {"node": ns.node_payload(node, is_admin=user.role == 1), "deduplicated": dedup}


@router.post("/nodes/check-hash")
def check_hash(
    db: DbSession,
    user: CurrentUser,
    sha256: str = Query(..., min_length=64, max_length=64),
    size: int = Query(default=0, ge=0),
) -> dict:
    """秒传探测：服务端已有同内容 blob 时前端可跳过上传。"""
    exists = ns.blob_ready(sha256, size) if sha256.isalnum() else False
    return {"exists": exists}
