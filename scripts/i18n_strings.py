"""列出源码里「模板字符串之外」的中文双引号字符串（这些必须手动包 t()，H 管不到）。

用法：
    python scripts/i18n_strings.py [文件...]      # 默认扫 static/js 下所有 js
    python scripts/i18n_strings.py --wrapped static/js/views/admin.js
        # 只看还没被 t(...) 包住的，直接给行号，方便逐个补
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "static" / "js"
CJK = re.compile(r"[\u4e00-\u9fa5]")
STR = re.compile(r'"((?:[^"\\]|\\.)*)"')


def mask(src: str) -> str:
    """把注释、模板字符串、单引号字符串挖空成空格（保持长度与行号）。"""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            out.append("  ")
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        if c in "'`":
            q = c
            out.append(" ")
            i += 1
            depth = 0
            while i < n:
                ch = src[i]
                if ch == "\\":
                    out.append("  ")
                    i += 2
                    continue
                if q == "`" and ch == "$" and i + 1 < n and src[i + 1] == "{":
                    depth += 1
                if q == "`" and ch == "}" and depth:
                    depth -= 1
                if ch == q and depth == 0:
                    out.append(" ")
                    i += 1
                    break
                out.append("\n" if ch == "\n" else " ")
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def report(paths, only_wrapped: bool) -> None:
    total = 0
    for path in paths:
        src = path.read_text(encoding="utf-8")
        masked = mask(src)
        hits = []
        for m in STR.finditer(masked):
            text = src[m.start() + 1 : m.end() - 1]
            if not CJK.search(text):
                continue
            before = masked[max(0, m.start() - 3) : m.start()]
            wrapped = before.endswith("t(")
            if only_wrapped and wrapped:
                continue
            line = masked.count("\n", 0, m.start()) + 1
            hits.append((line, wrapped, text))
            total += 1
        if hits:
            print(f"\n== {path.relative_to(ROOT)}  共 {len(hits)} 条 ==")
            for line, wrapped, text in hits:
                mark = "t()" if wrapped else "   "
                print(f"  {line:5d} [{mark}] {text}")
    print(f"\n合计 {total} 条")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    only_wrapped = "--wrapped" in sys.argv
    if args:
        files = [Path(a) if Path(a).is_absolute() else ROOT / a for a in args]
    else:
        files = sorted(JS_DIR.rglob("*.js"))
    report(files, only_wrapped)
