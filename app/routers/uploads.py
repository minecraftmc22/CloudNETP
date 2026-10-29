"""分片上传路由：init / status / chunk / complete / abort。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from .. import storage
from .. import upload_service as us
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import UploadInitIn
from ..security import check_origin, normalize_name
from ..user_service import log_action

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

MAX_BODY = 32 * 1024 * 1024  # 与 upload_service.MAX_CHUNK 对齐


@router.post("/init")
def init_upload(payload: UploadInitIn, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    result = us.init_upload(
        db,
        user,
        name=normalize_name(payload.name),
        size=payload.size,
        parent_id=payload.parent_id,
        sha256=payload.sha256,
        mime=payload.mime,
        chunk_size=payload.chunk_size,
    )
    if result.get("instant"):
        log_action(
            db,
            "upload_instant",
            user.id,
            {"name": payload.name, "size": payload.size, "sha256": (payload.sha256 or "")[:16]},
            request,
        )
    db.commit()
    return result


@router.get("/{session_id}")
def upload_status(session_id: str, db: DbSession, user: CurrentUser) -> dict:
    session = us.get_session(db, user.id, session_id)
    return us.session_payload(db, session)


@router.put("/{session_id}/{index}")
async def put_chunk(
    session_id: str, index: int, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "分片过大")
    body = await request.body()
    if not body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "分片内容为空")
    result = us.save_chunk(db, user.id, session_id, index, body)
    db.commit()
    return result


@router.post("/{session_id}/complete")
def complete_upload(
    session_id: str,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    sha256: str | None = Query(default=None, min_length=64, max_length=64),
) -> dict:
    check_origin(request)
    result = us.complete_upload(db, user, session_id, sha256)
    log_action(
        db,
        "node_upload",
        user.id,
        {
            "name": result["node"]["name"],
            "size": result["size"],
            "dedup": result["deduplicated"],
            "chunked": True,
        },
        request,
    )
    db.commit()
    return result


@router.delete("/{session_id}")
def abort_upload(session_id: str, request: Request, db: DbSession, user: CurrentUser) -> dict:
    check_origin(request)
    result = us.abort_upload(db, user.id, session_id)
    db.commit()
    return result


@router.get("/{session_id}/probe", include_in_schema=False)
def probe_chunk(
    session_id: str,
    db: DbSession,
    user: CurrentUser,
    index: int = Query(ge=0),
) -> dict:
    """调试用：确认某分片是否已落盘。"""
    session = us.get_session(db, user.id, session_id)
    want = us.expected_chunk_size(session, index) if index < session.total_chunks else 0
    present = storage.has_chunk(session.id, index, want) if want else False
    return {"index": index, "present": present, "expected": want}
