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

/* ---------------- 轻量对话框（自定义，不用原生弹窗） ---------------- */
function showModal({ title, body, confirmText = "确定", danger = false, onConfirm }) {
  const backdrop = document.createElement("div");
  backdrop.className = "share-modal__backdrop";
  backdrop.innerHTML = `
    <div class="share-modal" role="dialog" aria-modal="true">
      <h2 class="share-modal__title">${esc(title)}</h2>
      <div class="share-modal__body">${body}</div>
      <div class="share-modal__foot">
        <button class="btn" type="button" data-cancel>取消</button>
        <button class="btn ${danger ? "btn--danger" : "btn--accent"}" type="button" data-ok>${esc(confirmText)}</button>
      </div>
    </div>`;
  const close = () => backdrop.remove();
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) close();
  });
  backdrop.querySelector("[data-cancel]").addEventListener("click", close);
  backdrop.querySelector("[data-ok]").addEventListener("click", async () => {
    const okBtn = backdrop.querySelector("[data-ok]");
    okBtn.disabled = true;
    try {
      await onConfirm?.(backdrop);
      close();
    } catch (err) {
      const tip = backdrop.querySelector("[data-modal-error]");
      if (tip) {
        tip.textContent = err.message || "操作失败";
        tip.hidden = false;
      }
      okBtn.disabled = false;
    }
  });
  document.body.appendChild(backdrop);
  return backdrop;
}

function infoModal(title, message) {
  const backdrop = document.createElement("div");
  backdrop.className = "share-modal__backdrop";
  backdrop.innerHTML = `
    <div class="share-modal" role="dialog" aria-modal="true">
      <h2 class="share-modal__title">${esc(title)}</h2>
      <div class="share-modal__body">${message}</div>
      <div class="share-modal__foot">
        <button class="btn btn--accent" type="button" data-ok>好的</button>
      </div>
    </div>`;
  const close = () => backdrop.remove();
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) close();
  });
  backdrop.querySelector("[data-ok]").addEventListener("click", close);
  document.body.appendChild(backdrop);
}

/* ---------------- 登录态 ---------------- */
let me = null; // 登录用户信息；null = 未登录
async function loadMe() {
  try {
    const res = await fetch("/api/me", { credentials: "include" });
    if (res.ok) me = await res.json();
  } catch {
    me = null;
  }
}

/* ---------------- 举报 ---------------- */
function openReport() {
  showModal({
    title: "举报分享",
    body: `
      <p class="share-modal__desc">如果该分享包含违规内容，请提交举报。管理员审核确认后会禁止该文件的分享，你的举报会匿名处理。</p>
      <label class="share-modal__label" for="report-reason">举报理由（选填）</label>
      <textarea class="share-modal__textarea" id="report-reason" maxlength="200"
                placeholder="例如：包含违法违规内容 / 侵权 / 垃圾广告"></textarea>
      <p class="share-modal__error" data-modal-error hidden></p>`,
    confirmText: "提交举报",
    danger: true,
    onConfirm: async (backdrop) => {
      const reason = backdrop.querySelector("#report-reason").value.trim();
      await request("/report", { method: "POST", body: { reason } });
      infoModal("举报已提交", `<p class="share-modal__desc">感谢你的反馈，管理员会尽快审核处理。</p>`);
    },
  });
}

/* ---------------- 转存到我的网盘 ---------------- */
async function saveToMyDrive(nodeId) {
  if (!me) {
    infoModal(
      "需要登录",
      `<p class="share-modal__desc">转存到我的网盘需要先登录账号。</p>
       <div class="share-modal__foot" style="margin-top:14px">
         <a class="btn btn--accent" href="/?next=${encodeURIComponent(location.pathname)}">去登录</a>
       </div>`
    );
    return;
  }
  const name = meta?.name || "文件";
  showModal({
    title: "转存到我的网盘",
    body: `
      <p class="share-modal__desc">将「<b>${esc(name)}</b>」保存到自己网盘的根目录。文件不会占用额外存储（相同内容只存一份），但会占用你的空间配额。</p>
      <p class="share-modal__error" data-modal-error hidden></p>`,
    confirmText: "转存",
    onConfirm: async () => {
      const data = await fetch(apiUrl("/save", nodeId ? { nodeId } : {}), {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(nodeId ? { nodeId } : {}),
      });
      const payload = await data.json().catch(() => null);
      if (!data.ok) throw new Error(payload?.detail || `转存失败（${data.status}）`);
      infoModal(
        "转存成功",
        `<p class="share-modal__desc">「<b>${esc(payload.name || name)}</b>」已保存到你的网盘根目录。</p>`
      );
    },
  });
}

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
        <div class="share-card__actions">
          <button class="btn" type="button" data-save-drive title="转存到我的网盘">
            ${icon("i-cloud")}<span>转存</span>
          </button>
          <button class="btn" type="button" data-report title="举报该分享">
            ${icon("i-alert")}<span>举报</span>
          </button>
          <a class="btn btn--accent" href="${downloadUrl}" download>
            ${icon("i-download")}<span>下载</span>
          </a>
        </div>
      </header>
      ${previewMarkup(share) || `<div class="share-nopreview">这类文件暂不支持在线预览，请下载后查看。</div>`}
      ${
        share.remainingDownloads !== null
          ? `<p class="share-tip">剩余可下载 ${share.remainingDownloads} 次</p>`
          : ""
      }
    </section>`;

  root.querySelector("[data-save-drive]").addEventListener("click", () => saveToMyDrive());
  root.querySelector("[data-report]").addEventListener("click", openReport);

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
        <div class="share-card__actions">
          <button class="btn" type="button" data-save-drive title="转存整个文件夹到我的网盘">
            ${icon("i-cloud")}<span>转存</span>
          </button>
          <button class="btn" type="button" data-report title="举报该分享">
            ${icon("i-alert")}<span>举报</span>
          </button>
        </div>
      </header>
      <div class="share-items">${rows}</div>
    </section>`;

  root.querySelector("[data-save-drive]").addEventListener("click", () => saveToMyDrive(folder.isRoot ? null : folder.id));
  root.querySelector("[data-report]").addEventListener("click", openReport);

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
  loadMe(); // 并行拿登录态，供「转存」按钮判断
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
    if (err.status === 403) renderError("分享已被禁止访问", err.message || "该文件因违规被管理员禁止分享。");
    else if (err.status === 410) renderError("分享已失效", err.message);
    else if (err.status === 404) renderError("分享不存在", "链接可能输入有误，或者分享者已取消分享。");
    else renderError("打开失败", err.message || "请稍后重试。");
  }
}

boot();
