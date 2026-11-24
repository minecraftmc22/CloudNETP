"""个人资料：昵称/简介校验、头像规格化落盘、分享页展示名脱敏。

头像不存数据库（BLOB 会把 users 表拖胖），只存文件名：
    storage/avatars/<user_id>-<随机 12 位>.webp
文件名带随机后缀 → 换头像即换 URL，可以放心下长缓存。
"""

from __future__ import annotations

import io
import re
import secrets
from datetime import date, datetime
from pathlib import Path

from PIL import Image, ImageOps
from sqlalchemy import select

from .config import settings
from .models import User, UserSetting

AVATAR_SIDE = 256  # 统一裁成 256×256 方图，前端按 24~96px 显示都够
AVATAR_NAME_RE = re.compile(r"^([1-9]\d{0,17})-[0-9a-f]{12}\.(webp|png)$")
AVATAR_MAX_BYTES = 4 * 1024 * 1024  # 前端会先压到 256px，这里只防超大原图

BIO_MAX = 200
DISPLAY_NAME_MAX = 32
_MASK_KEEP = 2  # 关闭"显示完整昵称"时保留的字符数


# --------------------------------------------------------------------------- #
# 展示名
# --------------------------------------------------------------------------- #
def display_name_of(user: User | None) -> str:
    if user is None:
        return "未知用户"
    return (user.display_name or "").strip() or user.username


def mask_name(name: str) -> str:
    """分享页隐私模式：只保留前两个字符。"""
    text = (name or "").strip()
    return text[:_MASK_KEEP] if len(text) > _MASK_KEEP else text


def share_show_name(db, user_id: int) -> bool:
    """该用户是否允许在分享页露出完整昵称（默认关闭）。"""
    row = db.get(UserSetting, user_id)
    return bool(row and row.share_show_name)


def author_payload(db, share, user: User | None = None) -> dict:
    """公开分享页的分享者信息：昵称按用户设置脱敏，头像跟随同一开关。"""
    from .models import User as _User

    owner = user or db.get(_User, share.user_id)
    full = share_show_name(db, share.user_id)
    name = display_name_of(owner)
    has_avatar = bool(owner and owner.avatar)
    return {
        "name": name if full else mask_name(name),
        "masked": not full,
        "avatar": f"/api/public/shares/{share.token}/avatar" if (full and has_avatar) else None,
    }


# --------------------------------------------------------------------------- #
# 头像文件
# --------------------------------------------------------------------------- #
def avatar_path(name: str | None) -> Path | None:
    """把数据库里的文件名还原成磁盘路径；名字不合规一律当不存在。"""
    if not name or not AVATAR_NAME_RE.fullmatch(name):
        return None
    path = settings.avatar_dir / name
    return path if path.is_file() else None


def avatar_media_type(name: str) -> str:
    return "image/png" if name.endswith(".png") else "image/webp"


def _normalize(raw: bytes) -> tuple[bytes, str]:
    """裁成正方形并缩到 256px，统一转 webp（失败退回 png）。"""
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
        except Exception:  # Pillow 编译时没带 webp 支持
            buf = io.BytesIO()
            img.save(buf, "PNG", optimize=True)
            return buf.getvalue(), "png"


def save_avatar(user_id: int, raw: bytes) -> str:
    """校验 + 规格化 + 落盘，返回新文件名。调用方负责删除旧文件。"""
    if not raw:
        raise ValueError("empty")
    if len(raw) > AVATAR_MAX_BYTES:
        raise ValueError("too-large")

    data, ext = _normalize(raw)  # 图片损坏时抛 UnidentifiedImageError
    settings.avatar_dir.mkdir(parents=True, exist_ok=True)
    name = f"{user_id}-{secrets.token_hex(6)}.{ext}"
    target = settings.avatar_dir / name
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    return name


def drop_avatar(name: str | None) -> None:
    path = avatar_path(name)
    if path:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 自定义背景图
# --------------------------------------------------------------------------- #
BACKGROUND_NAME_RE = re.compile(r"^([1-9]\d{0,17})-[0-9a-f]{12}\.(webp|png)$")
BACKGROUND_MAX_BYTES = 16 * 1024 * 1024
BACKGROUND_SIDE = 2560  # 长边上限：4K 屏铺满也不糊，文件还压得住
BACKGROUND_QUALITY = 84


def background_path(name: str | None) -> Path | None:
    if not name or not BACKGROUND_NAME_RE.fullmatch(name):
        return None
    path = settings.background_dir / name
    return path if path.is_file() else None


def background_media_type(name: str) -> str:
    return "image/png" if name.endswith(".png") else "image/webp"


