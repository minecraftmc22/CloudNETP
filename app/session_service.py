"""登录设备（会话）管理：解析 UA、签发会话、下线单个 / 其它设备。

会话模型见 ``models.LoginSession``。要点：

- token 里带 `s`（sid），``get_current_user`` 每次请求查这一行——被撤销的
  会话下一次请求立刻 401，不需要等过期。
- ``last_seen_at`` 节流一分钟，聊天页 3 秒轮询也不会高频写库。
- 解析 UA 只求「够看的展示名」，不做浏览器指纹；解析不出来就显示原始串。
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import LoginSession, User, utcnow
from .security import client_ip
from .user_service import iso

UA_MAX = 255
SEEN_INTERVAL = 60  # last_seen 写库节流（秒）


def parse_device(ua: str) -> str:
    """UA → 「Windows · Chrome」这样的展示名。顺序敏感：Edg 必须先于 Chrome。"""
    lower = (ua or "").lower()
    if not lower:
        return "未知设备"
    if "windows" in lower:
        os_name = "Windows"
    elif "iphone" in lower:
        os_name = "iPhone"
    elif "ipad" in lower:
        os_name = "iPad"
    elif "android" in lower:
        os_name = "Android"
    elif "mac os" in lower or "macintosh" in lower:
        os_name = "macOS"
    elif "linux" in lower:
        os_name = "Linux"
    else:
        os_name = "未知设备"

    browser = ""
    for key, name in (
        ("micromessenger", "微信内置"),
        ("edg/", "Edge"),
        ("opr/", "Opera"),
        ("chrome/", "Chrome"),
        ("firefox/", "Firefox"),
        ("safari/", "Safari"),
    ):
        if key in lower:
            browser = name
            break
    if not browser:
        browser = "其它浏览器"
    return f"{os_name} · {browser}"


def start_session(db: Session, user: User, request: Request) -> LoginSession:
    ua = (request.headers.get("user-agent") or "")[:UA_MAX]
    row = LoginSession(
        user_id=user.id,
        sid=secrets.token_urlsafe(12),
        device=parse_device(ua),
        ua=ua,
        ip=client_ip(request),
    )
    db.add(row)
    db.flush()
    return row


def find_by_sid(db: Session, sid: str) -> LoginSession | None:
    return db.scalar(select(LoginSession).where(LoginSession.sid == sid))


def touch(db: Session, row: LoginSession) -> bool:
    """最近活跃时间节流：一分钟最多写一次。返回是否真的写了（调用方决定 commit）。"""
    now = utcnow()
    if row.last_seen_at and now - row.last_seen_at < timedelta(seconds=SEEN_INTERVAL):
        return False
    row.last_seen_at = now
    return True


def revoke_all(db: Session, user_id: int) -> None:
    """改密码 / 换绑邮箱 / 被管理员踢时调用：全部会话标记下线。

    token_version 本身已经会拒绝旧 token，这里只是把设备列表同步成事实，
    避免列表里挂着早已登不进去的「幽灵设备」。
    """
    db.execute(
        update(LoginSession)
        .where(LoginSession.user_id == user_id, LoginSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


def revoke_sid(db: Session, user_id: int, sid: str) -> None:
    db.execute(
        update(LoginSession)
        .where(
            LoginSession.sid == sid,
            LoginSession.user_id == user_id,
            LoginSession.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )


def list_payload(db: Session, user: User, current_sid: str | None) -> dict[str, Any]:
    rows = list(
        db.scalars(
            select(LoginSession)
            .where(LoginSession.user_id == user.id, LoginSession.revoked_at.is_(None))
            .order_by(LoginSession.last_seen_at.desc())
        ).all()
    )
    items = [
        {
            "id": row.sid,
            "device": row.device,
            "ua": row.ua,
            "ip": row.ip,
            "current": bool(current_sid and row.sid == current_sid),
            "createdAt": iso(row.created_at),
            "lastSeenAt": iso(row.last_seen_at),
        }
        for row in rows
    ]
    # 当前设备排最前面，其余按最近活跃
    items.sort(key=lambda x: (not x["current"],))
    return {"items": items, "current": current_sid}


def revoke_one(db: Session, user: User, sid: str, current_sid: str | None) -> None:
    if sid == current_sid:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "当前设备要下线请直接退出登录"
        )
    row = db.scalar(
        select(LoginSession).where(
            LoginSession.sid == sid, LoginSession.user_id == user.id
        )
    )
    if not row or row.revoked_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在或已下线")
    row.revoked_at = utcnow()


def revoke_others(db: Session, user: User, current_sid: str | None) -> int:
    if not current_sid:
        # 旧式 token 没有 sid：把别的会话全下线（当前这个反正也管不到）
        result = db.execute(
            update(LoginSession)
            .where(LoginSession.user_id == user.id, LoginSession.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        return int(result.rowcount or 0)
    result = db.execute(
        update(LoginSession)
        .where(
            LoginSession.user_id == user.id,
            LoginSession.sid != current_sid,
            LoginSession.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )
    return int(result.rowcount or 0)
