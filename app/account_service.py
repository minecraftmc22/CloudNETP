"""注销账号：把一个人留下的所有痕迹一次清干净。

管理员删用户走的是 ``admin_service.purge_user``（节点 / 分享 / 上传会话 / 设置 / 头像），
自己注销还要多清几样：长期对话、AI 历史会话、快传与取件码、歌词偏好、意见反馈、封禁申诉，
以及指向这些内容的举报记录。blob 的引用计数由 ``purge_user`` 里的 ``release_blob`` 负责，
引用归零时物理文件才会真正删掉——别人还在用的同一份内容不会受影响。
"""

from __future__ import annotations

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from . import admin_service, profile_service
from .models import (
    AiChatMessage,
    AiChatSession,
    AudioPref,
    BanAppeal,
    Feedback,
    Node,
    QuickPickupItem,
    QuickTransfer,
    Share,
    ShareReport,
    User,
)


def _count(db: Session, stmt) -> int:
    return int(db.scalar(stmt) or 0)


def purge_account(db: Session, user: User, *, drop_avatar_file: bool = True) -> dict:
    """删除该账号及其全部数据，返回删掉了多少东西（给审计和前端提示用）。"""
    uid = user.id
    avatar = user.avatar

    node_ids = select(Node.id).where(Node.user_id == uid)
    share_ids = select(Share.id).where(Share.user_id == uid)
    # 站内快传的接收方也算「跟他有关」：不清掉，对方列表里会留一条指向不存在用户的消息
    quick_ids = select(QuickTransfer.id).where(
        or_(QuickTransfer.owner_id == uid, QuickTransfer.peer_id == uid)
    )
    session_ids = select(AiChatSession.id).where(AiChatSession.user_id == uid)

    counts = {
        "nodes": _count(db, select(func.count()).select_from(Node).where(Node.user_id == uid)),
        "shares": _count(db, select(func.count()).select_from(Share).where(Share.user_id == uid)),
        "chats": _count(
            db, select(func.count()).select_from(AiChatSession).where(AiChatSession.user_id == uid)
        ),
        "quick": _count(
            db,
            select(func.count())
            .select_from(QuickTransfer)
            .where(or_(QuickTransfer.owner_id == uid, QuickTransfer.peer_id == uid)),
        ),
        "feedback": _count(
            db, select(func.count()).select_from(Feedback).where(Feedback.user_id == uid)
        ),
    }

    # 举报记录先清：share_id / quick_id 都不是外键，留着会指向空气
    db.execute(delete(ShareReport).where(ShareReport.share_id.in_(share_ids)))
    db.execute(delete(ShareReport).where(ShareReport.quick_id.in_(quick_ids)))
    # 快传与取件项（取件项没有外键，必须显式删）
    db.execute(delete(QuickPickupItem).where(QuickPickupItem.quick_id.in_(quick_ids)))
    db.execute(delete(QuickTransfer).where(QuickTransfer.id.in_(quick_ids)))
    # 看板娘：消息 → 会话；长期记忆存在 user_settings 里，跟着设置一起没
    db.execute(delete(AiChatMessage).where(AiChatMessage.session_id.in_(session_ids)))
    db.execute(delete(AiChatSession).where(AiChatSession.user_id == uid))
    # 每首歌的歌词偏好挂在节点上
    db.execute(delete(AudioPref).where(AudioPref.node_id.in_(node_ids)))
    # 反馈与申诉属于账号数据，一并删除
    db.execute(delete(Feedback).where(Feedback.user_id == uid))
    db.execute(delete(BanAppeal).where(BanAppeal.user_id == uid))

    # 节点 / 分享 / 上传会话 / 设置 / 用户本身 + blob 引用归零
    result = admin_service.purge_user(db, user)
    if drop_avatar_file:
        profile_service.drop_avatar(avatar)
    return {**result, "chats": counts["chats"], "quick": counts["quick"], "feedback": counts["feedback"]}
