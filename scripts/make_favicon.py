"""把 chibi 插画做成整套站点图标（favicon / PWA / apple-touch）。

做法：
 1. 原图是白底插画，从四角 flood fill 把**外部白底**挖成透明（角色内部的白色水手服、
    脸部高光被黑色描边封住，不会被误挖）。
 2. 给角色加一圈白色描边（sticker 感），贴在深紫渐变 + 粉色光晕的底上，
    这样在浅色/深色浏览器标签栏里都能认出来。
 3. 输出 ico + 各尺寸 png。

用法（在项目根目录）：
    ./.venv/Scripts/python.exe scripts/make_favicon.py [源图路径]
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "static" / "assets"
DEFAULT_SRC = r"C:\Users\Lenovo\.workbuddy\clipboard-images\clipboard-2026-11-02T04-01-15-900Z-f9d3ed26.jpg"

CANVAS = 1024  # 合成画布（超采样），最终缩到 512 等尺寸

# 底色：与站点主题 #150f21 + 樱粉呼应
BG_TOP = (21, 15, 33)
BG_BOTTOM = (46, 27, 66)
GLOW = (249, 168, 212)
GLOW_ALPHA = 92


def cutout(src: Path) -> Image.Image:
    """挖掉外部白底，返回裁到角色 bbox 的 RGBA。"""
    im = Image.open(src).convert("RGB")
    w, h = im.size
    key = (255, 0, 255)

    # 外部白底是连通的，四个角各来一次更保险
    for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        if im.getpixel(xy) != key:
            ImageDraw.floodfill(im, xy, key, thresh=88)

    out = Image.new("RGBA", (w, h))
    src_px = im.load()
    out_px = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b = src_px[x, y]
            if r > 235 and b > 235 and g < 60:
                continue  # key 色 → 透明
            out_px[x, y] = (r, g, b, 255)

    bbox = out.getbbox()
    if not bbox:
        raise SystemExit("抠图失败：整张都被当成背景了")
    return out.crop(bbox)


def backdrop() -> Image.Image:
    """深紫竖向渐变 + 顶部粉色光晕。"""
    img = Image.new("RGB", (CANVAS, CANVAS))
    d = ImageDraw.Draw(img)
    for y in range(CANVAS):
        t = y / (CANVAS - 1)
        d.line(
            [(0, y), (CANVAS, y)],
            fill=(
                round(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t),
                round(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t),
                round(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t),
            ),
        )

    # 径向粉色光晕（中心亮、边缘衰减到 0），整体偏上，四角保持深紫
    rad = Image.radial_gradient("L").resize((int(CANVAS * 1.3),) * 2, Image.BICUBIC)
    ox = (CANVAS - rad.width) // 2
    oy = int(-CANVAS * 0.16)
    canvas = Image.new("L", (CANVAS, CANVAS), 255)
    canvas.paste(rad, (ox, oy))
    glow = Image.eval(canvas, lambda v: int(GLOW_ALPHA * (1 - v / 255) ** 2.2))
    img.paste(Image.new("RGB", (CANVAS, CANVAS), GLOW), (0, 0), glow)
    return img.convert("RGBA")


def sticker(role: Image.Image, height_ratio: float) -> Image.Image:
    """把角色缩放、居中，加白色描边，合成到底图上。"""
    base = backdrop()
    target_h = int(CANVAS * height_ratio)
    scale = target_h / role.height
    target_w = max(1, round(role.width * scale))
    # 缩小用 LANCZOS（大图变小），放大才用 BICUBIC
    resample = Image.LANCZOS if scale < 1 else Image.BICUBIC
    role = role.resize((target_w, target_h), resample)

    # 白色贴纸描边：alpha 膨胀 减 原 alpha
    pad = max(6, int(CANVAS * 0.018))
    alpha = role.getchannel("A")
    grown = alpha.filter(ImageFilter.MaxFilter(pad * 2 + 1))
    solid = Image.new("RGBA", role.size, (255, 255, 255, 255))
    solid.putalpha(grown)

    x = (CANVAS - target_w) // 2
    y = (CANVAS - target_h) // 2
    base.alpha_composite(solid, (x, y))
    base.alpha_composite(role, (x, y))
    return base


def rounded(img: Image.Image, ratio: float = 0.225) -> Image.Image:
    size = img.size[0]
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size * 4 - 1, size * 4 - 1], radius=int(size * 4 * ratio), fill=255
    )
    mask = mask.resize((size, size), Image.LANCZOS)
    out = img.copy()
    out.putalpha(mask)
    return out


def save(img: Image.Image, name: str, size: int) -> None:
    path = ASSETS / name
    img.resize((size, size), Image.LANCZOS).save(path, optimize=True)
    print(f"  {name:<26} {size}x{size}  {path.stat().st_size / 1024:.1f} KB")


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(DEFAULT_SRC)
    if not src.exists():
        raise SystemExit(f"源图不存在：{src}")
    ASSETS.mkdir(parents=True, exist_ok=True)

    print(f"源图 {src.name} {Image.open(src).size}")
    role = cutout(src)
    print(f"抠图完成 bbox={role.size}")

    # 圆角版：PWA any + favicon
    round_src = rounded(sticker(role, 0.79))
    save(round_src, "icon-512.png", 512)
    save(round_src, "icon-192.png", 192)
    save(round_src, "favicon-32.png", 32)
    save(round_src, "favicon-16.png", 16)

    # maskable：安全区更小，四角出血（系统自己裁）
    save(sticker(role, 0.64), "icon-maskable-512.png", 512)

    # iOS 主屏图标：不透明、直角（系统自己加圆角）
    save(sticker(role, 0.76), "apple-touch-icon.png", 180)

    # 多尺寸 ico（浏览器标签 + Windows 快捷方式）
    ico = round_src.resize((256, 256), Image.LANCZOS)
    ico_path = ASSETS / "favicon.ico"
    ico.save(ico_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"  {'favicon.ico':<26} 16~256 多尺寸  {ico_path.stat().st_size / 1024:.1f} KB")

    # 预览图（拼成一张方便肉眼检查）
    tiles = [
        ("512", round_src.resize((256, 256), Image.LANCZOS)),
        ("64", round_src.resize((64, 64), Image.LANCZOS)),
        ("32", round_src.resize((32, 32), Image.LANCZOS)),
        ("16", round_src.resize((16, 16), Image.LANCZOS)),
    ]
    preview = Image.new("RGBA", (700, 300), (21, 15, 33, 255))
    x = 20
    for label, t in tiles:
        preview.alpha_composite(t, (x, 22))
        x += t.size[0] + 24
    preview.convert("RGB").save(ROOT / "scripts" / "favicon_preview.png")
    print("预览 scripts/favicon_preview.png")


if __name__ == "__main__":
    main()
