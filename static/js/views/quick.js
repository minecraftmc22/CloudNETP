/**
 * 快传：扫码直传 / 站内发文件 / 免登录取件码。
 *
 * 三个标签页共用一套记录接口：
 *   drop   一次性链接 + 二维码，手机扫码往指定目录传
 *   send   直接投递到另一个用户的网盘（复用 blob，不复制物理文件）
 *   pickup 6 位取件码，谁拿到都能下载，可设有效期与下载次数
 */
import { api, escapeHtml, formatBytes, formatTime } from "../api.js";
import { H, t } from "../i18n.js";
import { Dialog } from "../ui/dialog.js";
import { Toast } from "../ui/toast.js";
import { pickFolder } from "../ui/picker.js";
import { pickNodes } from "../ui/file-browser.js";
import { copyText } from "../ui/share.js";
import { siteOrigin } from "../store.js";

const DROP_MINUTES = [10, 30, 60, 120];
const PICKUP_DAYS = [1, 3, 7, 30];
const PICKUP_USES = [
  { value: null, label: "不限次数" },
  { value: 1, label: "1 次" },
  { value: 5, label: "5 次" },
  { value: 10, label: "10 次" },
];

const TABS = [
  { key: "drop", label: "扫码直传", icon: "i-upload", desc: "手机扫码，把相册里的东西直接传进网盘" },
  { key: "send", label: "发给用户", icon: "i-share", desc: "直接投递到对方的网盘根目录，不用提取码" },
  { key: "pickup", label: "取件码", icon: "i-key", desc: "生成 6 位码，谁拿到都能取，到期自动失效" },
];

const STATUS_TEXT = { active: "有效", expired: "已过期", revoked: "已取消" };

/* ------------------------------------------------------------------ */
/* 二维码：用到时才加载 vendor 里的库（56 KB，多数人不会开这个页面）      */
/* ------------------------------------------------------------------ */
let qrLoading = null;
function loadQrLib() {
  if (window.qrcode) return Promise.resolve();
  if (!qrLoading) {
    qrLoading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/static/vendor/qrcode.js";
      script.onload = () => resolve();
      script.onerror = () => reject(new Error(t("二维码组件加载失败")));
      document.head.append(script);
    });
  }
  return qrLoading;
}

async function paintQr(host, text) {
  try {
    await loadQrLib();
    const qr = window.qrcode(0, "M");
    qr.addData(text);
    qr.make();
    host.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 1, scalable: true });
  } catch {
    host.innerHTML = H`<div class="quick-empty">${t("二维码生成失败，请直接复制链接")}</div>`;
  }
}

