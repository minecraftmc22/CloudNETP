"""找出「被 ${...} 插值劈开的中文句子」——H 只翻标签之间的整段文字，这类句子会漏掉。

报告形如：文件:行号 → 片段文本（已把插值替换成 {}）。

用法：
    python scripts/i18n_inline.py            # 全部
    python scripts/i18n_inline.py --below 3  # 只报中文片段少于 3 个字符的（多半是单位/标点，可忽略）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "static" / "js"
CJK = re.compile(r"[\u4e00-\u9fa5]")
DICT = JS_DIR / "i18n" / "en-US.js"


def dict_keys() -> set[str]:
    src = DICT.read_text(encoding="utf-8")
    return {m.group(1) for m in re.finditer(r'^\s*"((?:[^"\\]|\\.)*)"\s*:', src, re.M)}


def scan(path: Path):
    """返回 [(行号, 近似原文)]：模板里「与插值相邻」的中文段。"""
    src = path.read_text(encoding="utf-8")
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "'\"":
            q = c
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == q:
                    i += 1
                    break
                i += 1
            continue
        if c != "`":
            i += 1
            continue
        # 进入模板字符串
        start = i
        i += 1
        buf = ""
        touched = False  # 这一段是否与插值相邻
        while i < n:
            ch = src[i]
            if ch == "\\":
                buf += src[i : i + 2]
                i += 2
                continue
            if ch == "`":
                if touched and CJK.search(buf):
                    line = src.count("\n", 0, start) + 1
                    out.append((line, re.sub(r"\s+", " ", buf).strip()))
                break
            if ch == "$" and i + 1 < n and src[i + 1] == "{":
                if CJK.search(buf):
                    line = src.count("\n", 0, start) + 1
                    out.append((line, re.sub(r"\s+", " ", buf).strip()))
                buf = ""
                touched = True
                i += 2
                depth = 1
                while i < n and depth:
                    if src[i] == "{":
                        depth += 1
                    elif src[i] == "}":
                        depth -= 1
                    elif src[i] in "'\"":
                        q = src[i]
                        i += 1
                        while i < n:
                            if src[i] == "\\":
                                i += 2
                                continue
                            if src[i] == q:
                                break
                            i += 1
                    elif src[i] == "`":
                        i += 1
                        while i < n:
                            if src[i] == "\\":
                                i += 2
                                continue
                            if src[i] == "`":
                                break
                            i += 1
                    i += 1
                continue
            buf += ch
            i += 1
        i += 1
    return out


def main() -> None:
    known = dict_keys()
    limit = 0
    if "--below" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--below") + 1])
    total = 0
    for path in sorted(JS_DIR.rglob("*.js")):
        if path == DICT:
            continue
        hits = [
            (line, text)
            for line, text in scan(path)
            if len(CJK.findall(text)) > limit and text not in known
        ]
        if not hits:
            continue
        total += len(hits)
        print(f"\n== {path.relative_to(ROOT)}  {len(hits)} 处 ==")
        for line, text in hits:
            print(f"  {line:5d}  {text}")
    print(f"\n合计 {total} 处")


if __name__ == "__main__":
    main()
