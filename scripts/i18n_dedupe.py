"""去掉字典里重复的 key，保留**最后一条**（JS 对象字面量的实际生效值）。

补字典时手写追加很容易写出重复键：后一条会静默盖掉前一条，看着像「翻译没生效」。
保留最后一条 = 不改任何现有行为，只是把死掉的那几行删掉。

用法：
    python scripts/i18n_dedupe.py            # 只报告
    python scripts/i18n_dedupe.py --write    # 真删
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DICT = ROOT / "static" / "js" / "i18n" / "en-US.js"

ENTRY = re.compile(r'^\s*"((?:[^"\\]|\\.)*)"\s*:')


def main() -> None:
    write = "--write" in sys.argv
    lines = DICT.read_text(encoding="utf-8").splitlines(keepends=True)

    # 每个 key 最后出现的那一行要保留
    last: dict[str, int] = {}
    for idx, line in enumerate(lines):
        m = ENTRY.match(line)
        if m:
            last[json.loads('"' + m.group(1) + '"')] = idx

    drop: list[int] = []
    for idx, line in enumerate(lines):
        m = ENTRY.match(line)
        if not m:
            continue
        key = json.loads('"' + m.group(1) + '"')
        if last[key] != idx:
            drop.append(idx)

    if not drop:
        print("没有重复 key")
        return
    print(f"重复 key {len(drop)} 行（保留每组最后一条）：")
    for idx in drop:
        print(f"  {idx + 1:5d}  {lines[idx].strip()[:90]}")
    if not write:
        print("\n加 --write 才会真的删除")
        return

    kept = [line for idx, line in enumerate(lines) if idx not in set(drop)]
    DICT.write_text("".join(kept), encoding="utf-8")

    # 校验：还能解析、key 数量没变
    inner = DICT.read_text(encoding="utf-8").split("{", 1)[1].rsplit("}", 1)[0]
    inner = re.sub(r"/\*.*?\*/", "", inner, flags=re.S)
    inner = re.sub(r",\s*$", "", inner.rstrip())
    data = json.loads("{" + inner + "}")
    print(f"\n已删除 {len(drop)} 行，剩 {len(data)} 条唯一 key；JSON 校验通过")


if __name__ == "__main__":
    main()