def _fit_background(raw: bytes) -> tuple[bytes, str]:
    """等比缩放到长边 2560 并转 webp（不裁切、保留透明通道）。"""
    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "transparency" in img.info or "A" in img.mode else "RGB")

        w, h = img.size
        long_side = max(w, h)
        if long_side > BACKGROUND_SIDE:
            scale = BACKGROUND_SIDE / long_side
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)

        buf = io.BytesIO()
        try:
            img.save(buf, "WEBP", quality=BACKGROUND_QUALITY, method=4)
            return buf.getvalue(), "webp"
        except Exception:  # Pillow 没编 webp
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "PNG", optimize=True)
            return buf.getvalue(), "png"


def save_background(user_id: int, raw: bytes) -> str:
    """校验 + 缩放 + 落盘，返回新文件名。调用方负责删除旧文件。"""
    if not raw:
        raise ValueError("empty")
    if len(raw) > BACKGROUND_MAX_BYTES:
        raise ValueError("too-large")

    data, ext = _fit_background(raw)
    settings.background_dir.mkdir(parents=True, exist_ok=True)
    name = f"{user_id}-{secrets.token_hex(6)}.{ext}"
    target = settings.background_dir / name
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    return name


def drop_background(name: str | None) -> None:
    path = background_path(name)
    if path:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 个人主页扩展资料
# --------------------------------------------------------------------------- #
# 除名称（display_name）与简介（bio）外，其余字段都可单独控制是否展示。
# 顺序即前端渲染顺序；键名同时用作 i18n key 后缀。
PROFILE_LINK_KEYS = (
    "email",
    "qq",
    "wechat",
    "x",
    "line",
    "google",
    "discord",
    "youtube",
    "netease",
    "spotify",
    "website",
    "weibo",
    "github",
    "steam",
    "bilibili",
    "douyin",
    "facebook",
    "instagram",
)
PROFILE_SHOW_KEYS = PROFILE_LINK_KEYS + ("gender", "birthday")
GENDERS = ("", "male", "female", "other")
LINK_MAX = 120
# 默认可见：社交账号填了就是想让人看到；生日 / 性别 / 邮箱偏隐私，默认不展示
_DEFAULT_HIDDEN = frozenset({"email", "gender", "birthday"})


def _blank() -> dict:
    return {"gender": "", "birthday": "", "links": {}, "show": {}}


