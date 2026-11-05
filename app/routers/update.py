"""版本检查接口：只读远端信息，不执行更新动作。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from .. import update_service
from ..deps import get_current_user
from ..models import User

router = APIRouter(prefix="/api", tags=["update"])

CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("/version/check")
def check_version(user: CurrentUser, force: int = Query(default=0, ge=0, le=1)) -> dict:
    """普通用户读缓存；管理员可以 force=1 立即刷新（30 秒冷却）。"""
    return update_service.check(force=bool(force) and user.role == 1)