/* ------------------------------------------------------------------ */
/* 视图                                                               */
/* ------------------------------------------------------------------ */
export function renderQuick(root) {
  const state = {
    tab: "drop",
    drops: [],
    inbox: [],
    outbox: [],
    pickups: [],
    unread: 0,
    target: { id: null, name: t("我的文件") },
    minutes: 30,
    days: 7,
    maxUses: null,
    sendNodes: [],
    pickupNodes: [],
    lastDrop: null,
    lastPickup: null,
  };

  root.innerHTML = H`
    <div class="view-head">
      <div class="view-title">
        <svg class="icon"><use href="#i-bolt"/></svg>
        <span>快传</span>
      </div>
      <span class="view-sub" data-summary></span>
      <div class="grow"></div>
      <button class="icon-btn" type="button" data-act="refresh" title="刷新">
        <svg class="icon"><use href="#i-refresh"/></svg>
      </button>
    </div>
    <div class="view-body">
      <div class="quick-tabs" data-tabs></div>
      <div data-panel></div>
    </div>`;

  const tabsEl = root.querySelector("[data-tabs]");
  const panelEl = root.querySelector("[data-panel]");
  const summaryEl = root.querySelector("[data-summary]");

  function paintTabs() {
    tabsEl.innerHTML = TABS.map(
      (tab) => H`
      <button class="quick-tab${state.tab === tab.key ? " is-active" : ""}" type="button" data-tab="${tab.key}">
        <svg class="icon"><use href="#${tab.icon}"/></svg>
        <span>${t(tab.label)}${tab.key === "send" && state.unread ? ` (${state.unread})` : ""}</span>
      </button>`
    ).join("");
    tabsEl.querySelectorAll("[data-tab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.tab = btn.dataset.tab;
        paintTabs();
        paintPanel();
      });
    });
  }

  function paintSummary() {
    if (state.tab === "drop") {
      const alive = state.drops.filter((item) => item.status === "active").length;
      summaryEl.textContent = state.drops.length ? t("{n} 个会话 · {alive} 个可用", { n: state.drops.length, alive }) : "";
    } else if (state.tab === "send") {
      summaryEl.textContent = state.unread
        ? t("{n} 条未读", { n: state.unread })
        : t("收到 {in} 条 · 送出 {out} 条", { in: state.inbox.length, out: state.outbox.length });
    } else {
      const alive = state.pickups.filter((item) => item.status === "active").length;
      summaryEl.textContent = state.pickups.length ? t("{n} 个取件码 · {alive} 个可用", { n: state.pickups.length, alive }) : "";
    }
  }

  function paintPanel() {
    paintSummary();
    if (state.tab === "drop") paintDrop();
    else if (state.tab === "send") paintSend();
    else paintPickup();
  }

  /* ---------------- 扫码直传 ---------------- */
  function paintDrop() {
    panelEl.innerHTML = H`
      <div class="quick-card">
        <div class="quick-card__title">
          <svg class="icon icon--s"><use href="#i-upload"/></svg>
          <span data-i18n="手机扫码，直接传进这个目录">${t("手机扫码，直接传进这个目录")}</span>
        </div>
        <div class="quick-card__desc" data-i18n="电脑这边生成一次链接，手机扫码打开就能选照片和文件；文件落在你选的目录里，不用在手机上登录账号。">
          ${t("电脑这边生成一次链接，手机扫码打开就能选照片和文件；文件落在你选的目录里，不用在手机上登录账号。")}
        </div>
        <div class="quick-fields">
          <div class="quick-field" style="flex:1 1 220px">
            <span data-i18n="上传到">${t("上传到")}</span>
            <button class="btn btn--subtle" type="button" data-act="target" style="justify-content:flex-start;height:34px">
              <svg class="icon icon--s"><use href="#i-folder"/></svg>
              <span data-target-name>${escapeHtml(state.target.name)}</span>
            </button>
          </div>
          <label class="quick-field">
            <span data-i18n="有效期">${t("有效期")}</span>
            <select data-minutes>
              ${DROP_MINUTES.map(
                (m) => H`<option value="${m}" ${m === state.minutes ? "selected" : ""}>${t("{n} 分钟", { n: m })}</option>`
              ).join("")}
            </select>
          </label>
          <button class="btn btn--accent" type="button" data-act="new-drop" style="height:34px">
            <svg class="icon icon--s"><use href="#i-bolt"/></svg>
            <span data-i18n="生成二维码">${t("生成二维码")}</span>
          </button>
        </div>
        <div data-last></div>
      </div>
      <div class="quick-list" data-list></div>`;

    panelEl.querySelector('[data-act="target"]').addEventListener("click", async () => {
      const picked = await pickFolder({ title: t("选择上传目录"), confirmText: t("传到这里") });
      if (picked === undefined) return;
      state.target = picked === null ? { id: null, name: t("我的文件") } : { id: picked, name: t("已选目录") };
      // pickFolder 只回 id，名字自己再取一次
      if (picked !== null) {
        try {
          const node = await api.get(`/api/nodes/${picked}`);
          state.target.name = node.node?.name || node.name || t("已选目录");
        } catch {
          state.target.name = t("已选目录");
        }
      }
      paintDrop();
    });

    panelEl.querySelector("[data-minutes]").addEventListener("change", (event) => {
      state.minutes = Number(event.target.value) || 30;
    });

    panelEl.querySelector('[data-act="new-drop"]').addEventListener("click", async () => {
      try {
        const data = await api.post("/api/quick/drops", {
          parentId: state.target.id,
          minutes: state.minutes,
        });
        state.lastDrop = data.drop;
        state.drops = [data.drop, ...state.drops];
        paintDrop();
        Toast.success(t("链接已生成，手机扫码即可上传"));
      } catch (err) {
        Toast.error(err.message);
      }
    });

    if (state.lastDrop) {
      const url = `${siteOrigin()}${state.lastDrop.url}`;
      const box = panelEl.querySelector("[data-last]");
      box.innerHTML = H`
        <div class="quick-share">
          <div class="quick-qr" data-qr></div>
          <div class="quick-share__side">
            <div class="quick-card__title">${t("用手机扫这个码")}</div>
            <div class="quick-card__desc">${t("打开后可以直接选照片 / 文件，扫一次可以连传多个。")}</div>
            <div class="quick-link">
              <input readonly value="${escapeHtml(url)}" data-url>
              <button class="btn btn--subtle" type="button" data-act="copy-url">${t("复制")}</button>
            </div>
            <div class="quick-hint">${t("链接 {when} 失效。", {
              when: state.lastDrop.expiresAt
                ? new Date(state.lastDrop.expiresAt).toLocaleString(document.documentElement.lang || "zh-CN")
                : t("长期"),
            })}</div>
          </div>
        </div>`;
      paintQr(box.querySelector("[data-qr]"), url);
      box.querySelector('[data-act="copy-url"]').addEventListener("click", async () => {
        const ok = await copyText(url);
        ok ? Toast.success(t("链接已复制")) : Toast.error(t("复制失败，请手动选中"));
      });
    }

    const listEl = panelEl.querySelector("[data-list]");
    if (!state.drops.length) {
      listEl.innerHTML = H`<div class="quick-empty">
        <svg class="icon"><use href="#i-bolt"/></svg>
        <div>${t("还没有生成过快传链接")}</div>
      </div>`;
      return;
    }
    listEl.innerHTML = state.drops
      .map(
        (item) => H`
      <div class="quick-row${item.status === "active" ? "" : " is-dead"}" data-id="${item.id}">
        <span class="quick-row__icon"><svg class="icon"><use href="#i-folder"/></svg></span>
        <div class="quick-row__main">
          <div class="quick-row__name">${escapeHtml(item.name)}</div>
          <div class="quick-row__meta">${t("已收到 {n} 个文件", { n: item.uses || 0 })} · ${formatTime(item.createdAt)}${
            item.status === "active" && item.expiresAt
              ? " · " + t("{when} 失效", { when: new Date(item.expiresAt).toLocaleString(document.documentElement.lang || "zh-CN") })
              : ""
          }</div>
        </div>
        <span class="quick-row__tag ${item.status === "active" ? "is-active" : "is-dead"}">${t(STATUS_TEXT[item.status] || item.status)}</span>
        <div class="quick-row__actions">
          <button class="icon-btn" type="button" data-act="open" title="${t("打开链接")}" ${item.status === "active" ? "" : "disabled"}>
            <svg class="icon"><use href="#i-arrow-up-right"/></svg>
          </button>
          <button class="icon-btn" type="button" data-act="revoke" title="${t("关闭")}" ${item.status === "active" ? "" : "disabled"}>
            <svg class="icon"><use href="#i-close"/></svg>
          </button>
        </div>
      </div>`
      )
      .join("");

    listEl.querySelectorAll(".quick-row").forEach((row) => {
      const item = state.drops.find((d) => d.id === Number(row.dataset.id));
      if (!item) return;
      row.querySelector('[data-act="open"]')?.addEventListener("click", () => {
        window.open(`${siteOrigin()}${item.url}`, "_blank", "noopener");
      });
      row.querySelector('[data-act="revoke"]')?.addEventListener("click", async () => {
        const ok = await Dialog.confirm({
          title: t("关闭快传"),
          message: t("关闭后这条链接立即失效，已经打开的页面也传不了文件了。"),
          confirmText: t("关闭"),
          danger: true,
        });
        if (!ok) return;
        try {
          await api.del(`/api/quick/drops/${item.id}`);
          item.status = "revoked";
          state.lastDrop = null;
          paintDrop();
          Toast.success(t("已关闭"));
        } catch (err) {
          Toast.error(err.message);
        }
      });
    });
  }

  /* ---------------- 发给用户 ---------------- */
  function paintSend() {
    panelEl.innerHTML = H`
      <div class="quick-card">
        <div class="quick-card__title">
          <svg class="icon icon--s"><use href="#i-share"/></svg>
          <span data-i18n="直接发到对方的网盘">${t("直接发到对方的网盘")}</span>
        </div>
        <div class="quick-card__desc" data-i18n="文件会复制一份到对方的根目录（同一份内容只占一份磁盘），对方登录后在「我的文件」里就能看到，不用来你的空间里挑。">
          ${t("文件会复制一份到对方的根目录（同一份内容只占一份磁盘），对方登录后在「我的文件」里就能看到，不用来你的空间里挑。")}
        </div>
        <div class="quick-fields">
          <label class="quick-field" style="flex:1 1 180px">
            <span data-i18n="对方的用户名">${t("对方的用户名")}</span>
            <input class="input" data-to maxlength="32" placeholder="${t("对方的登录名")}">
          </label>
          <label class="quick-field" style="flex:2 1 240px">
            <span data-i18n="附言（可选）">${t("附言（可选）")}</span>
            <input class="input" data-message maxlength="200" placeholder="${t("一句话告诉对方这是什么")}">
          </label>
        </div>
        <div class="quick-actions">
          <button class="btn btn--subtle" type="button" data-act="pick">
            <svg class="icon icon--s"><use href="#i-folder"/></svg>
            <span data-i18n="选择文件">${t("选择文件")}</span>
          </button>
          <button class="btn btn--accent" type="button" data-act="send">
            <svg class="icon icon--s"><use href="#i-arrow-up-right"/></svg>
            <span data-i18n="发送">发送</span>
          </button>
        </div>
        <div class="quick-chips" data-chips></div>
      </div>
      <div class="quick-card">
        <div class="quick-card__title">
          <svg class="icon icon--s"><use href="#i-download"/></svg>
          <span data-i18n="收到的">${t("收到的")}</span>
          ${state.unread ? H`<span class="quick-row__tag is-active">${t("{n} 条未读", { n: state.unread })}</span>` : ""}
          <span style="flex:1"></span>
          <button class="btn btn--subtle" type="button" data-act="read" ${state.unread ? "" : "disabled"}>${t("全部标为已读")}</button>
        </div>
        <div class="quick-list" data-inbox></div>
      </div>
      <div class="quick-card">
        <div class="quick-card__title">
          <svg class="icon icon--s"><use href="#i-arrow-up-right"/></svg>
          <span data-i18n="送出的">${t("送出的")}</span>
        </div>
        <div class="quick-list" data-outbox></div>
      </div>`;

    const chipsEl = panelEl.querySelector("[data-chips]");
    const paintChips = () => {
      chipsEl.innerHTML = state.sendNodes
        .map(
          (node) => H`
        <span class="quick-chip">
          <svg class="icon" style="width:14px;height:14px"><use href="#${node.icon}"/></svg>
          <span class="quick-chip__name" title="${escapeHtml(node.name)}">${escapeHtml(node.name)}</span>
          <button class="quick-chip__remove" type="button" data-remove="${node.id}" aria-label="${t("移除")}">
            <svg class="icon"><use href="#i-close"/></svg>
          </button>
        </span>`
        )
        .join("");
      chipsEl.querySelectorAll("[data-remove]").forEach((btn) => {
        btn.addEventListener("click", () => {
          state.sendNodes = state.sendNodes.filter((node) => node.id !== Number(btn.dataset.remove));
          paintChips();
        });
      });
    };
    paintChips();

    panelEl.querySelector('[data-act="pick"]').addEventListener("click", async () => {
      const picked = await pickNodes({ title: t("选择要发送的文件"), mode: "any" });
      if (!picked) return;
      const seen = new Set(state.sendNodes.map((node) => node.id));
      state.sendNodes = [...state.sendNodes, ...picked.filter((node) => !seen.has(node.id))];
      paintChips();
    });

    panelEl.querySelector('[data-act="send"]').addEventListener("click", async () => {
      if (!state.sendNodes.length) {
        Toast.info(t("先选一下要发的文件"));
        return;
      }
      const to = panelEl.querySelector("[data-to]").value.trim();
      if (!to) {
        Toast.info(t("填一下对方的用户名"));
        return;
      }
      const message = panelEl.querySelector("[data-message]").value.trim();
      const btn = panelEl.querySelector('[data-act="send"]');
      btn.disabled = true;
      try {
        await api.post("/api/quick/sends", {
          nodeIds: state.sendNodes.map((node) => node.id),
          toUsername: to,
          message: message || null,
        });
        state.sendNodes = [];
        Toast.success(t("已发送给 {name}", { name: to }));
        await loadAll();
        paintPanel();
      } catch (err) {
        Toast.error(err.message);
      } finally {
        btn.disabled = false;
      }
    });

    panelEl.querySelector('[data-act="read"]')?.addEventListener("click", async () => {
      try {
        await api.post("/api/quick/inbox/read", {});
        state.unread = 0;
        state.inbox = state.inbox.map((item) => ({ ...item, seen: true }));
        paintTabs();
        paintPanel();
      } catch (err) {
        Toast.error(err.message);
      }
    });

    const inboxEl = panelEl.querySelector("[data-inbox]");
    inboxEl.innerHTML = state.inbox.length
      ? state.inbox
          .map(
            (item) => H`
        <div class="quick-row">
          <span class="quick-row__icon"><svg class="icon"><use href="#i-download"/></svg></span>
          <div class="quick-row__main">
            <div class="quick-row__name">${escapeHtml(item.peer?.displayName || item.peer?.username || "")} · ${escapeHtml(item.name)}</div>
            <div class="quick-row__meta">${formatBytes(item.size)} · ${formatTime(item.createdAt)}${
              item.message ? ` · ${escapeHtml(item.message)}` : ""
            }</div>
          </div>
          ${item.seen ? "" : H`<span class="quick-row__tag is-active">${t("未读")}</span>`}
        </div>`
          )
          .join("")
      : H`<div class="quick-empty"><svg class="icon"><use href="#i-download"/></svg><div>${t("还没收到过快传文件")}</div></div>`;

    const outboxEl = panelEl.querySelector("[data-outbox]");
    outboxEl.innerHTML = state.outbox.length
      ? state.outbox
          .map(
            (item) => H`
        <div class="quick-row">
          <span class="quick-row__icon"><svg class="icon"><use href="#i-arrow-up-right"/></svg></span>
          <div class="quick-row__main">
            <div class="quick-row__name">${escapeHtml(item.name)} → ${escapeHtml(item.peer?.displayName || item.peer?.username || "")}</div>
            <div class="quick-row__meta">${formatBytes(item.size)} · ${formatTime(item.createdAt)}${
              item.message ? ` · ${escapeHtml(item.message)}` : ""
            }</div>
          </div>
        </div>`
          )
          .join("")
      : H`<div class="quick-empty"><svg class="icon"><use href="#i-share"/></svg><div>${t("还没有发送过")}</div></div>`;
  }

  /* ---------------- 取件码 ---------------- */
  function paintPickup() {
    panelEl.innerHTML = H`
      <div class="quick-card">
        <div class="quick-card__title">
          <svg class="icon icon--s"><use href="#i-key"/></svg>
          <span data-i18n="生成取件码">${t("生成取件码")}</span>
        </div>
        <div class="quick-card__desc" data-i18n="生成 6 位码发给别人，对方打开接收页输码即可下载，不用注册。到期、次数用完或手动撤销后失效。">
          ${t("生成 6 位码发给别人，对方打开接收页输码即可下载，不用注册。到期、次数用完或手动撤销后失效。")}
        </div>
        <div class="quick-fields">
          <label class="quick-field">
            <span data-i18n="有效期">${t("有效期")}</span>
            <select data-days>
              ${PICKUP_DAYS.map(
                (d) => H`<option value="${d}" ${d === state.days ? "selected" : ""}>${t("{n} 天", { n: d })}</option>`
              ).join("")}
            </select>
          </label>
          <label class="quick-field">
            <span data-i18n="下载次数">${t("下载次数")}</span>
            <select data-uses>
              ${PICKUP_USES.map(
                (opt) => H`<option value="${opt.value ?? ""}" ${opt.value === state.maxUses ? "selected" : ""}>${t(opt.label)}</option>`
              ).join("")}
            </select>
          </label>
          <button class="btn btn--subtle" type="button" data-act="pick">
            <svg class="icon icon--s"><use href="#i-folder"/></svg>
            <span data-i18n="选择文件">${t("选择文件")}</span>
          </button>
          <button class="btn btn--accent" type="button" data-act="new">
            <svg class="icon icon--s"><use href="#i-key"/></svg>
            <span data-i18n="生成">${t("生成")}</span>
          </button>
        </div>
        <div class="quick-chips" data-chips></div>
        <div class="quick-hint">${t("取件码只支持单个文件，文件夹请用「我的分享」生成链接。")}</div>
        <div data-last></div>
      </div>
      <div class="quick-list" data-list></div>`;

    const chipsEl = panelEl.querySelector("[data-chips]");
    const paintChips = () => {
      chipsEl.innerHTML = state.pickupNodes
        .map(
          (node) => H`
        <span class="quick-chip">
          <svg class="icon" style="width:14px;height:14px"><use href="#${node.icon}"/></svg>
          <span class="quick-chip__name" title="${escapeHtml(node.name)}">${escapeHtml(node.name)}</span>
          <button class="quick-chip__remove" type="button" data-remove="${node.id}" aria-label="${t("移除")}">
            <svg class="icon"><use href="#i-close"/></svg>
          </button>
        </span>`
        )
        .join("");
      chipsEl.querySelectorAll("[data-remove]").forEach((btn) => {
        btn.addEventListener("click", () => {
          state.pickupNodes = state.pickupNodes.filter((node) => node.id !== Number(btn.dataset.remove));
          paintChips();
        });
      });
    };
    paintChips();

    panelEl.querySelector('[data-act="pick"]').addEventListener("click", async () => {
      const picked = await pickNodes({ title: t("选择要放进取件码的文件"), mode: "file" });
      if (!picked) return;
      const seen = new Set(state.pickupNodes.map((node) => node.id));
      state.pickupNodes = [...state.pickupNodes, ...picked.filter((node) => !seen.has(node.id))];
      paintChips();
    });

    panelEl.querySelector("[data-days]").addEventListener("change", (event) => {
      state.days = Number(event.target.value) || 7;
    });
    panelEl.querySelector("[data-uses]").addEventListener("change", (event) => {
      const raw = event.target.value;
      state.maxUses = raw === "" ? null : Number(raw);
    });

    panelEl.querySelector('[data-act="new"]').addEventListener("click", async () => {
      if (!state.pickupNodes.length) {
        Toast.info(t("先选一下要交付的文件"));
        return;
      }
      try {
        const data = await api.post("/api/quick/pickups", {
          nodeIds: state.pickupNodes.map((node) => node.id),
          days: state.days,
          maxUses: state.maxUses,
        });
        state.lastPickup = data.pickup;
        state.pickups = [data.pickup, ...state.pickups];
        state.pickupNodes = [];
        paintPickup();
        Toast.success(t("取件码已生成"));
      } catch (err) {
        Toast.error(err.message);
      }
    });

    if (state.lastPickup) {
      const url = `${siteOrigin()}${state.lastPickup.url}`;
      const box = panelEl.querySelector("[data-last]");
      box.innerHTML = H`
        <div class="quick-share">
          <div class="quick-qr" data-qr></div>
          <div class="quick-share__side">
            <div class="quick-card__title">${t("把取件码告诉对方")}</div>
            <div class="quick-code" data-code>${escapeHtml(state.lastPickup.code || "")}</div>
            <div class="quick-link">
              <input readonly value="${escapeHtml(url)}" data-url>
              <button class="btn btn--subtle" type="button" data-act="copy-url">${t("复制链接")}</button>
            </div>
            <div class="quick-hint">${t("对方也可以直接打开 {url} 输入取件码。", { url: "/pickup" })}</div>
          </div>
        </div>`;
      paintQr(box.querySelector("[data-qr]"), url);
      box.querySelector("[data-act='copy-url']").addEventListener("click", async () => {
        const ok = await copyText(url);
        ok ? Toast.success(t("取件链接已复制")) : Toast.error(t("复制失败，请手动选中"));
      });
    }

    const listEl = panelEl.querySelector("[data-list]");
    if (!state.pickups.length) {
      listEl.innerHTML = H`<div class="quick-empty">
        <svg class="icon"><use href="#i-key"/></svg>
        <div>${t("还没有生成过取件码")}</div>
      </div>`;
      return;
    }
    listEl.innerHTML = state.pickups
      .map(
        (item) => H`
      <div class="quick-row${item.status === "active" ? "" : " is-dead"}" data-id="${item.id}">
        <span class="quick-row__icon"><svg class="icon"><use href="#i-key"/></svg></span>
        <div class="quick-row__main">
          <div class="quick-row__name">${escapeHtml(item.name)}${item.code ? H`<span class="quick-code" style="font-size:13px;padding:2px 8px;margin-left:8px;letter-spacing:.12em">${escapeHtml(item.code)}</span>` : ""}</div>
          <div class="quick-row__meta">${t("{n} 次下载", { n: item.uses || 0 })}${
            item.maxUses ? ` · ${t("上限 {n} 次", { n: item.maxUses })}` : ""
          } · ${formatTime(item.createdAt)}</div>
        </div>
        <span class="quick-row__tag ${item.status === "active" ? "is-active" : "is-dead"}">${t(STATUS_TEXT[item.status] || item.status)}</span>
        <div class="quick-row__actions">
          <button class="icon-btn" type="button" data-act="copy" title="${t("复制取件链接")}" ${item.status === "active" ? "" : "disabled"}>
            <svg class="icon"><use href="#i-link"/></svg>
          </button>
          <button class="icon-btn" type="button" data-act="revoke" title="${t("撤销")}" ${item.status === "active" ? "" : "disabled"}>
            <svg class="icon"><use href="#i-close"/></svg>
          </button>
        </div>
      </div>`
      )
      .join("");

    listEl.querySelectorAll(".quick-row").forEach((row) => {
      const item = state.pickups.find((p) => p.id === Number(row.dataset.id));
      if (!item) return;
      row.querySelector('[data-act="copy"]')?.addEventListener("click", async () => {
        const ok = await copyText(`${siteOrigin()}${item.url}`);
        ok ? Toast.success(t("取件链接已复制")) : Toast.error(t("复制失败，请手动选中"));
      });
      row.querySelector('[data-act="revoke"]')?.addEventListener("click", async () => {
        const ok = await Dialog.confirm({
          title: t("撤销取件码"),
          message: t("撤销后这个码立刻失效，已经发出去的码也取不了了。"),
          confirmText: t("撤销"),
          danger: true,
        });
        if (!ok) return;
        try {
          await api.del(`/api/quick/pickups/${item.id}`);
          item.status = "revoked";
          state.lastPickup = null;
          paintPickup();
          Toast.success(t("已撤销"));
        } catch (err) {
          Toast.error(err.message);
        }
      });
    });
  }

  /* ---------------- 数据 ---------------- */
  async function loadAll() {
    try {
      const [drops, inbox, outbox, pickups] = await Promise.all([
        api.get("/api/quick/drops"),
        api.get("/api/quick/inbox"),
        api.get("/api/quick/outbox"),
        api.get("/api/quick/pickups"),
      ]);
      state.drops = drops.items || [];
      state.inbox = inbox.items || [];
      state.unread = inbox.unread || 0;
      state.outbox = outbox.items || [];
      state.pickups = pickups.items || [];
    } catch (err) {
      Toast.error(err.message);
    }
  }

  root.querySelector('[data-act="refresh"]').addEventListener("click", async () => {
    await loadAll();
    paintTabs();
    paintPanel();
  });

  paintTabs();
  paintPanel();
  loadAll().then(() => {
    paintTabs();
    paintPanel();
  });
}