def normalized(raw: dict | None) -> dict:
    """把库里的 JSON 补成结构完整的字典（老数据可能缺字段或类型不对）。"""
    data = _blank()
    if not isinstance(raw, dict):
        return data
    gender = str(raw.get("gender") or "").strip().lower()
    data["gender"] = gender if gender in GENDERS else ""
    birthday = str(raw.get("birthday") or "").strip()
    data["birthday"] = birthday[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", birthday[:10]) else ""
    links = raw.get("links") if isinstance(raw.get("links"), dict) else {}
    show = raw.get("show") if isinstance(raw.get("show"), dict) else {}
    for key in PROFILE_LINK_KEYS:
        value = str(links.get(key) or "").strip()[:LINK_MAX]
        if value:
            data["links"][key] = value
        flag = show.get(key)
        data["show"][key] = bool(flag) if isinstance(flag, bool) else (key not in _DEFAULT_HIDDEN)
    for key in ("gender", "birthday"):
        flag = show.get(key)
        data["show"][key] = bool(flag) if isinstance(flag, bool) else False
    return data


def _safe_url(value: str) -> str | None:
    """只有明确写了 http(s) 的才转成可点链接，避免 javascript: / data: 之类的坑。"""
    text = (value or "").strip()
    if re.match(r"^https?://", text, re.I):
        return text
    return None


def age_of(birthday: str) -> int | None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", birthday or ""):
        return None
    try:
        born = date.fromisoformat(birthday)
    except ValueError:
        return None
    today = date.today()
    years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return years if 0 < years < 150 else None


def _relation(db, me_id: int | None, target_id: int) -> str:
    if me_id is None:
        return "guest"
    if me_id == target_id:
        return "self"
    from .friend_service import _pair
    from .models import Friendship

    row = db.scalar(select(Friendship).where(_pair(me_id, target_id)).limit(1))
    if row:
        if row.status == "accepted":
            return "friends"
        return "pending_out" if row.requester_id == me_id else "pending_in"
    from .models import ChatBlock

    if db.get(ChatBlock, {"blocker_id": me_id, "blocked_id": target_id}) is not None:
        return "blocked"
    return "none"


def profile_payload(
    db,
    user: User,
    *,
    viewer_id: int | None = None,
    public: bool = False,
    alias: str = "",
) -> dict:
    """个人主页数据。

    - ``public=True``（分享页）：不返回 id / username / 关系，头像走公开路由
    - 非本人视角：按 show 过滤掉未公开的字段
    - 本人视角：全部返回，供编辑回填
    """
    is_self = viewer_id is not None and viewer_id == user.id
    data = normalized(user.profile)
    name = display_name_of(user)
    has_avatar = bool(user.avatar)

    if public:
        avatar_url = f"/api/public/profile/{user.profile_token}/avatar" if has_avatar else None
    elif has_avatar:
        avatar_url = f"/api/avatar/{user.avatar}"
    else:
        avatar_url = None

    fields: dict = {}
    if is_self or data["show"].get("gender"):
        fields["gender"] = data["gender"] or None
    if is_self or data["show"].get("birthday"):
        fields["birthday"] = data["birthday"] or None
        fields["age"] = age_of(data["birthday"])

    links: list[dict] = []
    for key in PROFILE_LINK_KEYS:
        value = data["links"].get(key) or ""
        if not value:
            continue
        if not (is_self or data["show"].get(key)):
            continue
        # 邮箱只有绑定了才有意义；未验证的也照常显示（用户自己填的可见开关）
        if key == "email":
            value = (user.email or "").strip()
            if not value:
                continue
        links.append({"key": key, "value": value, "url": _safe_url(value)})

    payload = {
        "displayName": name,
        "bio": user.bio or "",
        "avatarUrl": avatar_url,
        "gender": fields.get("gender"),
        "birthday": fields.get("birthday"),
        "age": fields.get("age"),
        "links": links,
        "isSelf": is_self,
    }
    if alias:
        payload["alias"] = alias
    if not public:
        payload["id"] = user.id
        payload["username"] = user.username
        payload["role"] = user.role
        payload["relation"] = _relation(db, viewer_id, user.id)
        payload["joinDays"] = max(0, (datetime.utcnow() - (user.created_at or datetime.utcnow())).days)
    else:
        payload["relation"] = "guest"
    return payload


def editable(db, user: User) -> dict:
    """编辑弹窗回填：原样给出所有字段与开关，不做可见性过滤。"""
    data = normalized(user.profile)
    return {
        "displayName": user.display_name or "",
        "bio": user.bio or "",
        "gender": data["gender"],
        "birthday": data["birthday"],
        "links": {key: data["links"].get(key, "") for key in PROFILE_LINK_KEYS},
        "show": {key: bool(data["show"].get(key)) for key in PROFILE_SHOW_KEYS},
        "email": user.email or "",
        "avatarUrl": f"/api/avatar/{user.avatar}" if user.avatar else None,
        "shareToken": user.profile_token,
    }


def update_profile(db, user: User, payload: dict) -> dict:
    """写入扩展资料。只接受白名单字段，超长/非法值直接拒绝。"""
    data = normalized(user.profile)
    if "gender" in payload and payload["gender"] is not None:
        gender = str(payload["gender"]).strip().lower()
        if gender not in GENDERS:
            raise ValueError("gender")
        data["gender"] = gender
    if "birthday" in payload and payload["birthday"] is not None:
        birthday = str(payload["birthday"]).strip()
        if birthday:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", birthday):
                raise ValueError("birthday")
            try:
                born = date.fromisoformat(birthday)
            except ValueError:
                raise ValueError("birthday") from None
            if born > date.today() or born.year < 1900:
                raise ValueError("birthday")
        data["birthday"] = birthday
    links = payload.get("links")
    if isinstance(links, dict):
        for key, value in links.items():
            if key not in PROFILE_LINK_KEYS:
                continue
            text = str(value or "").strip()
            if len(text) > LINK_MAX:
                raise ValueError("link-too-long")
            if text:
                data["links"][key] = text
            else:
                data["links"].pop(key, None)
    show = payload.get("show")
    if isinstance(show, dict):
        for key, value in show.items():
            if key in PROFILE_SHOW_KEYS:
                data["show"][key] = bool(value)
    user.profile = data
    return data


def ensure_share_token(db, user: User) -> str:
    """开启分享：没有 token 就生成一个（同用户复用，关掉再开也不换链接）。"""
    if user.profile_token:
        return user.profile_token
    from .models import User as _User

    for _ in range(8):
        token = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:18]
        if db.scalar(select(_User.id).where(_User.profile_token == token).limit(1)) is None:
            user.profile_token = token
            return token
    raise RuntimeError("无法生成分享标识")


def drop_share_token(db, user: User) -> None:
    user.profile_token = None


def by_token(db, token: str) -> User | None:
    if not token or not re.fullmatch(r"[A-Za-z0-9]{6,24}", token):
        return None
    from .models import User as _User

    return db.scalar(select(_User).where(_User.profile_token == token).limit(1))

