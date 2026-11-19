"""主题色的小工具：色值规整、渐变配色、从背景图里取主色。

前端 ``static/js/theme.js`` 里有一套同口径的实现（``dominantColors`` /
``accentCompanion``），这里是给 AI 工具用的服务端版本 —— 用户说「按背景图
取色」时不需要把图发给模型，本地采样就够了：准、快、还不烧 token。
"""

from __future__ import annotations

import colorsys
import re
from pathlib import Path

from PIL import Image

DEFAULT_ACCENT = "#c084fc"
_HEX6 = re.compile(r"^#[0-9a-f]{6}$")


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


def normalize_hex(raw) -> str:
    """容忍少写 #、大小写、三位缩写；认不出来返回空串。"""
    text = str(raw or "").strip().lower().replace(" ", "")
    if not text:
        return ""
    if text.startswith("0x"):
        text = "#" + text[2:]
    if not text.startswith("#"):
        text = "#" + text
    if re.fullmatch(r"#[0-9a-f]{3}", text):
        text = "#" + "".join(ch * 2 for ch in text[1:])
    return text if _HEX6.match(text) else ""


def clean_colors(values, limit: int = 5) -> list[str]:
    """去重 + 校验 + 截断，保持传入顺序（渐变的顺序就是观感方向）。"""
    out: list[str] = []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return out
    for item in values:
        color = normalize_hex(item)
        if color and color not in out:
            out.append(color)
        if len(out) >= limit:
            break
    return out


def _rgb_to_hls(r: int, g: int, b: int) -> tuple[float, float, float]:
    """返回 (h 0-360, s 0-1, l 0-1)，跟前端 rgbToHsl 的输出顺序一致。"""
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    return h * 360, s, l


def _hsl_to_hex(h: float, s: float, l: float) -> str:
    r, g, b = colorsys.hls_to_rgb(h / 360 % 1.0, _clamp(l, 0, 1), _clamp(s, 0, 1))
    return "#{:02x}{:02x}{:02x}".format(
        round(r * 255), round(g * 255), round(b * 255)
    )


def companion(hex_color: str) -> str:
    """同色系的第二个颜色：色相挪一点、亮一点，单色也能拼出好看的渐变。"""
    color = normalize_hex(hex_color) or DEFAULT_ACCENT
    h, s, l = _rgb_to_hls(
        int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    )
    return _hsl_to_hex(h + 28, _clamp(s * 0.96, 0.5, 0.92), _clamp(l + 0.08, 0.5, 0.8))


def _hue_distance(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def image_colors(path: Path | str, limit: int = 3) -> list[str]:
    """从一张图里挑出适合当主题色的几种颜色（灰白黑不参与）。

    按色相聚类，再按「饱和度 × 明度是否接近中间调」加权；色相太接近的桶
    会合并，保证拿去当渐变时有区分度。整张图都偏低饱和时，退回出现最多的
    那一桶并强行提一点饱和度。
    """
    size = 72
    try:
        with Image.open(path) as img:
            img = img.convert("RGBA").resize((size, size), Image.BILINEAR)
            pixels = list(img.getdata())
    except Exception:  # 图坏了 / 不是图：交给调用方按「没有背景图」处理
        return []

    buckets: dict[int, list[float]] = {}
    plain: dict[tuple[int, int, int], int] = {}
    for r, g, b, a in pixels:
        if a < 128:
            continue
        key = (r >> 4, g >> 4, b >> 4)
        plain[key] = plain.get(key, 0) + 1

        h, s, l = _rgb_to_hls(r, g, b)
        if l < 0.1 or l > 0.94 or s < 0.14:
            continue
        index = min(23, int(h / 360 * 24))
        weight = s * (1 - abs(l - 0.56) * 1.1)
        if weight <= 0:
            continue
        bucket = buckets.setdefault(index, [0.0, 0.0, 0.0, 0.0])
        bucket[0] += weight
        bucket[1] += r * weight
        bucket[2] += g * weight
        bucket[3] += b * weight

    picked: list[tuple[float, str]] = []
    for _, bucket in sorted(buckets.items(), key=lambda kv: -kv[1][0]):
        if len(picked) >= limit:
            break
        w, r, g, b = bucket
        if w <= 0:
            continue
        h, s, l = _rgb_to_hls(round(r / w), round(g / w), round(b / w))
        if any(_hue_distance(prev[0], h) < 26 for prev in picked):
            continue
        picked.append((h, _hsl_to_hex(h, _clamp(s, 0.62, 0.9), _clamp(l, 0.62, 0.74))))
    if picked:
        return [color for _, color in picked]

    if not plain:
        return []
    r, g, b = max(plain.items(), key=lambda kv: kv[1])[0]
    h, s, l = _rgb_to_hls(r * 17, g * 17, b * 17)
    return [_hsl_to_hex(h, _clamp(s, 0.35, 0.8), _clamp(l, 0.58, 0.72))]
