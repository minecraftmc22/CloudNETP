/**
 * 公开快传页：一个页面两种角色，靠路径区分。
 *
 *   /pickup?code=ABC123   免登录取件：输 6 位码 → 列文件 → 下载
 *   /drop/<token>         扫码直传：手机扫码后往会话主人的目录里传文件
 *
 * 两页都不依赖登录态，与主应用共用主题 token 与 i18n 字典。
 */
import { registerPwa } from "./pwa.js";
import { H, initI18n, langTag, t } from "./i18n.js";

registerPwa();
initI18n();

const root = document.getElementById("quick-root");
const dropToken = (location.pathname.match(/\/drop\/([^/?#]+)/) || [])[1] || "";
const mode = dropToken ? "drop" : "pickup";

/* ---------------- 主题（与分享页同一套兜底） ---------------- */
(function applyTheme() {
  const el = document.documentElement;
  let cached = null;
  try {
    cached = JSON.parse(localStorage.getItem("cn:appearance") || "null");
  } catch {
    cached = null;
  }
  const preferDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const themeMode = cached?.themeMode ?? 2;
  const theme = themeMode === 0 ? "light" : themeMode === 1 ? "dark" : preferDark ? "dark" : "light";
  el.dataset.theme = theme;
  el.dataset.wallpaper = cached?.wallpaper || "sakura";
  if (cached?.accentColor) el.style.setProperty("--accent-user", cached.accentColor);
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", theme === "dark" ? "#150f21" : "#fdf4fa");
})();

/* ---------------- 工具 ---------------- */
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );

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

function expiryText(iso) {
  if (!iso) return t("长期有效");
  return t("{date} 到期", { date: new Date(iso).toLocaleString(langTag()) });
}

async function apiGet(path) {
  const res = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(data?.detail || t("请求失败（{status}）", { status: res.status }));
  return data;
}

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: body === undefined ? { Accept: "application/json" } : { "Content-Type": "application/json", Accept: "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(data?.detail || t("请求失败（{status}）", { status: res.status }));
  return data;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function card(inner, { center = false } = {}) {
  root.innerHTML = H`<section class="share-card${center ? " share-card--center" : ""}">${inner}</section>`;
  return root.querySelector("section");
}

/* ================================================================== */
/* 取件模式                                                            */
/* ================================================================== */
const codeFromUrl = (new URLSearchParams(location.search).get("code") || "").trim().toUpperCase();

function renderPickupForm(prefill = "", error = "") {
  card(
    H`
    <div class="quick-pub">
      <div class="quick-pub__head">
        <svg class="icon"><use href="#i-lock"/></svg>
        <span data-i18n="免登录取件">${t("免登录取件")}</span>
      </div>
      <div class="quick-pub__title" data-i18n="输入取件码取文件">${t("输入取件码取文件")}</div>
      <div class="quick-pub__sub" data-i18n="取件码是 6 位字母或数字，向发件人要一下即可；不用注册，也不会看到发件人的网盘。">
        ${t("取件码是 6 位字母或数字，向发件人要一下即可；不用注册，也不会看到发件人的网盘。")}
      </div>
      <form class="quick-fields" data-form>
        <label class="quick-field" style="flex:1">
          <span data-i18n="取件码">${t("取件码")}</span>
          <input class="input" name="code" maxlength="12" autocomplete="off" spellcheck="false"
                 style="letter-spacing:.28em;font-family:var(--font-mono);font-size:18px;text-transform:uppercase"
                 value="${esc(prefill)}" placeholder="ABC123" required>
        </label>
        <button class="btn btn--accent" type="submit">
          <svg class="icon icon--s"><use href="#i-arrow-up-right"/></svg>
          <span data-i18n="取件">${t("取件")}</span>
        </button>
      </form>
      ${error ? H`<div class="quick-pub__err"><svg class="icon icon--s"><use href="#i-alert"/></svg>${esc(error)}</div>` : ""}
      <div class="quick-note" data-i18n="取件码由文件主人设置有效期，过期或撤销后无法再打开。">
        ${t("取件码由文件主人设置有效期，过期或撤销后无法再打开。")}
      </div>
    </div>`
  );

  const form = root.querySelector("[data-form]");
  const input = form.querySelector('input[name="code"]');
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const code = input.value.trim().toUpperCase();
    if (code) loadPickup(code);
  });
  input.focus();
}

function renderPickup(data) {
  const items = data.items || [];
  card(
    H`
    <div class="quick-pub">
      <div class="quick-pub__head">
        <svg class="icon"><use href="#i-bolt"/></svg>
        <span>${esc(data.owner)} <span data-i18n="发来的文件">${t("发来的文件")}</span></span>
      </div>
      <div class="quick-pub__title">${esc(data.name)}</div>
      <div class="quick-pub__sub">
        ${items.length} 个文件 · ${formatBytes(data.size)} · ${expiryText(data.expiresAt)}${
          data.remaining === null || data.remaining === undefined
            ? ""
            : ` · ${t("还能取 {n} 次", { n: data.remaining })}`
        }
      </div>
      <div class="quick-items">
        ${items
          .map(
            (item) => H`
          <div class="quick-item">
            <span class="quick-row__icon"><svg class="icon"><use href="#${item.icon}"/></svg></span>
            <div class="quick-item__main">
              <div class="quick-item__name" title="${esc(item.name)}">${esc(item.name)}</div>
              <div class="quick-item__meta">${formatBytes(item.size)}</div>
            </div>
            <a class="btn btn--subtle" href="/api/public/quick/pickup/${esc(data.code)}/download?nodeId=${item.id}">
              <svg class="icon icon--s"><use href="#i-download"/></svg>
              <span data-i18n="下载">下载</span>
            </a>
          </div>`
          )
          .join("")}
      </div>
      <div class="quick-actions" style="justify-content:center">
        <button class="btn btn--subtle" type="button" data-act="again">
          <svg class="icon icon--s"><use href="#i-back"/></svg>
          <span data-i18n="换个取件码">${t("换个取件码")}</span>
        </button>
      </div>
      <div class="quick-note" data-i18n="文件由 CloudNETP 直接提供，不经过任何第三方服务器。">
        ${t("文件由 CloudNETP 直接提供，不经过任何第三方服务器。")}
      </div>
    </div>`
  );
  root.querySelector('[data-act="again"]').addEventListener("click", () => {
    history.replaceState(null, "", "/pickup");
    renderPickupForm();
  });
}

async function loadPickup(code) {
  card(
    H`<div class="share-skeleton"><span class="spinner"></span><span>${t("正在查询取件码…")}</span></div>`
  );
  try {
    const data = await apiGet(`/api/public/quick/pickup/${encodeURIComponent(code)}`);
    history.replaceState(null, "", `/pickup?code=${encodeURIComponent(code)}`);
    renderPickup(data.pickup);
  } catch (err) {
    renderPickupForm(code, err.message);
  }
}

/* ================================================================== */
/* 扫码直传模式（手机端上传）                                          */
/* ================================================================== */
const uploads = [];

function renderDrop(data) {
  card(
    H`
    <div class="quick-pub">
      <div class="quick-pub__head">
        <svg class="icon"><use href="#i-bolt"/></svg>
        <span data-i18n="扫码直传">${t("扫码直传")}</span>
      </div>
      <div class="quick-pub__title">${t("发送到 {owner} 的「{folder}」", {
        owner: esc(data.owner),
        folder: esc(data.folder),
      })}</div>
      <div class="quick-pub__sub">${expiryText(data.expiresAt)} · ${t("文件直接存进这台服务器，手机不用登录")}</div>

      <div class="quick-drop" data-drop tabindex="0" role="button">
        <svg class="icon"><use href="#i-upload"/></svg>
        <div class="quick-drop__title" data-i18n="点这里选文件，或把文件拖进来">${t("点这里选文件，或把文件拖进来")}</div>
        <div class="quick-drop__hint" data-i18n="支持一次选多个；大文件会自动分片续传">${t("支持一次选多个；大文件会自动分片续传")}</div>
        <input type="file" multiple hidden data-input>
      </div>

      <div class="quick-uploading" data-uploads></div>
      <div class="quick-items" data-done></div>
    </div>`
  );

  const zone = root.querySelector("[data-drop]");
  const input = root.querySelector("[data-input]");
  const upBox = root.querySelector("[data-uploads]");
  const doneBox = root.querySelector("[data-done]");

  const pick = () => input.click();
  zone.addEventListener("click", pick);
  zone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      pick();
    }
  });
  ["dragenter", "dragover"].forEach((name) =>
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.add("is-over");
    })
  );
  ["dragleave", "drop"].forEach((name) =>
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.remove("is-over");
    })
  );
  zone.addEventListener("drop", (event) => {
    const files = [...(event.dataTransfer?.files || [])];
    if (files.length) uploadAll(files);
  });
  input.addEventListener("change", () => {
    const files = [...input.files];
    input.value = "";
    if (files.length) uploadAll(files);
  });

  function paintDone() {
    if (!doneBox) return;
    const rows = uploads.filter((u) => u.state === "done");
    doneBox.innerHTML = rows
      .map(
        (u) => H`
        <div class="quick-item">
          <span class="quick-row__icon"><svg class="icon"><use href="#i-check"/></svg></span>
          <div class="quick-item__main">
            <div class="quick-item__name" title="${esc(u.name)}">${esc(u.name)}</div>
            <div class="quick-item__meta">${formatBytes(u.size)}</div>
          </div>
        </div>`
      )
      .join("");
  }

  function makeRow(upload) {
    const row = document.createElement("div");
    row.className = "quick-up";
    row.innerHTML = H`
      <div class="quick-up__top">
        <span class="quick-up__name" title="${esc(upload.name)}">${esc(upload.name)}</span>
        <span data-pct>0%</span>
      </div>
      <div class="quick-up__bar"><span></span></div>`;
    upBox.append(row);
    return row;
  }

  async function uploadOne(upload) {
    const row = makeRow(upload);
    const bar = row.querySelector(".quick-up__bar span");
    const pct = row.querySelector("[data-pct]");
    const setPct = (value) => {
      const safe = Math.max(0, Math.min(100, Math.round(value * 100)));
      bar.style.width = `${safe}%`;
      pct.textContent = `${safe}%`;
    };

    try {
      const init = await apiPost(`/api/public/quick/drop/${dropToken}/init`, {
        name: upload.file.name,
        size: upload.file.size,
        mime: upload.file.type || "",
        chunkSize: 0,
      });
      if (init.instant) {
        setPct(1);
        upload.state = "done";
        row.classList.add("is-done");
        paintDone();
        return;
      }

      const chunkSize = init.chunkSize || 4 * 1024 * 1024;
      const total = init.totalChunks || Math.max(1, Math.ceil(upload.file.size / chunkSize));
      const missing = init.missing || [...Array(total).keys()];
      let finished = total - missing.length;

      for (const index of missing) {
        const start = index * chunkSize;
        const blob = upload.file.slice(start, Math.min(start + chunkSize, upload.file.size));
        const res = await fetch(
          `/api/public/quick/drop/${dropToken}/${init.sessionId}/${index}`,
          {
            method: "PUT",
            body: blob,
            credentials: "same-origin",
            headers: { "Content-Type": "application/octet-stream" },
          }
        );
        if (!res.ok) {
          const data = await res.json().catch(() => null);
          throw new Error(data?.detail || t("分片上传失败（{status}）", { status: res.status }));
        }
        finished += 1;
        setPct(finished / total);
      }

      await apiPost(`/api/public/quick/drop/${dropToken}/${init.sessionId}/complete`);
      upload.state = "done";
      row.classList.add("is-done");
      setPct(1);
      paintDone();
    } catch (err) {
      upload.state = "error";
      row.classList.add("is-error");
      pct.textContent = err.message;
      pct.style.color = "var(--danger)";
      pct.style.maxWidth = "60%";
      pct.style.overflow = "hidden";
      pct.style.textOverflow = "ellipsis";
      pct.style.whiteSpace = "nowrap";
    }
  }

  async function uploadAll(files) {
    for (const file of files) {
      if (!file.size) continue;
      const upload = { file, name: file.name, size: file.size, state: "uploading" };
      uploads.push(upload);
      await uploadOne(upload);
    }
  }
}

/* ================================================================== */
/* 启动                                                               */
/* ================================================================== */
async function boot() {
  document.documentElement.lang = langTag();
  try {
    const brand = await fetch("/api/meta", { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);
    const name = brand?.appName;
    if (name) {
      document.querySelectorAll("[data-brand]").forEach((el) => (el.textContent = name));
      document.title = `${t("快传")} · ${name}`;
    }
  } catch {
    /* 站名拿不到不影响使用 */
  }

  if (mode === "drop") {
    try {
      const data = await apiGet(`/api/public/quick/drop/${encodeURIComponent(dropToken)}`);
      renderDrop(data.drop);
    } catch (err) {
      card(
        H`
        <div class="empty">
          <div class="empty__art"><svg class="icon"><use href="#i-alert"/></svg></div>
          <div class="empty__title">${t("这个快传打不开了")}</div>
          <div class="empty__desc">${esc(err.message)}</div>
        </div>`,
        { center: true }
      );
    }
    return;
  }

  if (codeFromUrl) loadPickup(codeFromUrl);
  else renderPickupForm();
}

boot();
