"""个人主页：自己的资料维护、他人名片、公开分享页。

与 ``auth.py`` 的分工：名称 / 简介 / 头像 / 背景图仍在那边（登录链路的一部分），
这里只管「扩展资料」（性别、生日、各种社交账号与各自的可见开关）和分享。

可见性只在服务端过滤一次：非本人视角下，没打开的字段根本不下发，
避免前端拿到数据后忘记隐藏、或者被随手改个请求参数就看到别人藏起来的信息。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import profile_service as ps
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import ProfileDetailIn
from ..security import check_origin

router = APIRouter(prefix="/api", tags=["profile"])

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


# --------------------------------------------------------------------------- #
# 自己的资料
# --------------------------------------------------------------------------- #
@router.get("/me/profile/detail")
def my_profile_detail(db: DbSession, user: CurrentUser) -> dict:
    return {"profile": ps.editable(db, user), "kinds": list(ps.PROFILE_SHOW_KEYS)}


@router.patch("/me/profile/detail")
def save_profile_detail(
    payload: ProfileDetailIn, request: Request, db: DbSession, user: CurrentUser
) -> dict:
    check_origin(request)
    try:
        ps.update_profile(db, user, payload.model_dump(exclude_none=True))
    except ValueError as exc:
        reason = str(exc)
        message = {
            "gender": "性别取值不合法",
            "birthday": "出生日期格式应为 YYYY-MM-DD，且不能是未来",
            "link-too-long": f"每一项最多 {ps.LINK_MAX} 个字符",
        }.get(reason, "资料格式不对")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, message) from None
    db.commit()
    return {"profile": ps.editable(db, user)}


@router.post("/me/profile/share")
def toggle_share(payload: dict, request: Request, db: DbSession, user: CurrentUser) -> dict:
    """开启 / 关闭公开主页。``enabled=false`` 时旧链接立即失效。"""
    check_origin(request)
    enabled = bool((payload or {}).get("enabled", True))
    if enabled:
        token = ps.ensure_share_token(db, user)
        db.commit()
        return {"enabled": True, "token": token, "path": f"/u/{token}"}
    ps.drop_share_token(db, user)
    db.commit()
    return {"enabled": False, "token": None, "path": None}


# --------------------------------------------------------------------------- #
# 看别人的资料（登录后：名片浮层 / 个人主页）
# --------------------------------------------------------------------------- #
@router.get("/users/{user_id}/card")
def user_card_by_id(user_id: int, db: DbSession, user: CurrentUser) -> dict:
    target = db.get(User, int(user_id))
    if not target or target.banned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    alias = friend_alias(db, user.id, target.id)
    data = ps.profile_payload(db, target, viewer_id=user.id, alias=alias)
    # 名片只需要摘要，链接列表在主页里再看；头像 / 关系 / 备注足够拼浮层
    data["bio"] = (data.get("bio") or "")[:120]
    data["links"] = data["links"][:3]
    return {"card": data}


def friend_alias(db: Session, me_id: int, other_id: int) -> str:
    """我对这位好友的备注（不是好友就空串）。群成员备注走 chat_service。"""
    from ..models import Friendship
    from ..friend_service import _pair
    from sqlalchemy import select

    row = db.scalar(select(Friendship).where(_pair(me_id, other_id)).limit(1))
    if not row or row.status != "accepted":
        return ""
    return (row.requester_alias if row.requester_id == me_id else row.addressee_alias) or ""


@router.get("/users/{user_id}/profile")
def user_profile(user_id: int, db: DbSession, user: CurrentUser) -> dict:
    target = db.get(User, int(user_id))
    if not target or target.banned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    alias = friend_alias(db, user.id, target.id)
    return {"profile": ps.profile_payload(db, target, viewer_id=user.id, alias=alias)}


# --------------------------------------------------------------------------- #
# 公开分享页（不需要登录）
# --------------------------------------------------------------------------- #
@router.get("/public/profile/{token}")
def public_profile(token: str, db: DbSession) -> dict:
    target = ps.by_token(db, token)
    if not target or target.banned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "主页不存在或分享已关闭")
    return {"profile": ps.profile_payload(db, target, public=True)}


@router.get("/public/groups/{token}")
def public_group(token: str, db: DbSession) -> dict:
    """群邀请链接的摘要（不需要登录）：只给名字/头像/简介/人数，进群还得登录。"""
    from .. import group_service as gs

    conv = gs.find_by_token(db, token)
    return {"group": gs.public_preview(db, conv)}


@router.get("/public/groups/{token}/avatar")
def public_group_avatar(token: str, db: DbSession):
    from .. import group_service as gs

    conv = gs.find_by_token(db, token)
    if not conv.avatar:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "头像不存在")
    path = gs.avatar_path(conv.avatar)
    if not path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "头像不存在")
    return FileResponse(
        path,
        media_type=gs.avatar_media_type(conv.avatar),
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/public/profile/{token}/avatar")
def public_avatar(token: str, db: DbSession):
    """公开主页的头像：只有开了分享的用户才能从这里取到，避免变成任意头像的来源。"""
    target = ps.by_token(db, token)
    if not target or not target.avatar:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "头像不存在")
    path = ps.avatar_path(target.avatar)
    if not path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "头像不存在")
    return FileResponse(
        path,
        media_type=ps.avatar_media_type(target.avatar),
        headers={"Cache-Control": "public, max-age=86400"},
    )
