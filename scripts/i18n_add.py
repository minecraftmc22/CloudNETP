"""把一批译文追加进 static/js/i18n/en-US.js。

手写对象字面量最容易犯的两个错：重复键、少逗号。这个脚本把两件事都挡住：
- 已经存在的 key 跳过（打印出来，方便确认不是「同词不同义」被合并）
- 写完立刻按 JSON 解析校验一遍，语法错会直接报出来

输入格式：一行一条「中文<TAB>English」，以 # 开头的行是注释。

用法：
    python scripts/i18n_add.py scripts/i18n_new.tsv
    python scripts/i18n_add.py --dry-run scripts/i18n_new.tsv
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DICT = ROOT / "static" / "js" / "i18n" / "en-US.js"


def load_pairs(path: Path) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" not in line:
            raise SystemExit(f"{path.name}:{no} 缺少 TAB 分隔：{line!r}")
        src, dst = line.split("\t", 1)
        src, dst = src.strip(), dst.strip()
        if not src or not dst:
            raise SystemExit(f"{path.name}:{no} 有一侧是空的：{line!r}")
        if src in seen:
            raise SystemExit(f"{path.name}:{no} 同一个 key 出现两次：{src!r}")
        seen.add(src)
        pairs.append((src, dst))
    return pairs


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    dry = "--dry-run" in sys.argv
    if not args:
        raise SystemExit(__doc__)

    src_text = DICT.read_text(encoding="utf-8")
    existing = set(
        json.loads('"' + m.group(1) + '"')
        for m in re.finditer(r'^\s*"((?:[^"\\]|\\.)*)"\s*:', src_text, re.M)
    )

    added, skipped = [], []
    for src, dst in load_pairs(Path(args[0])):
        (skipped if src in existing else added).append((src, dst))

    for src, _ in skipped:
        print(f"跳过（已有）：{src}")
    for src, dst in added:
        print(f"  新增 {src}  →  {dst}")

    if dry or not added:
        note = "（dry-run 未写入）" if dry else ""
        print(f"\n待新增 {len(added)} 条，跳过 {len(skipped)} 条{note}")
        return

    body = "".join(f"  {json.dumps(s, ensure_ascii=False)}: {json.dumps(d, ensure_ascii=False)},\n" for s, d in added)
    head, _, tail = src_text.rpartition("};")
    if not head:
        raise SystemExit("字典文件结构不认识：找不到结尾的 };")
    # 原来最后一条后面没有逗号，补一个，否则拼接后会成为语法错
    head = head.rstrip()
    if not head.endswith(","):
        head += ","
    new_text = f"{head}\n{body.rstrip()}\n}};\n{tail.lstrip()}"
    DICT.write_text(new_text, encoding="utf-8")

    inner = DICT.read_text(encoding="utf-8").split("{", 1)[1].rsplit("}", 1)[0]
    # 字典里按分组写了 /* ... */ 注释、末尾也留着尾逗号（JS 允许、JSON 不允许），校验前先摘掉
    inner = re.sub(r"/\*.*?\*/", "", inner, flags=re.S)
    inner = re.sub(r",\s*$", "", inner.rstrip())
    data = json.loads("{" + inner + "}")
    print(f"\n新增 {len(added)} 条，字典共 {len(data)} 条；JSON 校验通过")


if __name__ == "__main__":
    main()
