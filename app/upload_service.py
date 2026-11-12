"""分片上传：会话管理、秒传判定、断点续传、合并入库。

协议（前端 = 客户端）：
    POST /api/uploads/init            -> 会话 id + 分片大小 + 已上传分片（秒传时直接返回节点）
    PUT  /api/uploads/{sid}/{index}   -> 裸字节流写一个分片（可并发、可乱序、可重传）
    GET  /api/uploads/{sid}           -> 缺失分片清单（断点续传用）
    POST /api/uploads/{sid}/complete  -> 合并 + 全量校验 + 建节点
    DELETE /api/uploads/{sid}         -> 放弃

设计要点：
- 分片落盘与 DB 记录分离：落盘成功才算数，DB 只存索引与 sha256；
- 重传同一分片幂等（覆盖写 + 更新记录），网络重试不需要额外逻辑；
- 合并时边拼边算 sha256，和 init 声明的哈希比对，防止半截文件混进 blob；
- 会话按 (用户, 文件名, 大小, 父目录, sha256) 复用，刷新页面后自动接着传。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import node_service as ns
from . import storage
from .config import settings
from .models import Node, UploadChunk, UploadSession, User, utcnow
from .settings_store import get_int

MIN_CHUNK = 256 * 1024          # 256 KiB
MAX_CHUNK = 32 * 1024 * 1024    # 32 MiB
MAX_CHUNKS = 20000
DEFAULT_CHUNKS = 4 * 1024 * 1024  # 4 MiB，前端可覆盖


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def chunk_size_for(size: int, requested: int | None) -> int:
    size_basis = requested or settings.chunk_size_bytes or DEFAULT_CHUNKS
    chunk = max(MIN_CHUNK, min(MAX_CHUNK, int(size_basis)))
    # 分片数过多时自动放大分片
    while size > 0 and (size + chunk - 1) // chunk > MAX_CHUNKS and chunk < MAX_CHUNK:
        chunk = min(MAX_CHUNK, chunk * 2)
    return chunk


def expected_chunk_size(session: UploadSession, index: int) -> int:
    if index < session.total_chunks - 1:
        return session.chunk_size
    tail = session.size - session.chunk_size * (session.total_chunks - 1)
    return tail if tail > 0 else session.chunk_size


def get_session(db: Session, user_id: int, session_id: str, *, allow_done: bool = False) -> UploadSession:
    row = db.get(UploadSession, session_id)
    if not row or row.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "上传会话不存在，可能已过期")
    if row.status == "aborted":
        raise HTTPException(status.HTTP_409_CONFLICT, "该上传已被取消，请重新发起")
    if row.status == "completed" and not allow_done:
        raise HTTPException(status.HTTP_409_CONFLICT, "该上传已完成")
    return row


def uploaded_indexes(db: Session, session: UploadSession) -> list[int]:
    """以磁盘为准：DB 有记录但文件不见了的分片重新算缺失。"""
    rows = db.execute(
        select(UploadChunk.chunk_index, UploadChunk.size).where(UploadChunk.session_id == session.id)
    ).all()
    result: list[int] = []
    stale: list[int] = []
    for index, size in rows:
        if storage.has_chunk(session.id, int(index), int(size)):
            result.append(int(index))
        else:
            stale.append(int(index))
    if stale:
        db.execute(
            delete(UploadChunk).where(
                UploadChunk.session_id == session.id, UploadChunk.chunk_index.in_(stale)
            )
        )
    return sorted(result)


def session_payload(db: Session, session: UploadSession, *, instant: bool = False, node: Node | None = None) -> dict:
    uploaded = uploaded_indexes(db, session)
    uploaded_set = set(uploaded)
    missing = [i for i in range(session.total_chunks) if i not in uploaded_set]
    payload = {
        "sessionId": session.id,
        "chunkSize": session.chunk_size,
        "totalChunks": session.total_chunks,
        "size": session.size,
        "uploaded": uploaded,
        "missing": missing,
        "uploadedBytes": session.chunk_size * len(uploaded),
        "instant": instant,
    }
    if node is not None:
        payload["node"] = ns.node_payload(node)
    return payload


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
def init_upload(
    db: Session,
    user: User,
    *,
    name: str,
    size: int,
    parent_id: int | None,
    sha256: str | None,
    mime: str | None,
    chunk_size: int | None,
    quick: bool = False,
) -> dict:
    # 快传上传不落进任何目录，也不占用户配额
    if quick:
        parent_id = None
    ns.require_parent(db, user.id, parent_id)

    if size <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能上传空文件")
    max_size = get_int(db, "max_file_size_bytes") or settings.max_file_size_bytes
    if size > max_size:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"文件超过单文件上限（{max_size // 1024 // 1024} MB）",
        )
    if not quick and user.used_bytes + size > user.quota_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "存储空间不足，请清理回收站或联系管理员扩容"
        )

    digest = (sha256 or "").strip().lower() or None
    if digest is not None and len(digest) != 64:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "sha256 格式不正确")

    mime = (mime or "").strip()[:127] or None

    # 1) 秒传：盘上已有同内容且大小一致
    if digest and storage.blob_exists(digest, size):
        node = ns.create_file(db, user, parent_id, name, digest, size, mime, quick=quick)
        if not quick:
            ns.recalc_usage(db, user)
        return {
            "instant": True,
            "node": ns.node_payload(node),
            "sessionId": None,
            "uploadedBytes": size,
        }

    # 2) 断点续传：同一份内容 + 同一个目标目录的未完成会话直接复用
    existing = db.scalars(
        select(UploadSession)
        .where(
            UploadSession.user_id == user.id,
            UploadSession.status == "uploading",
            UploadSession.filename == name,
            UploadSession.size == size,
            UploadSession.target_node_id.is_(None)
            if parent_id is None
            else UploadSession.target_node_id == parent_id,
            UploadSession.sha256 == (digest or ""),
            UploadSession.quick_owned == bool(quick),
        )
        .order_by(UploadSession.updated_at.desc())
        .limit(1)
    ).first()
    if existing is not None and storage.session_dir(existing.id).is_dir():
        existing.updated_at = utcnow()
        return session_payload(db, existing)

    # 3) 新会话
    chunk = chunk_size_for(size, chunk_size)
    total = max(1, (size + chunk - 1) // chunk)
    session = UploadSession(
        id=uuid.uuid4().hex,
        user_id=user.id,
        filename=name,
        size=size,
        mime=mime,
        sha256=digest or "",
        target_node_id=parent_id,
        chunk_size=chunk,
        total_chunks=total,
        status="uploading",
        quick_owned=bool(quick),
    )
    db.add(session)
    db.flush()
    storage.session_dir(session.id).mkdir(parents=True, exist_ok=True)
    return session_payload(db, session)


# --------------------------------------------------------------------------- #
# 写分片
# --------------------------------------------------------------------------- #
def save_chunk(db: Session, user_id: int, session_id: str, index: int, data: bytes) -> dict:
    session = get_session(db, user_id, session_id)
    if index < 0 or index >= session.total_chunks:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"分片序号超出范围（0 ~ {session.total_chunks - 1}）"
        )
    want = expected_chunk_size(session, index)
    if len(data) != want:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"分片大小不符：第 {index + 1} 片应为 {want} 字节，收到 {len(data)} 字节",
        )

    sha = storage.write_chunk(session.id, index, data)

    row = db.get(UploadChunk, (session.id, index))
    if row is None:
        db.add(UploadChunk(session_id=session.id, chunk_index=index, size=len(data), sha256=sha))
    else:
        row.size = len(data)
        row.sha256 = sha
    session.updated_at = utcnow()
    uploaded = uploaded_indexes(db, session)
    return {
        "index": index,
        "received": len(data),
        "uploadedCount": len(uploaded),
        "totalChunks": session.total_chunks,
    }


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #
def complete_upload(
    db: Session,
    user: User,
    session_id: str,
    sha256: str | None = None,
    *,
    guard=None,
) -> dict:
    """合并分片并建节点。

    `guard(real_sha)` 是内容校验后的最后一道闸门（快传用它拦黑名单哈希）；
    抛异常即放弃合并，临时文件由 finally 清掉，不会留下半个 blob。
    """
    session = get_session(db, user_id=user.id, session_id=session_id)
    uploaded = set(uploaded_indexes(db, session))
    missing = [i for i in range(session.total_chunks) if i not in uploaded]
    if missing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"还有 {len(missing)} 个分片没传完（第一个缺失：第 {missing[0] + 1} 片）",
        )

    tmp, real_sha, written = storage.assemble_chunks(session.id, session.total_chunks, session.size)
    try:
        declared = (sha256 or session.sha256 or "").strip().lower()
        if declared and declared != real_sha:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "文件校验失败：内容与服务端合并结果不一致，请重新上传"
            )
        if guard is not None:
            guard(real_sha, written)
        dedup = storage.blob_exists(real_sha, written)
        storage.commit_tmp(tmp, real_sha)
    finally:
        tmp.unlink(missing_ok=True)

    node = ns.create_file(
        db,
        user,
        session.target_node_id,
        session.filename,
        real_sha,
        written,
        session.mime,
        quick=session.quick_owned,
    )
    if not session.quick_owned:
        ns.recalc_usage(db, user)

    session.status = "completed"
    session.sha256 = real_sha
    session.updated_at = utcnow()
    db.execute(delete(UploadChunk).where(UploadChunk.session_id == session.id))
    storage.drop_session_dir(session.id)
    return {"node": ns.node_payload(node), "sha256": real_sha, "size": written, "deduplicated": dedup}


def abort_upload(db: Session, user_id: int, session_id: str) -> dict:
    session = get_session(db, user_id=user_id, session_id=session_id, allow_done=True)
    if session.status == "completed":
        return {"aborted": False, "reason": "already_completed"}
    session.status = "aborted"
    session.updated_at = utcnow()
    db.execute(delete(UploadChunk).where(UploadChunk.session_id == session.id))
    storage.drop_session_dir(session.id)
    return {"aborted": True}


# --------------------------------------------------------------------------- #
# 维护
# --------------------------------------------------------------------------- #
def gc_stale_sessions(db: Session, ttl_hours: int | None = None) -> int:
    """清掉超时未动的会话与其分片目录（服务器重启时调用）。"""
    hours = ttl_hours if ttl_hours is not None else settings.UPLOAD_SESSION_TTL_HOURS
    if hours <= 0:
        return 0
    cutoff = utcnow() - timedelta(hours=hours)
    rows = db.scalars(
        select(UploadSession).where(
            UploadSession.status == "uploading", UploadSession.updated_at < cutoff
        )
    ).all()
    for session in rows:
        storage.drop_session_dir(session.id)
        db.execute(delete(UploadChunk).where(UploadChunk.session_id == session.id))
        session.status = "aborted"
    # 目录里可能还有早已删掉会话的残留分片
    if settings.chunk_dir.is_dir():
        known = {s.id for s in db.scalars(select(UploadSession)).all() if s.status == "uploading"}
        for child in settings.chunk_dir.iterdir():
            if child.is_dir() and child.name not in known:
                storage.drop_session_dir(child.name)
    return len(rows)
