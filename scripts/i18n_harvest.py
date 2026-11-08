"""扫描前端源码里的可翻译文案（与运行时 translateMarkup 的口径一致）。

规则：
- 只看模板字符串（反引号）里的**静态文本**：标签之间的文字、title/aria-label/placeholder/alt 属性
- 插值 ${...} 里的内容一律跳过（那是用户数据或已翻译好的片段）
- JS 里传给 Toast/Dialog 的普通字符串单独收集
- 输出「规范化后的中文原文 → 出处」，供 en-US.js 对照翻译

用法：
    ./.venv/Scripts/python.exe scripts/i18n_harvest.py            # 汇总所有视图
    ./.venv/Scripts/python.exe scripts/i18n_harvest.py --missing  # 只列还没翻译的
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
# 标签之间的文字：>文字<
TEXT_RE = re.compile(r">([^<>]*[\u4e00-\u9fa5][^<>]*)<")
# 带中文的属性值
ATTR_RE = re.compile(r'\b(title|aria-label|placeholder|alt)="([^"]*[\u4e00-\u9fa5][^"]*)"')
# JS 里的整句字符串（toast / dialog / label）
JS_STR = re.compile(r'"((?:[^"\\]|\\.)*[\u4e00-\u9fa5](?:[^"\\]|\\.)*)"')

TARGETS = [
    "app.js",
    "api.js",
    "views/auth.js",
    "views/files.js",
    "views/settings.js",
    "views/trash.js",
    "views/shares.js",
    "views/feedback.js",
    "views/about.js",
    "views/admin.js",
    "ui/dialog.js",
    "ui/menu.js",
    "ui/toast.js",
    "ui/viewer.js",
    "ui/mascot-chat.js",
    "ui/mascot-actions.js",
    "ui/picker.js",
    "ui/tasks.js",
    "ui/share.js",
    "ui/update.js",
]


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def template_chunks(src: str):
    """粗粒度提取模板字符串里的静态文本段（${...} 整段跳过，含嵌套）。"""
    out = []
    i, n = 0, len(src)
    in_template = False
    while i < n:
        c = src[i]
        if not in_template:
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
            if c == "`":
                in_template = True
                i += 1
                continue
            i += 1
            continue

        # 模板里：收集到 ${ 或 `
        if c == "\\":
            i += 2
            continue
        if c == "`":
            in_template = False
            i += 1
            continue
        if c == "$" and i + 1 < n and src[i + 1] == "{":
            out.append(("skip", ""))
            i += 2
            depth = 1
            while i < n and depth:
                ch = src[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                elif ch in "'\"":
                    q = ch
                    i += 1
                    while i < n:
                        if src[i] == "\\":
                            i += 2
                            continue
                        if src[i] == q:
                            break
                        i += 1
                elif ch == "`":
                    # 嵌套模板：整段当插值跳过（内部由它自己的 H 处理）
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

        start = i
        while i < n and src[i] not in ("`", "$"):
            i += 1
        out.append(("text", src[start:i]))
    return out


def harvest_file(path: Path) -> tuple[dict[str, int], dict[str, int]]:
    src = path.read_text(encoding="utf-8")
    texts: dict[str, int] = {}
    attrs: dict[str, int] = {}

    for kind, chunk in template_chunks(src):
        if kind != "text":
            continue
        for m in TEXT_RE.finditer(chunk):
            key = norm(m.group(1))
            if key:
                texts[key] = texts.get(key, 0) + 1
        for m in ATTR_RE.finditer(chunk):
            key = norm(m.group(2))
            if key:
                attrs[key] = attrs.get(key, 0) + 1

    # 模板之外的单句（toast / dialog 参数）
    stripped = "".join(chunk if kind == "text" else " " for kind, chunk in template_chunks(src))
    for m in JS_STR.finditer(stripped):
        key = norm(m.group(1))
        # 只要「纯文案」：带标签/属性的是 HTML 片段，交给模板那一路处理
        if not key or any(ch in key for ch in "<>=`"):
            continue
        texts[key] = texts.get(key, 0) + 1
    return texts, attrs


def dict_keys() -> set[str]:
    if not DICT.is_file():
        return set()
    src = DICT.read_text(encoding="utf-8")
    keys = set()
    for line in src.splitlines():
        m = re.match(r'\s*"((?:[^"\\]|\\.)*)"\s*:', line)
        if m:
            raw = m.group(1)
            try:
                keys.add(json.loads(f'"{raw}"'))
            except json.JSONDecodeError:
                keys.add(raw)
        m2 = re.match(r"\s*'((?:[^'\\]|\\.)*)'\s*:", line)
        if m2:
            keys.add(m2.group(1))
    return keys


def main() -> None:
    only_missing = "--missing" in sys.argv
    known = dict_keys()
    total = 0
    missing_total = 0
    for rel in TARGETS:
        path = JS_DIR / rel
        if not path.is_file():
            continue
        texts, attrs = harvest_file(path)
        keys = sorted(set(texts) | set(attrs))
        if not keys:
            continue
        missing = [k for k in keys if k not in known]
        total += len(keys)
        missing_total += len(missing)
        shown = missing if only_missing else keys
        print(f"\n== {rel}  共 {len(keys)} 条，缺译 {len(missing)} 条 ==")
        for key in shown[: (400 if only_missing else 60)]:
            print(f"  {key}")
        if len(shown) > (400 if only_missing else 60):
            print(f"  … 还有 {len(shown) - (400 if only_missing else 60)} 条")
    print(f"\n合计 {total} 条，缺译 {missing_total} 条，字典已有 {len(known)} 条")


if __name__ == "__main__":
    main()
