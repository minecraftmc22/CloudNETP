# -*- coding: utf-8 -*-
"""卡片透明度接线检查：所有「卡片」类底色都必须乘 --op-card 倍数。

0.3.75 加了「设置 → 外观 → 卡片」滑块后，陆续发现分享分页条、快传列表行
这类卡片没接上滑块（写死了 86% / 88%）。这个脚本零依赖、不用起服务，
逐个选择器确认 background 里用的是 calc(var(--op-card, ...) * NN%)。

弹层（.share-modal）、对话面板（.mchat）、法律页目录是有意不跟卡片滑块的，
不在清单里；要加新卡片时把选择器补进 TARGETS 即可。

    ./.venv/Scripts/python.exe scripts/card_opacity_check.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TARGETS: dict[str, list[str]] = {
    "static/css/views.css": [
        ".settings-card",
        ".settings-card__body",
        ".about-card",
        ".share-pager",
    ],
    "static/css/quick.css": [".quick-card", ".quick-row", ".quick-gate"],
    "static/css/admin.css": [".stat-card"],
    "static/css/files.css": [".tile"],
}

# 规则块开头：允许有作用域前缀（如 ".settings .settings-card__body"）
def blocks(css: str, selector: str) -> list[str]:
    pattern = re.compile(
        r"(?m)^[^\n{}]*" + re.escape(selector) + r"[^{\n]*\{", re.M
    )
    out = []
    for match in pattern.finditer(css):
        end = css.find("}", match.end())
        if end > 0:
            out.append(css[match.end() : end])
    return out


def main() -> int:
    passed = failed = 0
    for rel, selectors in TARGETS.items():
        css = (ROOT / rel).read_text(encoding="utf-8")
        print(f"== {rel} ==")
        for selector in selectors:
            found = blocks(css, selector)
            # 一个选择器可以有多处规则（普通态 / hover / sticky 覆盖），
            # 至少要有声明背景那一块接上 --op-card
            wired = any("calc(var(--op-card" in block for block in found)
            if wired:
                passed += 1
                print(f"  OK   {selector}")
            else:
                failed += 1
                print(f"  FAIL {selector} —— 没找到 calc(var(--op-card …) 的背景（规则块 {len(found)} 个）")

    print()
    if failed:
        print(f"失败 {failed} 项，通过 {passed} 项")
        return 1
    print(f"卡片透明度接线 OK（{passed} 个选择器）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
