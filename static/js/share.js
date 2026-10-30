/**
 * 公开分享页：提取码校验 → 文件预览 / 文件夹浏览 → 下载。
 * 与主应用共用主题 token，但不依赖登录态与 store。
 */
import { registerPwa } from "./pwa.js";

registerPwa();

const root = document.getElementById("share-root");
const token = (location.pathname.match(/\/s\/([^/?#]+)/) || [])[1] || "";
const codeFromUrl = new URLSearchParams(location.search).get("code") || "";
let code = codeFromUrl;
let meta = null;
let trail = []; // 文件夹导航栈 [{id, name}]

/* ---------------- 主题 ---------------- */
(function applyTheme() {
  const el = document.documentElement;
  let cached = null;
  try {
    cached = JSON.parse(localStorage.getItem("cn:appearance") || "null");
  } catch {
    cached = null;
  }
  const preferDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const mode = cached?.themeMode ?? 2;
  const theme = mode === 0 ? "light" : mode === 1 ? "dark" : preferDark ? "dark" : "light";
  el.dataset.theme = theme;
  el.dataset.wallpaper = cached?.wallpaper || "sakura";
  if (cached?.accentColor) el.style.setProperty("--accent-user", cached.accentColor);
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "dark" ? "#150f21" : "#fdf4fa");
})();

/* ---------------- 工具 ---------------- */
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function formatBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = n / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}

function apiUrl(path, params = {}) {
  const url = new URL(`/api/public/shares/${token}${path}`, location.origin);
  if (code) url.searchParams.set("code", code);
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
  });
  return url.pathname + url.search;
}

async function request(path, options = {}) {
  const res = await fetch(apiUrl(path, options.params), {
    method: options.method || "GET",
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const type = res.headers.get("content-type") || "";
  const data = type.includes("json") ? await res.json().catch(() => null) : null;
  if (!res.ok) {
    const err = new Error((data && (data.detail || data.message)) || `请求失败（${res.status}）`);
    err.status = res.status;
    throw err;
  }
  return data;
}

const icon = (name) => `<svg class="icon"><use href="#${name}"/></svg>`;

/** 分享者标签：关闭「显示完整昵称」时后端只会给出前两个字，头像也不下发 */
function authorChip(share) {
  const a = share?.author;
  if (!a || !a.name) return "";
  const face = a.avatar
    ? `<img class="share-author__img" src="${esc(a.avatar)}" alt="">`
    : `<span class="share-author__initial">${esc(a.name.slice(0, 1).toUpperCase())}</span>`;
  return `<span class="share-author" title="${a.masked ? "分享者只公开昵称的前两位" : ""}">
      <span class="share-author__avatar">${face}</span>
      <span class="share-author__text">由 <b>${esc(a.name)}</b> 分享</span>
    </span>`;
}

/* ---------------- 状态渲染 ---------------- */
function renderError(title, desc) {
  root.innerHTML = `
    <section class="share-card share-card--center">
      <div class="share-empty__art">${icon("i-alert")}</div>
      <h1 class="share-empty__title">${esc(title)}</h1>
      <p class="share-empty__desc">${esc(desc)}</p>
    </section>`;
}

function renderGate() {
  root.innerHTML = `
    <section class="share-card share-card--center">
      <div class="share-empty__art">${icon("i-lock")}</div>
      <h1 class="share-empty__title">需要提取码</h1>
      <p class="share-empty__desc">「${esc(meta.name)}」受提取码保护，请输入提取码。</p>
      ${authorChip(meta)}
      <form class="share-gate" data-gate>
        <span class="textbox">
          <input type="text" name="code" maxlength="8" placeholder="请输入提取码"
                 autocomplete="off" spellcheck="false" autocapitalize="characters" data-code>
        </span>
        <button class="btn btn--accent" type="submit" data-submit>打开</button>
      </form>
      <p class="share-gate__error" data-error hidden></p>
    </section>`;

  const form = root.querySelector("[data-gate]");
  const input = root.querySelector("[data-code]");
  const errorEl = root.querySelector("[data-error]");
  input.focus();

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = input.value.trim();
    if (!value) {
      errorEl.textContent = "请先输入提取码";
      errorEl.hidden = false;
      return;
    }
    const btn = root.querySelector("[data-submit]");
    btn.disabled = true;
    errorEl.hidden = true;
    try {
      code = value;
      const data = await request("/unlock", { method: "POST", body: { code: value } });
      meta = data.share;
      await enter();
    } catch (err) {
      code = "";
      errorEl.textContent = err.message || "提取码不正确";
      errorEl.hidden = false;
      input.select();
    } finally {
      btn.disabled = false;
    }
  });
}

/* ---------------- 文件预览 ---------------- */
function previewMarkup(share) {
  const src = apiUrl("/preview");
  switch (share.previewKind) {
    case "image":
      return `<div class="share-preview share-preview--image">
          <img src="${src}" alt="${esc(share.name)}">
        </div>`;
    case "video":
      return `<div class="share-preview share-preview--video">
          <video src="${src}" controls preload="metadata" playsinline></video>
        </div>`;
    case "audio":
      return `<div class="share-preview share-preview--audio">
          <div class="share-audio__art">${icon("i-star")}</div>
          <audio src="${src}" controls preload="metadata"></audio>
        </div>`;
    case "pdf":
      return `<div class="share-preview share-preview--pdf">
          <iframe src="${src}" title="${esc(share.name)}"></iframe>
        </div>`;
    case "text":
      return `<div class="share-preview share-preview--text">
          <pre data-text>加载中…</pre>
        </div>`;
    default:
      return "";
  }
}

