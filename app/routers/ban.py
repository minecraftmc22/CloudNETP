"""封禁申诉：被ban用户提交、管理员审核。

被ban的人拿不到正常会话（`get_current_user` 会 403），所以这里单独认三种身份：

1. 登录被拒时下发的短期票据（query / body / cookie 均可）；
2. 还在线的封禁会话 cookie —— 弹窗点「去申诉」时用这个；
3. 管理员的正常会话 —— 只用于审核接口。
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import ban_service
from .. import notice_service
from ..config import settings
from ..db import get_db
from ..deps import require_admin
from ..models import BanAppeal, User, utcnow
from ..schemas import AdminAppealReviewIn, BanAppealIn
from ..security import check_origin, decode_token, verify_ban_ticket
from ..user_service import iso, log_action

router = APIRouter(prefix="/api", tags=["ban"])

BAN_COOKIE = ban_service.BAN_COOKIE
MAX_MESSAGE = 1000
# 同一账号 24 小时内最多提交几条，防止刷后台
APPEAL_PER_DAY = 3


def _from_ticket(db: Session, ticket: str) -> User | None:
    found = verify_ban_ticket(ticket.strip()) if ticket else None
    if not found:
        return None
    uid, stamp = found
    user = db.get(User, uid)
    if user is None:
        return None
    # 改过密码 / 被强制下线后旧票据就作废了
    if str(int(user.token_version)) != stamp:
        return None
    return user


def _from_session(request: Request, db: Session) -> User | None:
    token = request.cookies.get(settings.COOKIE_NAME)
    payload = decode_token(token) if token else None
    if not payload:
        return None
    user = db.get(User, int(payload.get("u", 0)))
    if user is None or int(payload.get("v", 0)) != user.token_version:
        return None
    return user


def _resolve(request: Request, db: Session, ticket: str = "") -> User:
    """票据优先，其次会话；都不行就 401。"""
    ticket = ticket or request.cookies.get(BAN_COOKIE, "")
    user = _from_ticket(db, ticket) or _from_session(request, db)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "登录状态已失效，请重新登录后再试"
        )
    return user


@router.get("/ban/info")
def ban_info(
    request: Request, db: Session = Depends(get_db), ticket: str = Query(default="")
) -> dict:
    """封禁详情 + 最近一条申诉；已解封也能查（回来看审核结果）。"""
    user = _resolve(request, db, ticket)
    if user.banned and ban_service.lift_expired(db, user):
        log_action(db, "ban_expired", user.id, {"reason": "到期自动解封"}, request)
        db.commit()
    return {
        "username": user.username,
        "ban": ban_service.info(user),
        "appeal": ban_service.appeal_dict(ban_service.latest_appeal(db, user.id)),
    }


@router.post("/ban/appeal")
def submit_appeal(
    payload: BanAppealIn,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    check_origin(request)
    user = _resolve(request, db, payload.ticket or "")
    message = (payload.message or "").strip()
    if not message:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "申诉内容不能为空")
    if len(message) > MAX_MESSAGE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "申诉内容太长（最多 1000 字）")

    # 到期了就直接放行，没必要再走审核
    if ban_service.lift_expired(db, user):
        log_action(db, "ban_expired", user.id, {"reason": "到期自动解封"}, request)
        db.commit()
        return {"ok": True, "unbanned": True, "appeal": None}
    if not user.banned:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "当前账号没有被封禁，不用申诉")

    if ban_service.pending_appeal(db, user.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "已经提交过申诉，正在等待管理员审核")

    since = utcnow() - timedelta(hours=24)
    recent = int(
        db.scalar(
            select(func.count())
            .select_from(BanAppeal)
            .where(BanAppeal.user_id == user.id, BanAppeal.created_at >= since)
        )
        or 0
    )
    if recent >= APPEAL_PER_DAY:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "今天提交的申诉太多了，明天再来吧")

    row = BanAppeal(
        user_id=user.id,
        message=message,
        status="pending",
        # 留个快照：通过后封禁原因会被清空
        ban_reason=user.ban_reason,
        ban_until=user.ban_until,
        created_at=utcnow(),
    )
    db.add(row)
    db.flush()
    notice_service.notify_admins(
        db,
        "appeal",
        "新的封禁申诉待审核",
        (message or "").strip()[:120],
        "#/admin/appeals",
    )
    log_action(db, "ban_appeal_submit", user.id, {"length": len(message)}, request)
    db.commit()
    db.refresh(row)
    return {"ok": True, "unbanned": False, "appeal": ban_service.appeal_dict(row)}


@router.post("/ban/logout")
def ban_logout(request: Request, response: Response) -> dict:
    """被ban状态下的退出：正常登出接口会 403，这里只负责清 cookie。"""
    check_origin(request)
    response.delete_cookie(settings.COOKIE_NAME, path="/")
    response.delete_cookie(BAN_COOKIE, path="/")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 管理后台：申诉审核
# --------------------------------------------------------------------------- #
@router.get("/admin/appeals")
def list_appeals(
    db: Session = Depends(get_db),
    me: User = Depends(require_admin),
    status_filter: str = Query(default="pending", alias="status", pattern="^(pending|approved|rejected|all)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="pageSize"),
) -> dict:
    conds = []
    if status_filter != "all":
        conds.append(BanAppeal.status == status_filter)
    total = int(
        db.scalar(select(func.count()).select_from(BanAppeal).where(*conds)) or 0
    )
    rows = db.execute(
        select(BanAppeal, User)
        .join(User, User.id == BanAppeal.user_id)
        .where(*conds)
        .order_by(BanAppeal.created_at.desc(), BanAppeal.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    items = []
    for row, user in rows:
        items.append(
            {
                "id": row.id,
                "message": row.message,
                "status": row.status,
                "reply": row.reply or "",
                "banReason": row.ban_reason or "",
                "banUntil": iso(row.ban_until),
                "createdAt": iso(row.created_at),
                "handledAt": iso(row.handled_at),
                "userId": user.id,
                "username": user.username,
                "displayName": user.display_name or user.username,
                "role": user.role,
                "isSuper": bool(user.is_super),
                "banned": bool(user.banned),
                "currentBan": ban_service.info(user) if user.banned else None,
            }
        )
    return {"items": items, "total": total, "page": page, "pageSize": page_size}


@router.post("/admin/appeals/{appeal_id}/review")
def review_appeal(
    appeal_id: int,
    payload: AdminAppealReviewIn,
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(require_admin),
) -> dict:
    check_origin(request)
    row = db.get(BanAppeal, appeal_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "申诉不存在")
    if row.status != "pending":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "这条申诉已经处理过了")
    target = db.get(User, row.user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "账号不存在")
    # 管理员成员归超管管：普通管理员不能通过别人的申诉把人放回来
    if target.role == 1 and not me.is_super:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有超级管理员能处理管理员的申诉")

    reply = (payload.reply or "").strip()[:200]
    row.status = "approved" if payload.approve else "rejected"
    row.reply = reply or None
    row.handled_by = me.id
    row.handled_at = utcnow()
    if payload.approve:
        ban_service.clear_ban(db, target)
        # 不踢会话：正在申诉页的本人下一次轮询就能直接回到网盘
    log_action(
        db,
        "admin_appeal_approve" if payload.approve else "admin_appeal_reject",
        me.id,
        {"target": target.username, "appealId": row.id},
        request,
    )
    db.commit()
    return {
        "ok": True,
        "status": row.status,
        "appeal": ban_service.appeal_dict(row),
        "banned": bool(target.banned),
    }
