/**
 * 用户协议 / 隐私政策页（独立入口，不依赖登录态）
 *
 * 只做四件事：填站点名与版本、填联系邮箱、按小节生成目录与序号、按界面语言给一行说明。
 * 正文写死在 HTML 里：这类文本要长期稳定，不宜走 i18n 运行时替换。
 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function uiLang() {
  try {
    const saved = localStorage.getItem("cloudnetp.lang");
    if (saved) return saved;
  } catch {
    /* 隐私模式下 localStorage 会抛错，忽略即可 */
  }
  return (navigator.language || "zh-CN").toLowerCase().startsWith("en") ? "en-US" : "zh-CN";
}

function buildToc() {
  const sections = $$(".legal-body section");
  if (!sections.length) return;
  sections.forEach((section, index) => {
    const h2 = section.querySelector("h2");
    if (!h2) return;
    section.id = `sec-${index + 1}`;
    if (!h2.querySelector(".legal-body__no")) {
      const no = document.createElement("span");
      no.className = "legal-body__no";
      no.textContent = String(index + 1).padStart(2, "0");
      h2.prepend(no);
    }
  });

  const toc = $("[data-toc]");
  const list = toc?.querySelector("ol");
  if (!toc || !list) return;
  sections.forEach((section, index) => {
    const h2 = section.querySelector("h2");
    if (!h2) return;
    const li = document.createElement("li");
    const link = document.createElement("a");
    link.href = `#${section.id}`;
    // 去掉刚才插进去的序号，目录只显示标题
    link.textContent = h2.textContent.replace(/^\s*\d+\s*/, "").trim();
    li.appendChild(link);
    list.appendChild(li);
  });
  toc.hidden = false;
}

function fillSite(meta) {
  const name = (meta.appName || "").trim() || "CloudNETP";
  $$("[data-site]").forEach((el) => {
    el.textContent = name;
  });
  if (document.title.includes("CloudNETP")) {
    document.title = document.title.replace("CloudNETP", name);
  }
  const versionEl = $("[data-version]");
  if (versionEl && meta.version) versionEl.textContent = `v${meta.version}`;

  const contact = (meta.contactEmail || "").trim();
  const link = $("[data-contact]");
  const empty = $("[data-contact-empty]");
  if (link && contact) {
    link.href = `mailto:${contact}`;
    const text = link.querySelector("[data-contact-text]");
    if (text) text.textContent = contact;
    link.hidden = false;
    if (empty) empty.hidden = true;
  }
}

async function main() {
  const en = uiLang() === "en-US";

  // 语言提示与返回按钮都不依赖网络，先处理，避免离线时一片空白
  if (en) {
    const note = $("[data-lang-note]");
    if (note) note.hidden = false;
    const backLabel = $("[data-back-label]");
    if (backLabel) backLabel.textContent = "Back to sign in";
    const tocTitle = $(".legal-toc__title");
    if (tocTitle) tocTitle.textContent = "Contents";
  }

  const back = $("[data-back]");
  if (back) {
    // 从登录页新标签打开时 referrer 是同源登录页，优先返回上一页
    const sameOrigin = (() => {
      try {
        return document.referrer && new URL(document.referrer).origin === location.origin;
      } catch {
        return false;
      }
    })();
    if (sameOrigin && history.length > 1) {
      back.addEventListener("click", (event) => {
        event.preventDefault();
        history.back();
      });
    }
  }

  buildToc();

  try {
    const res = await fetch("/api/meta", { credentials: "same-origin", cache: "no-store" });
    if (res.ok) fillSite(await res.json());
  } catch {
    /* 站点信息拿不到就保留 HTML 里的默认值，正文照常可读 */
  }
}

main();
