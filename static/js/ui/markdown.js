/**
 * 极简 Markdown 渲染器（零依赖）
 *
 * 为什么不用 marked / markdown-it：
 * 1. 它们默认允许内联 HTML，而分享链接里的 md 是「别人上传的文件」，
 *    同源执行脚本等于把整个网盘交出去；自己写就天然先转义再标记。
 * 2. 体积：整个文件不到 8KB，marked 压缩后 40KB 起，还要再配 DOMPurify。
 *
 * 支持：标题、段落、粗体/斜体/删除线、行内代码、围栏代码块、引用、
 *       有序/无序列表（含一层缩进）、表格、分隔线、链接、图片。
 * 不支持（按纯文本渲染）：内联 HTML、脚注、公式、任务列表的勾选框。
 */

const SAFE_URL = /^(https?:\/\/|mailto:|tel:|\/|#|\.\/|\.\.\/)/i;

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** 链接地址白名单：挡掉 javascript: / data: 这类能在同源执行脚本的协议 */
function safeUrl(raw) {
  const url = String(raw || "").trim();
  if (!url) return "";
  return SAFE_URL.test(url) ? url : "";
}

function inline(text) {
  let out = escapeHtml(text);

  // 行内代码先抽出来，避免里面的 * _ 被当成强调
  const codes = [];
  out = out.replace(/`([^`]+)`/g, (_, code) => {
    codes.push(code);
    return `\u0000${codes.length - 1}\u0000`;
  });

  // 图片 → 链接 → 强调（顺序不能反，链接文本里可能带星号）
  out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g, (_, alt, src) => {
    const url = safeUrl(unescapeEntities(src));
    if (!url) return alt;
    return `<img src="${escapeHtml(url)}" alt="${alt}" loading="lazy">`;
  });
  out = out.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g, (_, label, href) => {
    const url = safeUrl(unescapeEntities(href));
    if (!url) return label;
    const ext = /^https?:/i.test(url) ? ' target="_blank" rel="noopener noreferrer"' : "";
    return `<a href="${escapeHtml(url)}"${ext}>${label}</a>`;
  });

  out = out
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>")
    .replace(/~~([^~]+)~~/g, "<del>$1</del>");

  out = out.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
  return out;
}

function unescapeEntities(text) {
  return text.replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#39;/g, "'");
}

const RE_FENCE = /^\s*(```+|~~~+)\s*([\w+-]*)\s*$/;
const RE_HEADING = /^\s{0,3}(#{1,6})\s+(.*)$/;
const RE_HR = /^\s{0,3}([-*_])\s*(?:\1\s*){2,}$/;
const RE_QUOTE = /^\s{0,3}>\s?(.*)$/;
const RE_UL = /^(\s*)[-*+]\s+(.*)$/;
const RE_OL = /^(\s*)(\d{1,9})[.)]\s+(.*)$/;
const RE_TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function isTableRow(line) {
  return /\|/.test(line) && line.trim().length > 2;
}

function splitRow(line) {
  const body = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return body.split("|").map((cell) => cell.trim());
}

/** 列表：同缩进连续行归为一组，缩进更深的当作子列表 */
function buildList(lines, start) {
  const baseIndent = lines[start].match(RE_UL)?.[1].length ?? lines[start].match(RE_OL)?.[1].length ?? 0;
  const ordered = RE_OL.test(lines[start]) && !RE_UL.test(lines[start]);
  const items = [];
  let i = start;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      const next = lines[i + 1] || "";
      if (RE_UL.test(next) || RE_OL.test(next)) {
        i += 1;
        continue;
      }
      break;
    }
    const ul = line.match(RE_UL);
    const ol = line.match(RE_OL);
    const match = ul || ol;
    if (!match) {
      // 续行（懒换行）并入上一条
      if (items.length && /^\s{2,}/.test(line)) {
        items[items.length - 1].text += " " + line.trim();
        i += 1;
        continue;
      }
      break;
    }
    const indent = match[1].length;
    if (indent < baseIndent) break;
    if (indent > baseIndent) {
      const sub = buildList(lines, i);
      items[items.length - 1].children = sub.html;
      i = sub.next;
      continue;
    }
    items.push({ text: ul ? match[2] : match[3], children: "" });
    i += 1;
  }
  const tag = ordered ? "ol" : "ul";
  const html = `<${tag}>${items
    .map((it) => `<li>${inline(it.text)}${it.children}</li>`)
    .join("")}</${tag}>`;
  return { html, next: i };
}

export function renderMarkdown(source) {
  const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      i += 1;
      continue;
    }

    const fence = line.match(RE_FENCE);
    if (fence) {
      const marker = fence[1][0];
      const lang = fence[2];
      const body = [];
      i += 1;
      while (i < lines.length && !new RegExp(`^\\s*${marker}{3,}\\s*$`).test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // 跳过收尾的 ```
      const cls = lang ? ` class="language-${escapeHtml(lang)}"` : "";
      out.push(`<pre class="md-code"><code${cls}>${escapeHtml(body.join("\n"))}</code></pre>`);
      continue;
    }

    const heading = line.match(RE_HEADING);
    if (heading) {
      const level = heading[1].length;
      out.push(`<h${level}>${inline(heading[2].replace(/\s+#+\s*$/, ""))}</h${level}>`);
      i += 1;
      continue;
    }

    if (RE_HR.test(line)) {
      out.push("<hr>");
      i += 1;
      continue;
    }

    if (RE_QUOTE.test(line)) {
      const quote = [];
      while (i < lines.length && RE_QUOTE.test(lines[i])) {
        quote.push(lines[i].match(RE_QUOTE)[1]);
        i += 1;
      }
      out.push(`<blockquote>${renderMarkdown(quote.join("\n"))}</blockquote>`);
      continue;
    }

    // 表格：表头 + 分隔行
    if (isTableRow(line) && RE_TABLE_SEP.test(lines[i + 1] || "")) {
      const head = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && isTableRow(lines[i])) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      const th = head.map((c) => `<th>${inline(c)}</th>`).join("");
      const tb = rows
        .map((row) => `<tr>${row.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`)
        .join("");
      out.push(`<div class="md-table"><table><thead><tr>${th}</tr></thead><tbody>${tb}</tbody></table></div>`);
      continue;
    }

    if (RE_UL.test(line) || RE_OL.test(line)) {
      const list = buildList(lines, i);
      out.push(list.html);
      i = list.next;
      continue;
    }

    // 段落：连续普通行合并
    const para = [];
    while (i < lines.length) {
      const cur = lines[i];
      if (!cur.trim() || RE_FENCE.test(cur) || RE_HEADING.test(cur) || RE_HR.test(cur) ||
          RE_QUOTE.test(cur) || RE_UL.test(cur) || RE_OL.test(cur)) {
        break;
      }
      if (isTableRow(cur) && RE_TABLE_SEP.test(lines[i + 1] || "")) break;
      para.push(cur.trim());
      i += 1;
    }
    if (para.length) out.push(`<p>${para.map(inline).join("<br>")}</p>`);
  }

  return out.join("\n");
}
