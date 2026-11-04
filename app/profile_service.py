"""个人资料：昵称/简介校验、头像规格化落盘、分享页展示名脱敏。

头像不存数据库（BLOB 会把 users 表拖胖），只存文件名：
    storage/avatars/<user_id>-<随机 12 位>.webp
文件名带随机后缀 → 换头像即换 URL，可以放心下长缓存。
"""

from __future__ import annotations

import io
import re
import secrets
from pathlib import Path

from PIL import Image, ImageOps

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
