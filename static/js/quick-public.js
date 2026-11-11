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

/** 取件页只内联了一小撮图标；后端给的类型图标不在里面时退回 i-file */
function iconName(name) {
  return name && document.getElementById(name) ? name : "i-file";
}

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
  if (!res.ok) {
    const err = new Error(data?.detail || t("请求失败（{status}）", { status: res.status }));
    err.status = res.status;
    throw err;
  }
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

/* ---------------- 轻量弹层（与分享页同一套 .share-modal 样式） ---------------- */
function openModal({ title, body, confirmText, cancelText = "取消", danger = false, onConfirm }) {
  const backdrop = document.createElement("div");
  backdrop.className = "share-modal__backdrop";
  backdrop.innerHTML = H`
    <div class="share-modal" role="dialog" aria-modal="true">
      <h2 class="share-modal__title">${esc(t(title))}</h2>
      <div class="share-modal__body">${body}</div>
      <div class="share-modal__foot">
        ${cancelText ? H`<button class="btn" type="button" data-cancel>${t(cancelText)}</button>` : ""}
        <button class="btn ${danger ? "btn--danger" : "btn--accent"}" type="button" data-ok>${esc(t(confirmText))}</button>
      </div>
    </div>`;
  document.body.append(backdrop);
  const close = () => backdrop.remove();
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) close();
  });
  const onKey = (event) => {
    if (event.key === "Escape") {
      close();
      document.removeEventListener("keydown", onKey);
    }
  };
  document.addEventListener("keydown", onKey);
  backdrop.querySelector("[data-cancel]")?.addEventListener("click", () => {
    close();
    document.removeEventListener("keydown", onKey);
  });
  backdrop.querySelector("[data-ok]").addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    const errBox = backdrop.querySelector("[data-modal-error]");
    btn.disabled = true;
    try {
      await onConfirm(backdrop);
      close();
      document.removeEventListener("keydown", onKey);
    } catch (err) {
      btn.disabled = false;
      if (errBox) {
        errBox.textContent = err.message;
        errBox.hidden = false;
      }
    }
  });
  return backdrop;
}

/** 举报取件码里的某个文件：匿名提交，管理员审核后拉黑哈希 */
function openReport(item, code) {
  openModal({
    title: "举报这个文件",
    danger: true,
    confirmText: "提交举报",
    body: H`
      <p class="share-modal__desc">${t(
        "如果这个文件违反法律法规或侵犯了你的权益，请提交举报。管理员审核确认后会禁止该文件被分享与下载，举报是匿名的。"
      )}</p>
      <p class="share-modal__desc">${esc(item.name)}</p>
      <label class="share-modal__label" for="quick-report-reason">${t("举报理由（选填）")}</label>
      <textarea class="share-modal__textarea" id="quick-report-reason" maxlength="200"
                placeholder="${t("例如：包含违法违规内容 / 侵权 / 垃圾广告")}"></textarea>
      <p class="share-modal__error" data-modal-error hidden></p>`,
    onConfirm: async (backdrop) => {
      const reason = backdrop.querySelector("#quick-report-reason").value.trim();
      await apiPost(`/api/public/quick/pickup/${encodeURIComponent(code)}/report`, {
        nodeId: item.id,
        reason: reason || null,
      });
      openModal({
        title: "举报已提交",
        confirmText: "好的",
        cancelText: "",
        body: H`<p class="share-modal__desc">${t("感谢你的反馈，管理员会尽快审核处理。")}</p>`,
        onConfirm: () => {},
      });
    },
  });
}

/** 当前登录用户；未登录返回 null（/api/me 401 是正常情况） */
async function currentUser() {
  try {
    const res = await fetch("/api/me", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!res.ok) return null;
    const data = await res.json();
    return data?.user || null;
  } catch {
    return null;
  }
}

/**
 * 扫码直传的上传闸门：必须登录，且必须是发起人本人。
 * 取件码不受影响——匿名下载仍然照旧。
 */