function renderFile(share) {
  const downloadUrl = apiUrl("/download");
  root.innerHTML = `
    <section class="share-card">
      <header class="share-file">
        <span class="share-file__icon">${icon(share.icon || "i-file")}</span>
        <div class="share-file__main">
          <h1 class="share-file__name" title="${esc(share.name)}">${esc(share.name)}</h1>
          <p class="share-file__meta">${formatBytes(share.size)}${
            share.expiresAt ? ` · ${new Date(share.expiresAt).toLocaleDateString("zh-CN")} 到期` : ""
          }</p>
          ${authorChip(share)}
        </div>
        <a class="btn btn--accent" href="${downloadUrl}" download>
          ${icon("i-download")}<span>下载</span>
        </a>
      </header>
      ${previewMarkup(share) || `<div class="share-nopreview">这类文件暂不支持在线预览，请下载后查看。</div>`}
      ${
        share.remainingDownloads !== null
          ? `<p class="share-tip">剩余可下载 ${share.remainingDownloads} 次</p>`
          : ""
      }
    </section>`;

  if (share.previewKind === "text") {
    const pre = root.querySelector("[data-text]");
    request("/text")
      .then((data) => {
        pre.textContent = data.content || "";
        if (data.truncated) pre.insertAdjacentHTML("afterend", `<p class="share-tip">仅显示前 1 MB 内容</p>`);
      })
      .catch((err) => {
        pre.textContent = `无法预览文本：${err.message}`;
      });
  }
}

/* ---------------- 文件夹浏览 ---------------- */
function renderFolder(folder, items) {
  const downloadUrl = (id) => apiUrl("/download", { nodeId: id });
  const previewUrl = (id) => apiUrl("/preview", { nodeId: id });

  const rows = items.length
    ? items
        .map((item) => {
          const isFolder = item.type === "folder";
          return `
        <div class="share-item" data-id="${item.id}" data-type="${item.type}">
          <span class="share-item__icon">${icon(item.icon || (isFolder ? "i-folder" : "i-file"))}</span>
          <span class="share-item__name" title="${esc(item.name)}">${esc(item.name)}</span>
          <span class="share-item__size">${isFolder ? `${item.childCount ?? 0} 项` : formatBytes(item.size)}</span>
          <span class="share-item__actions">
            ${
              isFolder
                ? ""
                : `<a class="icon-btn" href="${previewUrl(item.id)}" target="_blank" rel="noopener" title="在新标签页预览">
                     ${icon("i-arrow-up-right")}
                   </a>`
            }
            ${
              isFolder
                ? `<button class="icon-btn" type="button" data-enter title="打开文件夹">${icon("i-folder")}</button>`
                : `<a class="icon-btn" href="${downloadUrl(item.id)}" download title="下载">${icon("i-download")}</a>`
            }
          </span>
        </div>`;
        })
        .join("")
    : `<div class="share-empty__desc" style="text-align:center;padding:28px 0">这个文件夹是空的。</div>`;

  root.innerHTML = `
    <section class="share-card">
      <header class="share-folder__head">
        <div class="share-folder__title">
          ${
            trail.length > 1
              ? `<button class="icon-btn" type="button" data-back title="返回上一级">${icon("i-back")}</button>`
              : ""
          }
          <span class="share-folder__icon">${icon("i-folder")}</span>
          <div>
            <h1 class="share-file__name">${esc(folder.name)}</h1>
            <p class="share-file__meta">共 ${items.length} 项${
              meta?.expiresAt ? ` · ${new Date(meta.expiresAt).toLocaleDateString("zh-CN")} 到期` : ""
            }</p>
            ${authorChip(meta)}
          </div>
        </div>
      </header>
      <div class="share-items">${rows}</div>
    </section>`;

  root.querySelector("[data-back]")?.addEventListener("click", () => {
    trail.pop();
    loadFolder(trail[trail.length - 1].id);
  });

  root.querySelectorAll(".share-item").forEach((row) => {
    const id = Number(row.dataset.id);
    const type = row.dataset.type;
    const item = items.find((i) => i.id === id);
    row.querySelector("[data-enter]")?.addEventListener("click", () => {
      trail.push({ id, name: item.name });
      loadFolder(id);
    });
  });
}

/* ---------------- 加载 ---------------- */
async function loadFolder(folderId) {
  root.querySelector(".share-card")?.classList.add("is-loading");
  try {
    const data = await request("/list", { params: { folderId } });
    renderFolder(data.folder, data.items);
  } catch (err) {
    renderError("无法打开文件夹", err.message);
  }
}

async function enter() {
  if (meta.type === "folder") {
    trail = [{ id: null, name: meta.name }];
    await loadFolder(null);
  } else {
    renderFile(meta);
  }
}

async function boot() {
  if (!token) {
    renderError("链接不完整", "请确认分享链接是否复制完整。");
    return;
  }
  try {
    const data = await request("");
    meta = data.share;
    document.title = `${meta.name} · 分享`;
    document.querySelector("[data-brand]").textContent = "CloudNETP 分享";
    if (meta.hasCode && !meta.unlocked) {
      if (code) {
        // 链接里带 code，直接换授权
        try {
          const unlocked = await request("/unlock", { method: "POST", body: { code } });
          meta = unlocked.share;
          history.replaceState(null, "", location.pathname);
        } catch {
          renderGate();
          return;
        }
      } else {
        renderGate();
        return;
      }
    }
    await enter();
  } catch (err) {
    if (err.status === 410) renderError("分享已失效", err.message);
    else if (err.status === 404) renderError("分享不存在", "链接可能输入有误，或者分享者已取消分享。");
    else renderError("打开失败", err.message || "请稍后重试。");
  }
}

boot();
