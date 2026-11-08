/**
 * 前端 i18n：零依赖、零构建。
 *
 * 设计取舍
 * - key 直接用中文原文（gettext 的 msgid 风格），查不到就原样返回：
 *   漏翻的句子仍然可读，翻译表只是「中文 → 译文」，不用额外维护一套 key 命名。
 * - 字典按语言拆文件（./i18n/en-US.js），加语言 = LANGS 加一项 + 建一个字典文件。
 * - 服务端只存语言 ID（zh-CN / en-US），不存文案。
 * - 语言优先级：账号设置 → 本机 localStorage → 浏览器语言 → 中文。
 */

import enUS from "./i18n/en-US.js";

export const LANGS = [
  { id: "zh-CN", label: "简体中文", short: "中" },
  { id: "en-US", label: "English", short: "EN" },
];

const DICTS = { "en-US": enUS };
const STORAGE_KEY = "cloudnetp.lang";

let current = "zh-CN";

/* ------------------------------------------------------------------ */
/* 语言选择                                                           */
/* ------------------------------------------------------------------ */
export function normalizeLang(id) {
  const value = String(id || "").trim().toLowerCase();
  if (!value) return "";
  if (value.startsWith("zh")) return "zh-CN";
  if (value.startsWith("en")) return "en-US";
  return "";
}

function storedLang() {
  try {
    return normalizeLang(localStorage.getItem(STORAGE_KEY));
  } catch {
    return "";
  }
}

export function detectLang(preferred) {
  const fromAccount = normalizeLang(preferred);
  if (fromAccount) return fromAccount;
  const saved = storedLang();
  if (saved) return saved;
  // 浏览器语言：navigator.languages 是按偏好排序的，逐个认，认不出就下一个
  const tags = navigator.languages?.length ? navigator.languages : [navigator.language || ""];
  for (const tag of tags) {
    const id = normalizeLang(tag);
    if (id) return id;
  }
  return "zh-CN";
}

export function getLang() {
  return current;
}

/** BCP-47 标签，给 Intl / <html lang> 用 */
export function langTag() {
  return current;
}

export function setLang(id, { persist = true, silent = false } = {}) {
  const next = normalizeLang(id) || "zh-CN";
  const changed = next !== current;
  current = next;
  document.documentElement.lang = next;
  if (persist) {
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* 隐私模式写不了就算了，本次会话仍然生效 */
    }
  }
  applyStatic();
  if (changed && !silent) {
    window.dispatchEvent(new CustomEvent("i18n:change", { detail: { lang: next } }));
  }
  return next;
}

/** 启动时调用：按账号 / 本机 / 浏览器选定语言，并把静态文案翻好 */
export function initI18n(preferred) {
  const id = setLang(detectLang(preferred), { persist: false, silent: true });
  applyStatic();
  return id;
}

/* ------------------------------------------------------------------ */
/* 模板标记：H`<div>中文</div>`                                       */
/* ------------------------------------------------------------------ */
const CJK = /[\u4e00-\u9fa5]/;
// 标签之间的文字（不含子标签）
const TEXT_RE = />([^<>]*[\u4e00-\u9fa5][^<>]*)</g;
// 带中文的属性值
const ATTR_RE = /\b(title|aria-label|placeholder|alt)="([^"]*[\u4e00-\u9fa5][^"]*)"/g;

const normKey = (text) => String(text).replace(/\s+/g, " ").trim();

function lookup(raw) {
  const key = normKey(raw);
  if (!key) return null;
  const dict = DICTS[current];
  if (!dict || !Object.prototype.hasOwnProperty.call(dict, key)) return null;
  return dict[key];
}

/**
 * 翻译一段 HTML 片段里的静态文案。
 * 只处理「标签之间的文字」和 title/aria-label/placeholder/alt 属性，
 * 不会碰标签名、class、样式，所以对结构化 HTML 是安全的。
 */
export function translateMarkup(chunk) {
  if (!chunk || current === "zh-CN" || !CJK.test(chunk)) return chunk;
  return chunk
    .replace(TEXT_RE, (whole, inner) => {
      const translated = lookup(inner);
      if (!translated) return whole;
      // 保留原有的缩进与换行，避免打乱排版
      const lead = inner.match(/^\s*/)[0];
      const trail = inner.match(/\s*$/)[0];
      return `>${lead}${translated}${trail}<`;
    })
    .replace(ATTR_RE, (whole, name, value) => {
      const translated = lookup(value);
      return translated ? `${name}="${translated.replace(/"/g, "&quot;")}"` : whole;
    });
}

/**
 * 模板标签：H`<div class="x">主题模式</div>`
 *
 * 关键点是**逐段翻译**：插值 ${...} 里的内容（文件名、用户名等用户数据）原样拼回去，
 * 永远不会被当成界面文案翻译；嵌套模板自己套一层 H 即可。
 */
export function H(strings, ...values) {
  let out = translateMarkup(strings[0]);
  for (let i = 0; i < values.length; i += 1) {
    out += String(values[i] ?? "") + translateMarkup(strings[i + 1] ?? "");
  }
  return out;
}


/* ------------------------------------------------------------------ */
/* 取词                                                               */
/* ------------------------------------------------------------------ */
function fill(text, vars) {
  if (!vars) return text;
  return text.replace(/\{(\w+)\}/g, (match, key) =>
    Object.prototype.hasOwnProperty.call(vars, key) ? String(vars[key]) : match
  );
}

/**
 * 取译文。params 用 {name} 占位。
 * 没有对应译文时返回原文，所以「翻一半」也不会出现空白。
 */
export function t(text, vars) {
  if (typeof text !== "string" || !text) return text;
  const dict = DICTS[current];
  const hit = dict && Object.prototype.hasOwnProperty.call(dict, text) ? dict[text] : text;
  return fill(hit, vars);
}

/** 是否已经有这条译文（调试 / 覆盖率统计用） */
export function hasTranslation(text) {
  const dict = DICTS[current];
  return Boolean(dict && Object.prototype.hasOwnProperty.call(dict, text));
}

/* ------------------------------------------------------------------ */
/* 静态 DOM 翻译                                                      */
/* ------------------------------------------------------------------ */
/**
 * 翻译带 data-i18n* 属性的节点。index.html 的静态外壳靠它，
 * 视图自己拼的 HTML 一般在模板里直接调 t()。
 */
export function applyStatic(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  root.querySelectorAll("[data-i18n-html]").forEach((el) => {
    el.innerHTML = t(el.dataset.i18nHtml);
  });
  root.querySelectorAll("[data-i18n-title]").forEach((el) => {
    el.title = t(el.dataset.i18nTitle);
  });
  root.querySelectorAll("[data-i18n-aria]").forEach((el) => {
    el.setAttribute("aria-label", t(el.dataset.i18nAria));
  });
  root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
}

/** 视图重绘后重新套一遍静态文案（视图里若已用 t() 可省略） */
export function watchI18n(handler) {
  window.addEventListener("i18n:change", handler);
}
