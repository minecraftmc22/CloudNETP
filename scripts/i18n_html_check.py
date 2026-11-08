"""检查 H`...` 模板与 innerHTML 字符串里漏译的静态文案。

`i18n_check.py` 只看 `t("...")`，但很多视图是拿 H`<p>中文</p>` 或
`el.innerHTML = "<span>中文</span>"` 直接拼 HTML 的 —— 这些文案由
translateMarkup 逐段查表，查不到就原样留在界面里。本脚本把同一套规则
（标签之间的文字 + title/aria-label/placeholder/alt 属性）在 Python 侧复现，
把「英文界面里仍会露出中文」的候选列出来。

用法：
    python scripts/i18n_html_check.py
    python scripts/i18n_html_check.py static/js/share.js
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "static" / "js"
DICT = JS_DIR / "i18n" / "en-US.js"

CJK = re.compile(r"[\u4e00-\u9fa5]")
# 反引号模板（含 H`...`），不跨越模板边界；`(?:\\.|[^`\\])*` 允许转义反引号
TEMPLATE = re.compile(r"`((?:\\.|[^`\\])*)`", re.S)
TEXT_RE = re.compile(r">([^<>]*[\u4e00-\u9fa5][^<>]*)<")
ATTR_RE = re.compile(r'\b(title|aria-label|placeholder|alt)="([^"]*[\u4e00-\u9fa5][^"]*)"')


def strip_interp(body: str) -> str:
    """把 ${...} 整段挖掉（按花括号配对，字符串里的括号不算）。

    插值内容是数据或者已经 t() 过的译文，不该再查表；挖掉之后剩下的
    标签间文字才是「translateMarkup 会去查表」的静态文案。
    """
    out = []
    i = 0
    while i < len(body):
        if body.startswith("${", i):
            depth = 0
            j = i + 1
            quote = ""
            while j < len(body):
                ch = body[j]
                if quote:
                    if ch == "\\":
                        j += 2
                        continue
                    if ch == quote:
                        quote = ""
                elif ch in "\"'`":
                    quote = ch
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append(" ")
            i = j + 1
            continue
        out.append(body[i])
        i += 1
    return "".join(out)


def dict_keys() -> set[str]:
    src = DICT.read_text(encoding="utf-8")
    return {
        json.loads('"' + m.group(1) + '"')
        for m in re.finditer(r'^\s*"((?:[^"\\]|\\.)*)"\s*:', src, re.M)
    }


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def scan(path: Path, keys: set[str]) -> int:
    src = path.read_text(encoding="utf-8")
    hits = 0
    for tpl in TEMPLATE.finditer(src):
        body = tpl.group(1)
        if not CJK.search(body):
            continue
        line0 = src.count("\n", 0, tpl.start()) + 1
        flat = strip_interp(body)
        for m in TEXT_RE.finditer(flat):
            key = norm(m.group(1))
            if CJK.search(key) and key not in keys:
                line = line0 + flat.count("\n", 0, m.start())
                print(f"  {line:5d}  {key}")
                hits += 1
        for m in ATTR_RE.finditer(flat):
            key = norm(m.group(2))
            if key not in keys:
                line = line0 + flat.count("\n", 0, m.start())
                print(f"  {line:5d}  {key}")
                hits += 1
    return hits


def main() -> None:
    keys = dict_keys()
    targets = [Path(a) for a in sys.argv[1:]] or [
        p for p in sorted(JS_DIR.rglob("*.js")) if p != DICT
    ]
    total = 0
    for path in targets:
        label = path.relative_to(ROOT) if path.is_absolute() else path
        print(f"\n== {label} ==")
        total += scan(path, keys)
    print(f"\n合计漏译 {total} 条（H 模板 / innerHTML 里的静态文案）")


if __name__ == "__main__":
    main()
