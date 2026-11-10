# -*- coding: utf-8 -*-
"""关于页的更新记录有没有跟上版本号。

CHANGELOG 写死在 static/js/views/about.js 里，改完版本号经常忘了补一条。这里检查：

  1. 最新一条的版本号 == config.VERSION（发版必须写日志）
  2. 当前版本确实在列表里，版本号不重复、严格从新到旧
  3. 每条都有标题，且至少写了一条说明（避免占位式空条目）

    ./.venv/Scripts/python.exe scripts/changelog_check.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ABOUT = ROOT / "static" / "js" / "views" / "about.js"

# 一条记录：version / title / items（items 取到第一个 ] 为止，条目标题里不会再出现 ]）
ENTRY = re.compile(
    r'version:\s*"(?P<version>[^"]+)"\s*,\s*'
    r'title:\s*"(?P<title>[^"]*)"\s*,\s*'
    r'items:\s*\[(?P<items>.*?)\]',
    re.S,
)
QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')


def key_of(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def main() -> int:
    from app.config import settings

    src = ABOUT.read_text(encoding="utf-8")
    body = src[src.index("const CHANGELOG = [") :]
    entries = list(ENTRY.finditer(body))

    problems: list[str] = []
    if not entries:
        problems.append("about.js 里没读到任何 CHANGELOG 条目")
    else:
        versions = [m.group("version") for m in entries]
        current = settings.VERSION

        if versions[0] != current:
            problems.append(
                f"最新一条是 v{versions[0]}，但当前版本是 v{current} —— 发版要补一条更新记录"
            )
        if current not in versions:
            problems.append(f"CHANGELOG 里没有当前版本 v{current}")
        if len(versions) != len(set(versions)):
            dup = sorted({v for v in versions if versions.count(v) > 1})
            problems.append(f"版本号重复：{', '.join(dup)}")
        for older, newer in zip(versions[1:], versions):
            if key_of(older) >= key_of(newer):
                problems.append(f"顺序不对：v{older} 排在 v{newer} 后面")

        for item in entries:
            version = item.group("version")
            if not item.group("title").strip():
                problems.append(f"v{version} 没有标题")
            if len(QUOTED.findall(item.group("items"))) < 1:
                problems.append(f"v{version} 一条说明都没写")

    if problems:
        print("更新记录检查未通过：")
        for line in problems:
            print("  -", line)
        return 1

    print(f"更新记录 OK：共 {len(entries)} 个版本，最新 v{entries[0].group('version')} = 当前版本")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
