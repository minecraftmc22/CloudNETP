/**
 * 模糊匹配（前端版，规则与 app/fuzzy.py 保持一致）。
 *
 * 用途：设置页搜索设项。后端那套负责文件与后台数据，这里负责已经渲染好的 DOM 文本。
 *
 * - 归一化：小写、去掉空格和 - _ . · ・，所以「年度报告」能命中「2024-年度_报告.pdf」
 * - 分词：按空白切开，每个词都要命中（AND）
 * - 子序列：字符按顺序出现就算命中（"rpt" → "report"）
 * - 打分：完全相等 > 前缀 > 包含 > 子序列
 */

const SEPARATORS = /[\s\-_.·・]+/g;

export function normalizeFuzzy(text) {
  return String(text || "").toLowerCase().replace(SEPARATORS, "");
}

export function splitTokens(keyword) {
  return String(keyword || "")
    .trim()
    .split(/\s+/)
    .map(normalizeFuzzy)
    .filter(Boolean);
}

export function isSubsequence(needle, haystack) {
  if (!needle) return true;
  let index = 0;
  for (const ch of haystack) {
    if (ch === needle[index]) {
      index += 1;
      if (index === needle.length) return true;
    }
  }
  return false;
}

function tokenScore(name, token) {
  if (!token) return 0;
  if (name === token) return 1000;
  if (name.startsWith(token)) return 600 + Math.floor((200 * token.length) / Math.max(name.length, 1));
  const pos = name.indexOf(token);
  if (pos >= 0) {
    return 400 + Math.floor((150 * token.length) / Math.max(name.length, 1)) - Math.min(pos, 60);
  }
  if (token.length >= 2 && isSubsequence(token, name)) return 120;
  return 0;
}

/** 名字对关键词的得分，0 = 不匹配。tokens 可传已切好的词，省得反复切。 */
export function fuzzyScore(name, keyword, tokens) {
  const target = normalizeFuzzy(name);
  if (!target) return 0;
  const words = tokens || splitTokens(keyword);
  if (!words.length) return 1;
  let total = 0;
  for (const word of words) {
    const got = tokenScore(target, word);
    if (!got) return 0;
    total += got;
  }
  const whole = normalizeFuzzy(keyword);
  if (words.length > 1 && whole && target.includes(whole)) total += 80;
  return total + 40 * (words.length - 1);
}

/** 把命中的词在原文里标出来（只标「原文直接包含」的，归一化后才匹配的不标）。 */
export function highlight(text, keyword) {
  const raw = String(text || "");
  const words = String(keyword || "")
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .sort((a, b) => b.length - a.length);
  if (!words.length) return escapeHtml(raw);
  let out = escapeHtml(raw);
  for (const word of words) {
    const safe = escapeHtml(word).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    if (!safe) continue;
    out = out.replace(new RegExp(safe, "gi"), (hit) => `<mark>${hit}</mark>`);
  }
  return out;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[ch]);
}
