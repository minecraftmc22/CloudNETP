"""检查源码里 t("...") / t(`...`) 的 key 是否都在字典里。

用法：
    python scripts/i18n_check.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "static" / "js"
DICT = JS_DIR / "i18n" / "en-US.js"

CJK = re.compile(r"[\u4e00-\u9fa5]")
# t("...") / t('...') / t(`...`)，允许中间有换行和空格
CALL = re.compile(r"\bt\(\s*([\"'`])((?:\\.|(?!\1)[^\\])*?)\1", re.S)


def dict_keys() -> set[str]:
    src = DICT.read_text(encoding="utf-8")
    return {
        json.loads('"' + m.group(1) + '"')
        for m in re.finditer(r'^\s*"((?:[^"\\]|\\.)*)"\s*:', src, re.M)
    }


def dict_duplicates() -> list[str]:
    """同一个 key 写了两次：后一条会静默盖掉前一条，是补字典时最常见的坑。"""
    src = DICT.read_text(encoding="utf-8")
    seen: dict[str, int] = {}
    for m in re.finditer(r'^\s*"((?:[^"\\]|\\.)*)"\s*:', src, re.M):
        key = json.loads('"' + m.group(1) + '"')
        seen[key] = seen.get(key, 0) + 1
    return sorted(k for k, n in seen.items() if n > 1)


def t_keys(path: Path):
    src = path.read_text(encoding="utf-8")
    for m in CALL.finditer(src):
        raw = m.group(2)
        if "${" in raw:
            continue
        if not CJK.search(raw):
            continue
        line = src.count("\n", 0, m.start()) + 1
        yield line, raw.replace("\\n", "\n")


def main() -> None:
    keys = dict_keys()
    missing_total = 0
    for path in sorted(JS_DIR.rglob("*.js")):
        if path == DICT:
            continue
        miss = [(line, key) for line, key in t_keys(path) if key not in keys]
        if miss:
            missing_total += len(miss)
            print(f"\n== {path.relative_to(ROOT)} 缺 {len(miss)} 条 ==")
            for line, key in miss:
                print(f"  {line:5d}  {key}")
    dups = dict_duplicates()
    if dups:
        print(f"\n== 字典里重复的 key {len(dups)} 条 ==")
        for key in dups:
            print("  " + key)
    print(f"\n合计缺译 {missing_total} 条，字典 {len(keys)} 条")


if __name__ == "__main__":
    main()
