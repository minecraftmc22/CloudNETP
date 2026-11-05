"""版本检查与更新接口：检查只读；更新需要管理员且工作区干净。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from .. import update_service
from ..db import get_db
from ..deps import get_current_user, require_admin
from ..models import User
from ..schemas import UpdateApplyIn
from ..security import check_origin
from ..user_service import log_action

router = APIRouter(prefix="/api", tags=["update"])

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]


@router.get("/version/check")
def check_version(user: CurrentUser, force: int = Query(default=0, ge=0, le=1)) -> dict:
    """普通用户读缓存；管理员可以 force=1 立即刷新（30 秒冷却）。"""
    return update_service.check(force=bool(force) and user.role == 1)


@router.post("/admin/update/apply")
def apply_update(
    payload: UpdateApplyIn,
    request: Request,
    db: DbSession,
    me: AdminUser,
) -> dict:
    """手动更新：git fetch + merge --ff-only，成功后重启服务。"""
    check_origin(request)
    result = update_service.apply_update(restart=payload.restart, trigger="manual")
    log_action(db, "admin_update_apply", me.id, result, request)
    db.commit()
    return result
