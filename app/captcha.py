"""图形验证码：服务端生成、服务端核验，前端只负责显示。

设计要点：
- 码只在服务端内存里，前端拿到的 SVG 是渲染结果，不参与判定；
- 一次性：无论是核验成功还是输错，这张码都立即销毁（前端会自动换一张）；
- 有效期 5 分钟，过期自动清；
- 签发限流：同一 IP 每分钟最多 20 张，防止被当成图片工厂刷内存；
- 总量上限 5000 条，超了直接清最旧的一批，避免被刷爆内存。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

# 去掉 0/O/1/I/L 这类一眼看不出区别的字符
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 4
CODE_TTL_SECONDS = 300

ISSUE_WINDOW_SECONDS = 60
ISSUE_LIMIT_PER_IP = 20
MAX_ENTRIES = 5000

_lock = threading.Lock()
_codes: dict[str, "Challenge"] = {}
_issue_log: dict[str, list[float]] = {}


@dataclass
class Challenge:
    code: str
    expires_at: float


class TooManyRequests(Exception):
    """签发过于频繁。"""


def _purge(now: float) -> None:
    for key, item in list(_codes.items()):
        if item.expires_at <= now:
            _codes.pop(key, None)
    if len(_codes) > MAX_ENTRIES:
        # 按过期时间排序，砍掉最早的一批
        ordered = sorted(_codes.items(), key=lambda kv: kv[1].expires_at)
        for key, _ in ordered[: len(_codes) - MAX_ENTRIES]:
            _codes.pop(key, None)


# 5×7 点阵字形。用点阵而不是 <text> 是刻意的：
# SVG 里的 <text> 节点能被脚本直接读出来，验证码就等于没有；
# 点阵画成几何图形后，机器只能靠 OCR 去猜。
_GLYPHS: dict[str, tuple[str, ...]] = {
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("#####", "...#.", "..#..", "...#.", "....#", "#...#", ".###."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
}

# 尺寸按「登录框里 132×40 的展示区」反推：
# 4 个字符 × 5 列 × 4px + 3 段间距 + 留白 ≈ 121×42，
# 这个比例和展示区几乎一致，浏览器只需微缩 5%，点阵不会糊成一团。
_DOT = 4.0          # 每个点阵格的边长
_GAP = 7.0          # 字符间距
_MARGIN_X = 8.0
_MARGIN_Y = 7.0


def _svg(code: str) -> str:
    """把验证码画成点阵 SVG —— 没有任何可被读出的文字节点。"""
    cols, rows = 5, 7
    width = int(_MARGIN_X * 2 + len(code) * cols * _DOT + (len(code) - 1) * _GAP)
    height = int(_MARGIN_Y * 2 + rows * _DOT)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="验证码">',
        f'<rect width="{width}" height="{height}" fill="#1b1326" rx="8"/>',
    ]

    # 背景噪点（数量与不透明度都压低：噪点是为了干扰机器切格，不是为了让用户看不清）
    for _ in range(24):
        cx = secrets.randbelow(width)
        cy = secrets.randbelow(height)
        r = 0.8 + secrets.randbelow(10) / 10
        opacity = 0.05 + secrets.randbelow(12) / 100
        parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r:.1f}" fill="#c084fc" opacity="{opacity:.2f}"/>'
        )

    # 干扰线
    for _ in range(1):
        x1, y1 = secrets.randbelow(width), secrets.randbelow(height)
        x2, y2 = secrets.randbelow(width), secrets.randbelow(height)
        parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#f9a8d4" '
            f'stroke-opacity="0.16" stroke-width="1"/>'
        )

    palette = ("#f5e9ff", "#e9d5ff", "#fbcfe8", "#ddd6fe", "#ffffff")
    for index, char in enumerate(code):
        glyph = _GLYPHS.get(char)
        if not glyph:
            continue
        base_x = _MARGIN_X + index * (cols * _DOT + _GAP)
        base_y = _MARGIN_Y + secrets.randbelow(5) - 2
        color = palette[secrets.randbelow(len(palette))]
        # 整体倾斜控制在 ±6°：再大就真的读不出来了
        angle = secrets.randbelow(13) - 6
        dots: list[str] = []
        for row, line in enumerate(glyph):
            for col, cell in enumerate(line):
                if cell != "#":
                    continue
                # 每格抖一点点，既保持可读性，又破坏规整网格
                jx = (secrets.randbelow(9) - 4) / 18
                jy = (secrets.randbelow(9) - 4) / 18
                cx = base_x + col * _DOT + _DOT / 2 + jx
                cy = base_y + row * _DOT + _DOT / 2 + jy
                if secrets.randbelow(100) < 18:
                    dots.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{_DOT / 2:.1f}"/>')
                else:
                    dots.append(
                        f'<rect x="{cx - _DOT / 2:.1f}" y="{cy - _DOT / 2:.1f}" '
                        f'width="{_DOT:.1f}" height="{_DOT:.1f}" rx="1"/>'
                    )
        center_x = base_x + cols * _DOT / 2
        center_y = base_y + rows * _DOT / 2
        parts.append(
            f'<g fill="{color}" opacity="0.95" '
            f'transform="rotate({angle} {center_x:.1f} {center_y:.1f})">{"".join(dots)}</g>'
        )

    parts.append("</svg>")
    return "".join(parts)


def issue(ip: str) -> tuple[str, str]:
    """签发一张验证码，返回 (captcha_id, svg)。"""
    now = time.time()
    with _lock:
        _purge(now)

        recent = [t for t in _issue_log.get(ip, []) if now - t < ISSUE_WINDOW_SECONDS]
        if len(recent) >= ISSUE_LIMIT_PER_IP:
            raise TooManyRequests
        recent.append(now)
        _issue_log[ip] = recent

        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        cid = secrets.token_urlsafe(16)
        _codes[cid] = Challenge(code=code, expires_at=now + CODE_TTL_SECONDS)
        return cid, _svg(code)


def verify(captcha_id: str, code: str) -> tuple[bool, str]:
    """核验验证码。返回 (是否通过, 失败原因)。

    无论成功还是输错，这张码都立刻作废（前端在报错后会自动换一张）。
    这样既不给撞码留重试空间，也避免「图还是旧的、码已经死了」的迷惑状态。
    """
    if not captcha_id or not code:
        return False, "请输入验证码"

    now = time.time()
    with _lock:
        item = _codes.get(captcha_id)
        if item is None:
            return False, "验证码已失效，请点击刷新重试"
        if item.expires_at <= now:
            _codes.pop(captcha_id, None)
            return False, "验证码已过期，请点击刷新重试"

        _codes.pop(captcha_id, None)
        if not secrets.compare_digest(item.code, (code or "").strip().upper()):
            return False, "验证码不正确"
        return True, ""


def consume(captcha_id: str) -> None:
    """作废一张验证码（核验失败后前端会换新的，这里用于主动清理）。"""
    with _lock:
        _codes.pop(captcha_id, None)
