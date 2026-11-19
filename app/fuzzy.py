"""模糊匹配：把「输入几个字就找到」这件事统一到一处。

三处搜索（我的文件 / 管理后台用户 / 审计日志）都用这套规则：

- **归一化**：小写、去掉空格和 - _ . · 这些分隔符，
  所以「年度报告」能命中「2024-年度_报告.pdf」
- **分词**：按空白切开，每个词都必须命中（AND），
  所以「2024 报告」比单搜「报告」更窄
- **子序列**：词里的字符按顺序出现就算命中（"rpt" → "report"），
  中文用不上，但英文文件名和中转英式缩写有用
- **打分**：完全相等 > 前缀 > 包含 > 子序列，越短的名字越靠前

SQL 只是粗筛（用 replace 去掉分隔符后 LIKE），最终排序在这里做。
"""

from __future__ import annotations

import re

# 归一化时丢掉的字符：空白与常见分隔符（含中文间隔号）
_SEPARATORS = re.compile(r"[\s\-_.·・]+")
_TOKENS = re.compile(r"\s+")

FULL = 1000  # 整串完全相等
PREFIX = 600  # 以关键词开头
CONTAIN = 400  # 中间包含
SUBSET = 120  # 字符按顺序散落出现
PER_TOKEN = 40  # 每个额外词的加成：词越多匹配越「精确」


def normalize(text: str) -> str:
    return _SEPARATORS.sub("", (text or "").lower())


def split_tokens(keyword: str) -> list[str]:
    """把输入切成归一化后的词，丢掉空串。"""
    return [norm for norm in (normalize(part) for part in _TOKENS.split((keyword or "").strip())) if norm]


def is_subsequence(needle: str, haystack: str) -> bool:
    """needle 的字符是否按顺序出现在 haystack 里（不要求连续）。"""
    if not needle:
        return True
    index = 0
    for ch in haystack:
        if ch == needle[index]:
            index += 1
            if index == len(needle):
                return True
    return False


def token_score(name_norm: str, token: str) -> int:
    """单个词对名字的得分，0 表示不匹配。"""
    if not token:
        return 0
    if name_norm == token:
        return FULL
    if name_norm.startswith(token):
        # 前缀越长、名字越短，分越高
        return PREFIX + int(200 * len(token) / max(len(name_norm), 1))
    pos = name_norm.find(token)
    if pos >= 0:
        return CONTAIN + int(150 * len(token) / max(len(name_norm), 1)) - min(pos, 60)
    if len(token) >= 2 and is_subsequence(token, name_norm):
        return SUBSET
    return 0


def score(name: str, keyword: str, tokens: list[str] | None = None) -> int:
    """名字对整串关键词的得分；0 = 不匹配。"""
    name_norm = normalize(name)
    if not name_norm:
        return 0
    words = tokens if tokens is not None else split_tokens(keyword)
    if not words:
        return 1  # 空关键词不筛人
    total = 0
    for word in words:
        got = token_score(name_norm, word)
        if not got:
            return 0
        total += got
    # 整串（连起来）也命中的，再给一点加成，让「年度报告」优先于「报告…年度…」
    whole = normalize(keyword)
    if len(words) > 1 and whole and whole in name_norm:
        total += 80
    return total + PER_TOKEN * (len(words) - 1)


def matches(name: str, keyword: str) -> bool:
    return score(name, keyword) > 0


def rank(items, keyword: str, key=lambda item: item):
    """按得分从高到低排，同分保持原顺序（调用方自己先按时间排好）。"""
    tokens = split_tokens(keyword)
    if not tokens:
        return list(items)
    scored = []
    for index, item in enumerate(items):
        got = score(key(item), keyword, tokens)
        if got:
            scored.append((got, index, item))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in scored]