function renderLoginGate(kind) {
  const needLogin = kind === "login";
  card(
    H`
    <div class="quick-pub">
      <div class="quick-pub__head">
        <svg class="icon"><use href="#i-lock"/></svg>
        <span>${t("扫码直传")}</span>
      </div>
      <div class="quick-pub__title">${
        needLogin ? t("扫码直传需要先登录") : t("这条快传不是你的")
      }</div>
      <div class="quick-pub__sub">${
        needLogin
          ? t("上传会记在你的配额上，所以链接外泄也不会被人往你的网盘里塞东西。登录后会自动回到这个页面。")
          : t("只有发起人本人能往这个目录上传。想取别人发给你的文件，用取件码就行。")
      }</div>
      <div class="quick-actions" style="justify-content:center">
        ${
          needLogin
            ? H`<a class="btn btn--accent" href="/?next=${encodeURIComponent(
                location.pathname + location.search
              )}">
                <svg class="icon icon--s"><use href="#i-arrow-up-right"/></svg>
                <span>${t("登录 / 注册")}</span>
              </a>`
            : ""
        }
        <a class="btn btn--subtle" href="/pickup">
          <svg class="icon icon--s"><use href="#i-lock"/></svg>
          <span>${t("去取件")}</span>
        </a>
      </div>
      <div class="quick-note">${t("取件码下载不需要登录，只有往网盘里传才要。")}</div>
    </div>`
  );
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
            <span class="quick-row__icon"><svg class="icon"><use href="#${iconName(item.icon)}"/></svg></span>
            <div class="quick-item__main">
              <div class="quick-item__name" title="${esc(item.name)}">${esc(item.name)}</div>
              <div class="quick-item__meta">${formatBytes(item.size)}</div>
            </div>
            <button class="btn btn--subtle quick-item__report" type="button" data-report="${item.id}"
                    title="${t("举报该文件")}">
              <svg class="icon icon--s"><use href="#i-alert"/></svg>
              <span data-i18n="举报">${t("举报")}</span>
            </button>
            <a class="btn btn--subtle" href="/api/public/quick/pickup/${esc(data.code)}/download?nodeId=${item.id}">
              <svg class="icon icon--s"><use href="#i-download"/></svg>
              <span data-i18n="下载">${t("下载")}</span>
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
      <div class="quick-note quick-note--warn">
        ${t("发现违法或侵权内容，点文件旁的「举报」提交给管理员，核实后会禁止该文件继续被取件。")}
      </div>
    </div>`
  );
  root.querySelectorAll("[data-report]").forEach((btn) => {
    const item = items.find((it) => it.id === Number(btn.dataset.report));
    if (item) btn.addEventListener("click", () => openReport(item, data.code));
  });
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
      <div class="quick-pub__sub">${expiryText(data.expiresAt)} · ${t("已登录，文件直接存进这台服务器")}</div>
      <div class="quick-pub__warn">
        <svg class="icon icon--s"><use href="#i-alert"/></svg>
        <span>${t("禁止上传法律、法规禁止的文件与内容。违者封号，并依法追究法律责任；命中已确认的违规文件会被直接拒绝。")}</span>
      </div>

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
      // 失败原因（含「此文件包含法律禁止的内容，无法上传」）单独一行，别挤在进度数字里被截断
      upload.state = "error";
      row.classList.add("is-error");
      bar.style.width = "0%";
      pct.textContent = t("失败");
      pct.style.color = "var(--danger)";
      const msg = document.createElement("div");
      msg.className = "quick-up__err";
      msg.textContent = err.message;
      row.append(msg);
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
    // 上传必须登录：先问一次当前账号，未登录直接给登录入口（带 next 跳回来）
    const user = await currentUser();
    if (!user) {
      renderLoginGate("login");
      return;
    }
    try {
      const data = await apiGet(`/api/public/quick/drop/${encodeURIComponent(dropToken)}`);
      renderDrop(data.drop);
    } catch (err) {
      if (err.status === 403) {
        renderLoginGate("owner");
        return;
      }
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
