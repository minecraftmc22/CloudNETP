/**
 * 快传：扫码直传 / 站内发文件 / 免登录取件码。
 *
 * 三个标签页共用一套记录接口：
 *   drop   一次性链接 + 二维码，手机扫码往指定目录传
 *   send   投递到对方的暂存区，对方同意后才搬进他的网盘（复用 blob，不复制物理文件）
 *   pickup 6 位取件码，谁拿到都能下载，可设有效期与下载次数
 */
import { api, escapeHtml, formatBytes, formatTime } from "../api.js";
import { H, t } from "../i18n.js";
import { Dialog } from "../ui/dialog.js";
import { Toast } from "../ui/toast.js";
import { DROP_MAX_FILES, collectDropped } from "../ui/drop-tree.js";
import { pickFolder } from "../ui/picker.js";
import { pickNodes } from "../ui/file-browser.js";
import { openSendBrowser } from "../ui/send-browser.js";
import { copyText } from "../ui/share.js";
import { siteOrigin } from "../store.js";
import { uploadFile } from "../uploader.js";

const DROP_MINUTES = [10, 30, 60, 120];
const PICKUP_USE_CHOICES = [null, 1, 5, 10];
/** 与后端 quick_service.MAX_ITEMS 一致：一条取件码最多列这么多文件 */
const MAX_ITEMS = 100;

const TABS = [
  { key: "drop", label: "扫码直传", icon: "i-upload", desc: "手机扫码登录后，把相册里的东西直接传进指定目录" },
  { key: "send", label: "发给用户", icon: "i-share", desc: "先发到对方的暂存区，对方同意后才存进他的网盘" },
  { key: "pickup", label: "取件码", icon: "i-key", desc: "生成 6 位码，谁拿到都能取，到期自动失效" },
];

const STATUS_TEXT = { active: "有效", expired: "已过期", revoked: "已取消" };

/** 使用规范：与后端 quick_service.BLOCKED_UPLOAD_MSG 保持一致 */
const RULES = [
  "禁止上传、发送、分享任何法律、法规禁止的文件与内容，包括但不限于色情低俗、暴力恐怖、赌博毒品、侵权盗版、恶意程序等。",
  "每次上传都会记录文件哈希、账号、时间与 IP，仅用于违规追查与配合监管。",
  "违反上述要求将被封禁账号；情节严重的保存证据并移交司法机关，依法追究法律责任。",
];

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
    // 记录列表分「进行中 / 历史过期」两个视角，默认只看还能用的
    dropScope: "active",
    pickupScope: "active",
    dropCounts: { active: 0, history: 0 },
    pickupCounts: { active: 0, history: 0 },
    unread: 0,
    // 「发给用户」：待接收条数（标签角标）、被拒绝后还没提示过的条数、收件目录
    pending: 0,
    unseenResults: 0,
    receive: null,
    target: { id: null, name: t("我的文件") },
    minutes: 30,
    days: 7,
    maxUses: null,
    sendNodes: [],
    // 取件码：既可以是「我的文件」里选的，也可以是刚上传的快传文件
    pickupItems: [],
    uploading: [],
    limits: { maxDays: 30, maxUses: 0, retentionDays: 7 },
    lookup: null,
    lookupCode: "",
    lookupError: "",
    lastDrop: null,
    lastPickup: null,
    // null = 还没问到；false = 必须先同意规范
    termsAccepted: null,
    // 标签栏的入场动画只放一次
    tabsIn: false,
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
      <div data-gate hidden></div>
      <div data-body>
        <div class="quick-warn">
          <svg class="icon icon--s"><use href="#i-alert"/></svg>
          <div class="quick-warn__text">
            <b>${t("禁止上传法律禁止的文件")}</b>
            <span>${t("快传禁止上传、发送任何法律、法规禁止的内容。违者封号，并依法追加法律责任。")}</span>
          </div>
        </div>
        <div class="quick-tabs" data-tabs></div>
        <div data-panel></div>
      </div>
    </div>`;

  const tabsEl = root.querySelector("[data-tabs]");
  const panelEl = root.querySelector("[data-panel]");
  const summaryEl = root.querySelector("[data-summary]");
  const gateEl = root.querySelector("[data-gate]");
  const bodyEl = root.querySelector("[data-body]");

  /* ---------------- 入场动画 ----------------
   * 面板每次重绘都会换掉整棵子树：只在「切标签 / 首次进来 / 刷新 / 新建记录」这类
   * 整块重建的时机补动画，选目录、删一条、上传进度这些小重绘不重播，免得点一下闪一下。 */
  function playIn(nodes, { stagger = 0, delay = 0, typed = "quick-rise" } = {}) {
    [...(nodes || [])].forEach((el, index) => {
      if (!el) return;
      el.classList.add(typed);
      const wait = delay + stagger * Math.min(index, 9);
      if (wait) el.style.setProperty("--enter-i", `${wait}ms`);
      el.addEventListener(
        "animationend",
        () => {
          el.classList.remove(typed);
          el.style.removeProperty("--enter-i");
        },
        { once: true }
      );
    });
  }

  /* ---------------- 使用规范闸门 ---------------- */
  function paintGate() {
    gateEl.innerHTML = H`
      <div class="quick-gate">
        <div class="quick-gate__head">
          <svg class="icon"><use href="#i-alert"/></svg>
          <span>${t("使用快传前请先阅读并同意")}</span>
        </div>
        <p class="quick-gate__lead">${t("快传会把文件直接送进网盘，也可能发给别人，所以请先确认下面几条：")}</p>
        <ul class="quick-gate__list">
          ${RULES.map((line) => H`<li>${t(line)}</li>`).join("")}
        </ul>
        <label class="quick-agree">
          <input type="checkbox" data-agree>
          <span>${t("我已阅读并同意《快传使用规范》，承诺不上传法律禁止的内容")}</span>
        </label>
        <button class="btn btn--accent" type="button" data-accept disabled>
          <svg class="icon icon--s"><use href="#i-check"/></svg>
          <span>${t("同意并继续")}</span>
        </button>
      </div>`;

    const box = gateEl.querySelector("[data-agree]");
    const btn = gateEl.querySelector("[data-accept]");
    box.addEventListener("change", () => {
      btn.disabled = !box.checked;
    });
    btn.addEventListener("click", async () => {
      if (!box.checked) return;
      btn.disabled = true;
      try {
        await api.post("/api/quick/terms", { accepted: true });
        state.termsAccepted = true;
        paintBody();
      } catch (err) {
        Toast.error(err.message);
        btn.disabled = false;
      }
    });
  }

  function paintBody() {
    const blocked = state.termsAccepted === false;
    gateEl.hidden = !blocked;
    bodyEl.hidden = blocked;
    if (blocked) {
      paintGate();
      return;
    }
    paintTabs();
    paintPanel(true);
  }

  function paintTabs() {
    tabsEl.innerHTML = TABS.map(
      (tab) => H`
      <button class="quick-tab${state.tab === tab.key ? " is-active" : ""}" type="button" data-tab="${tab.key}">
        <svg class="icon"><use href="#${tab.icon}"/></svg>
        <span>${t(tab.label)}${tab.key === "send" && (state.pending || state.unread) ? ` (${state.pending || state.unread})` : ""}</span>
      </button>`
    ).join("");
    // 标签栏只在第一次露面时滑进来，切标签不重播（否则点一下抖一下）
    if (!state.tabsIn) {
      state.tabsIn = true;
      playIn(tabsEl.querySelectorAll(".quick-tab"), { stagger: 40, typed: "quick-slide" });
    }
    tabsEl.querySelectorAll("[data-tab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.tab = btn.dataset.tab;
        paintTabs();
        paintPanel(true);
      });
    });
  }

  function paintSummary() {
    if (state.tab === "drop") {
      const alive = state.drops.filter((item) => item.status === "active").length;
      summaryEl.textContent = state.drops.length ? t("{n} 个会话 · {alive} 个可用", { n: state.drops.length, alive }) : "";
    } else if (state.tab === "send") {
      summaryEl.textContent = state.pending
        ? t("{n} 条待接收 · 送出 {out} 条", { n: state.pending, out: state.outbox.length })
        : t("收到 {in} 条 · 送出 {out} 条", { in: state.inbox.length, out: state.outbox.length });
    } else {
      const alive = state.pickups.filter((item) => item.status === "active").length;
      summaryEl.textContent = state.pickups.length ? t("{n} 个取件码 · {alive} 个可用", { n: state.pickups.length, alive }) : "";
    }
  }

  function paintPanel(fresh = false) {
    paintSummary();
    if (state.tab === "drop") paintDrop();
    else if (state.tab === "send") paintSend();
    else paintPickup();
    if (!fresh) return;
    // 卡片依次浮起，列表条目再跟着逐条冒出来
    playIn(panelEl.querySelectorAll(":scope > .quick-card"), { stagger: 70 });
    playIn(panelEl.querySelectorAll(".quick-list > *"), { stagger: 34, delay: 70 });
  }

  /* ---------------- 记录列表的工具条 ----------------
   * 记录分两个视角：进行中 / 历史过期（过期、已取消）。失效的那些不再混在下面，
   * 收到历史里，能逐条删、也能一键清空。 */
  function listbarHtml(scope, counts) {
    const onHistory = scope === "history";
    const num = onHistory ? counts.history : counts.active;
    // 按钮上带的是「另一边」有多少条，这样不切过去也知道历史里堆了几条
    const other = onHistory ? counts.active : counts.history;
    return H`
      <div class="quick-listbar">
        <span class="quick-listbar__label">
          <svg class="icon icon--s"><use href="#${onHistory ? "i-clock" : "i-play"}"/></svg>
          ${onHistory ? t("历史过期") : t("进行中")}
        </span>
        <span class="quick-listbar__count">${num}</span>
        <span class="grow"></span>
        ${
          onHistory && counts.history
            ? H`<button class="btn btn--subtle btn--danger" type="button" data-act="clear-history">
                <svg class="icon icon--s"><use href="#i-trash"/></svg>${t("清空历史")}
              </button>`
            : ""
        }
        <button class="btn btn--subtle" type="button" data-act="toggle-history">
          <svg class="icon icon--s" ${onHistory ? 'style="transform:rotate(90deg)"' : ""}>
            <use href="#${onHistory ? "i-chevron" : "i-clock"}"/>
          </svg>
          ${onHistory ? t("返回进行中") : t("历史过期")}
          ${other ? H`<span class="quick-listbar__count">${other}</span>` : ""}
        </button>
      </div>`;
  }

  /** 切换记录视角 + 清空历史：两个标签页共用一套 */
  function bindListbar(listEl, kind, scopeKey, repaint) {
    listEl.querySelector('[data-act="toggle-history"]')?.addEventListener("click", () => {
      state[scopeKey] = state[scopeKey] === "history" ? "active" : "history";
      repaint();
    });
    listEl.querySelector('[data-act="clear-history"]')?.addEventListener("click", async () => {
      const counts = kind === "drop" ? state.dropCounts : state.pickupCounts;
      const ok = await Dialog.confirm({
        title: t("清空历史记录"),
        message:
          kind === "drop"
            ? t("把 {n} 条已失效的快传链接记录删掉，已经传过来的文件不受影响。", { n: counts.history })
            : t("把 {n} 个已失效的取件码记录删掉，快传暂存区里属于它们的文件也会一起回收。", { n: counts.history }),
        confirmText: t("清空"),
        danger: true,
      });
      if (!ok) return;
      try {
        const data = await api.del(`/api/quick/history?kind=${kind}`);
        Toast.success(t("已删除 {n} 条记录", { n: data.deleted || 0 }));
        await loadAll();
        repaint();
      } catch (err) {
        Toast.error(err.message);
      }
    });
  }

  /* ---------------- 扫码直传 ---------------- */
  function paintDrop() {
    panelEl.innerHTML = H`
      <div class="quick-card">
        <div class="quick-card__title">
          <svg class="icon icon--s"><use href="#i-upload"/></svg>
          <span data-i18n="手机扫码，直接传进这个目录">${t("手机扫码，直接传进这个目录")}</span>
        </div>
        <div class="quick-card__desc" data-i18n="电脑这边生成一次链接，手机扫码登录后就能选照片和文件，直接落进你选的目录，不用在手机上找目录。">
          ${t("电脑这边生成一次链接，手机扫码登录后就能选照片和文件，直接落进你选的目录，不用在手机上找目录。")}
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
        state.dropCounts = { ...state.dropCounts, active: state.dropCounts.active + 1 };
        paintDrop();
        // 新生成的二维码与刚冒出来的那条记录单独弹一下，比整块重绘更有点「生成成功」的手感
        playIn(panelEl.querySelectorAll(".quick-share"), { typed: "quick-pop" });
        // 列表顶上还挂着工具条，第一条记录未必是 :first-child
        playIn([panelEl.querySelector(".quick-list > .quick-row")], { typed: "quick-pop" });
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
    const onHistory = state.dropScope === "history";
    const rows = state.drops.filter((item) =>
      onHistory ? item.status !== "active" : item.status === "active"
    );

    const emptyHtml = onHistory
      ? H`<div class="quick-empty">
          <svg class="icon"><use href="#i-clock"/></svg>
          <div>${t("还没有过期或已关闭的快传")}</div>
        </div>`
      : H`<div class="quick-empty">
          <svg class="icon"><use href="#i-bolt"/></svg>
          <div>${t("还没有生成过快传链接")}</div>
        </div>`;

    listEl.innerHTML =
      listbarHtml(state.dropScope, state.dropCounts) +
      (rows.length
        ? rows
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
          ${
            onHistory
              ? H`<button class="icon-btn" type="button" data-act="forget" title="${t("删除记录")}">
                  <svg class="icon"><use href="#i-trash"/></svg>
                </button>`
              : H`<button class="icon-btn" type="button" data-act="open" title="${t("打开链接")}">
                  <svg class="icon"><use href="#i-arrow-up-right"/></svg>
                </button>
                <button class="icon-btn" type="button" data-act="revoke" title="${t("关闭")}">
                  <svg class="icon"><use href="#i-close"/></svg>
                </button>`
          }
        </div>
      </div>`
            )
            .join("")
        : emptyHtml);

    bindListbar(listEl, "drop", "dropScope", paintDrop);

    listEl.querySelectorAll(".quick-row").forEach((row) => {
      const item = state.drops.find((d) => d.id === Number(row.dataset.id));
      if (!item) return;
      row.querySelector('[data-act="open"]')?.addEventListener("click", () => {
        window.open(`${siteOrigin()}${item.url}`, "_blank", "noopener");
      });
      row.querySelector('[data-act="revoke"]')?.addEventListener("click", async () => {
        const ok = await Dialog.confirm({
          title: t("关闭快传"),
          message: t("关闭后这条链接立即失效，已经打开的页面也传不了文件了。记录会收进「历史过期」。"),
          confirmText: t("关闭"),
          danger: true,
        });
        if (!ok) return;
        try {
          await api.del(`/api/quick/drops/${item.id}`);
          item.status = "revoked";
          state.lastDrop = null;
          state.dropCounts = {
            active: Math.max(0, state.dropCounts.active - 1),
            history: state.dropCounts.history + 1,
          };
          paintDrop();
          Toast.success(t("已关闭，记录收进历史过期"));
        } catch (err) {
          Toast.error(err.message);
        }
      });
      row.querySelector('[data-act="forget"]')?.addEventListener("click", async () => {
        const ok = await Dialog.confirm({
          title: t("删除这条记录"),
          message: t("只删掉这条历史记录，已经传过来的文件不受影响。"),
          confirmText: t("删除记录"),
          danger: true,
        });
        if (!ok) return;
        try {
          await api.del(`/api/quick/drops/${item.id}`);
          state.drops = state.drops.filter((d) => d.id !== item.id);
          state.dropCounts = {
            ...state.dropCounts,
            history: Math.max(0, state.dropCounts.history - 1),
          };
          paintDrop();
          Toast.success(t("记录已删除"));
        } catch (err) {
          Toast.error(err.message);
        }
      });
    });
  }

  /* ---------------- 发给用户 ---------------- */
  /** 接收状态 → 标签文案与配色（与后端 quick_service.send_status 对齐） */
  const SEND_TAGS = {
    pending: { text: "待接收", cls: "is-wait" },
    accepted: { text: "已存入", cls: "is-active" },
    rejected: { text: "被拒绝", cls: "is-dead" },
    expired: { text: "已过期", cls: "" },
  };

  function sendTag(item) {
    const conf = SEND_TAGS[item.sendStatus] || SEND_TAGS.pending;
    return H`<span class="quick-row__tag ${conf.cls}">${t(conf.text)}</span>`;
  }

  /** 收到后存到哪：设置里的收件目录（默认 我的文件/快传的文件） */
  function receivePathLabel() {
    const parts = (state.receive?.path?.path || []).map((part) => part.name);
    return parts.length ? parts.join(" / ") : t("我的文件 / 快传的文件");
  }

  async function decideSend(item, action) {
    if (action === "reject") {
      const ok = await Dialog.confirm({
        title: t("拒绝这份快传"),
        message: t("拒绝后文件会从暂存区删掉，对方会看到「文件被拒绝」。想留个余地的话，也可以先点眼睛看看里面是什么。"),
        confirmText: t("拒绝"),
        danger: true,
      });
      if (!ok) return;
    }
    try {
      await api.post(`/api/quick/sends/${item.id}/${action}`, {});
      if (action === "accept") {
        Toast.success(t("已存入 {path}", { path: receivePathLabel() }));
      } else {
        Toast.success(t("已拒绝，对方会看到「文件被拒绝」"));
      }
      await loadAll();
      paintTabs();
      paintPanel();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  function paintSend() {
    const pending = state.pending || 0;
    panelEl.innerHTML = H`
      <div class="quick-card">
        <div class="quick-card__title">
          <svg class="icon icon--s"><use href="#i-share"/></svg>
          <span data-i18n="发给用户">${t("发给用户")}</span>
        </div>
        <div class="quick-card__desc" data-i18n="文件先放进对方的暂存区，对方看完内容点「同意」之后才搬进他的网盘（默认 我的文件/快传的文件）；对方也可以拒绝，拒绝后会提示你。">
          ${t("文件先放进对方的暂存区，对方看完内容点「同意」之后才搬进他的网盘（默认 我的文件/快传的文件）；对方也可以拒绝，拒绝后会提示你。")}
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
          ${pending ? H`<span class="quick-row__tag is-wait">${t("{n} 条待接收", { n: pending })}</span>` : ""}
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
        ${
          state.unseenResults
            ? H`
        <div class="quick-notice" data-notice>
          <svg class="icon icon--s"><use href="#i-alert"/></svg>
          <span>${t("有 {n} 条快传对方没有收下（文件被拒绝或已过期），可以换个方式再发一次。", { n: state.unseenResults })}</span>
          <span style="flex:1"></span>
          <button class="btn btn--subtle" type="button" data-act="ack">${t("知道了")}</button>
        </div>`
            : ""
        }
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
      playIn(chipsEl.querySelectorAll(".quick-chip"), { stagger: 30, typed: "quick-pop" });
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
        const data = await api.post("/api/quick/sends", {
          nodeIds: state.sendNodes.map((node) => node.id),
          toUsername: to,
          message: message || null,
        });
        state.sendNodes = [];
        const accepted = data?.send?.sendStatus === "accepted";
        Toast.success(
          accepted
            ? t("已发送给 {name}，对方开着自动接收，已经存进他的网盘", { name: to })
            : t("已发送给 {name}，等他同意后才会存进他的网盘", { name: to })
        );
        await loadAll();
        paintTabs();
        paintPanel(true);
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

    panelEl.querySelector('[data-act="ack"]')?.addEventListener("click", async () => {
      try {
        await api.post("/api/quick/outbox/ack", {});
        state.unseenResults = 0;
        paintSend();
      } catch (err) {
        Toast.error(err.message);
      }
    });

    const inboxEl = panelEl.querySelector("[data-inbox]");
    inboxEl.innerHTML = state.inbox.length
      ? state.inbox
          .map((item) => {
            const waiting = item.sendStatus === "pending";
            const dim = !waiting && item.sendStatus !== "accepted";
            return H`
        <div class="quick-row ${waiting ? "is-pending" : ""} ${dim ? "is-dead" : ""}" data-id="${item.id}" data-send>
          <span class="quick-row__icon"><svg class="icon"><use href="#${item.count > 1 ? "i-folder" : "i-file"}"/></svg></span>
          <div class="quick-row__main">
            <div class="quick-row__name">${escapeHtml(item.peer?.displayName || item.peer?.username || "")} · ${escapeHtml(item.name)}</div>
            <div class="quick-row__meta">${formatBytes(item.size)} · ${formatTime(item.createdAt)}${
              item.message ? ` · ${escapeHtml(item.message)}` : ""
            }${waiting ? ` · ${t("双击查看内容")}` : ""}</div>
          </div>
          ${sendTag(item)}
          ${
            waiting
              ? H`<div class="quick-row__actions">
                  <button class="icon-btn" type="button" data-act="browse" title="${t("查看内容")}">
                    <svg class="icon"><use href="#i-eye"/></svg>
                  </button>
                  <button class="icon-btn" type="button" data-act="accept" title="${t("同意并存入")}">
                    <svg class="icon"><use href="#i-check"/></svg>
                  </button>
                  <button class="icon-btn" type="button" data-act="reject" title="${t("拒绝")}">
                    <svg class="icon"><use href="#i-close"/></svg>
                  </button>
                </div>`
              : ""
          }
        </div>`;
          })
          .join("")
      : H`<div class="quick-empty"><svg class="icon"><use href="#i-download"/></svg><div>${t("还没收到过快传文件")}</div></div>`;

    inboxEl.querySelectorAll("[data-send]").forEach((row) => {
      const item = state.inbox.find((entry) => entry.id === Number(row.dataset.id));
      if (!item || item.sendStatus !== "pending") return;
      row.addEventListener("dblclick", (event) => {
        // 点的是按钮就别顺手把弹层打开
        if (event.target.closest("[data-act]")) return;
        openSendBrowser(item, { onAccept: () => decideSend(item, "accept") });
      });
      row.querySelector('[data-act="browse"]')?.addEventListener("click", () => {
        openSendBrowser(item, { onAccept: () => decideSend(item, "accept") });
      });
      row.querySelector('[data-act="accept"]')?.addEventListener("click", () => decideSend(item, "accept"));
      row.querySelector('[data-act="reject"]')?.addEventListener("click", () => decideSend(item, "reject"));
    });

    const outboxEl = panelEl.querySelector("[data-outbox]");
    outboxEl.innerHTML = state.outbox.length
      ? state.outbox
          .map((item) => {
            const dim = item.sendStatus === "rejected" || item.sendStatus === "expired";
            return H`
        <div class="quick-row ${dim ? "is-dead" : ""}" data-id="${item.id}">
          <span class="quick-row__icon"><svg class="icon"><use href="#i-arrow-up-right"/></svg></span>
          <div class="quick-row__main">
            <div class="quick-row__name">${escapeHtml(item.name)} → ${escapeHtml(item.peer?.displayName || item.peer?.username || "")}</div>
            <div class="quick-row__meta">${formatBytes(item.size)} · ${formatTime(item.createdAt)}${
              item.message ? ` · ${escapeHtml(item.message)}` : ""
            }</div>
          </div>
          ${sendTag(item)}
        </div>`;
          })
          .join("")
      : H`<div class="quick-empty"><svg class="icon"><use href="#i-share"/></svg><div>${t("还没有发送过")}</div></div>`;
  }

  /* ---------------- 取件码 ---------------- */
  function pickupDayOptions() {
    const cap = Math.max(1, Number(state.limits.maxDays) || 30);
    const values = [1, 3, 7, 30].filter((day) => day <= cap);
    if (!values.includes(cap)) values.push(cap);
    return [...new Set(values)].sort((a, b) => a - b);
  }

  function pickupUseOptions() {
    const cap = Number(state.limits.maxUses) || 0;
    const values = PICKUP_USE_CHOICES.filter((value) => value === null || !cap || value <= cap);
    if (cap && !values.includes(cap)) values.push(cap);
    return values.sort((a, b) => (a ?? Infinity) - (b ?? Infinity));
  }

  function useLabel(value) {
    return value === null ? t("不限次数") : t("{n} 次", { n: value });
  }

  /** 一行：文件 + 这个文件自己的有效期 / 下载次数 */
  function pickupItemHtml(item, index) {
    const days = pickupDayOptions();
    const uses = pickupUseOptions();
    return H`
      <div class="quick-item-row" data-item="${item.id}">
        <svg class="icon quick-item-row__icon"><use href="#${item.icon || "i-file"}"/></svg>
        <div class="quick-item-row__main">
          <div class="quick-item-row__name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div>
          <div class="quick-item-row__meta">${formatBytes(item.size)}${item.uploaded ? ` · ${t("快传文件，不占网盘空间")}` : ""}</div>
        </div>
        <label class="quick-mini">
          <select data-item-days="${item.id}">
            ${days.map((day) => H`<option value="${day}" ${Number(item.days) === day ? "selected" : ""}>${t("{n} 天", { n: day })}</option>`).join("")}
          </select>
        </label>
        <label class="quick-mini">
          <select data-item-uses="${item.id}">
            ${uses.map((value) => H`<option value="${value ?? ""}" ${item.uses === value ? "selected" : ""}>${useLabel(value)}</option>`).join("")}
          </select>
        </label>
        <button class="icon-btn" type="button" data-item-remove="${item.id}" title="${t("移除")}">
          <svg class="icon"><use href="#i-close"/></svg>
        </button>
      </div>`;
  }

  function paintPickup() {
    panelEl.innerHTML = H`
      <div class="quick-card">
        <div class="quick-card__title">
          <svg class="icon icon--s"><use href="#i-key"/></svg>
          <span>${t("生成取件码")}</span>
        </div>
        <div class="quick-card__desc">
          ${t("生成 6 位码发给别人，对方打开接收页输码即可下载，不用注册。每个文件可以单独设有效期和下载次数，到期、用完或撤销后失效。")}
        </div>

        <div class="quick-pick-drop" data-drop>
          <svg class="icon"><use href="#i-upload"/></svg>
          <div class="quick-pick-drop__text">
            <b>${t("把文件或文件夹拖到这里")}</b>
          </div>
          <div class="quick-pick-drop__acts">
            <button class="btn btn--subtle" type="button" data-act="upload">
              <svg class="icon icon--s"><use href="#i-plus"/></svg>${t("选择文件")}
            </button>
            <button class="btn btn--subtle" type="button" data-act="upload-dir">
              <svg class="icon icon--s"><use href="#i-folder"/></svg>${t("选择文件夹")}
            </button>
          </div>
          <div class="quick-pick-drop__hint">
            ${t("支持一次选多个或整个文件夹；上传只停在快传暂存区，不占网盘空间，也不会进「我的文件」。")}
          </div>
          <input type="file" multiple hidden data-file-input>
          <input type="file" multiple hidden webkitdirectory data-dir-input>
        </div>

        <div class="quick-fields">
          <label class="quick-field">
            <span>${t("新加文件的有效期")}</span>
            <select data-days>
              ${pickupDayOptions().map((day) => H`<option value="${day}" ${day === state.days ? "selected" : ""}>${t("{n} 天", { n: day })}</option>`).join("")}
            </select>
          </label>
          <label class="quick-field">
            <span>${t("新加文件的下载次数")}</span>
            <select data-uses>
              ${pickupUseOptions().map((value) => H`<option value="${value ?? ""}" ${value === state.maxUses ? "selected" : ""}>${useLabel(value)}</option>`).join("")}
            </select>
          </label>
          <button class="btn btn--subtle" type="button" data-act="pick">
            <svg class="icon icon--s"><use href="#i-folder"/></svg>${t("从我的文件选择")}
          </button>
          <button class="btn btn--accent" type="button" data-act="new">
            <svg class="icon icon--s"><use href="#i-key"/></svg>${t("生成")}
          </button>
        </div>

        <div class="quick-item-list" data-items></div>
        <div class="quick-uploads" data-uploads hidden></div>
        <div class="quick-hint">${t("取件码只列文件：选中或上传文件夹时，会展开成里面的每个文件。")}</div>
        <div data-last></div>
      </div>

      <div class="quick-card">
        <div class="quick-card__title">
          <svg class="icon icon--s"><use href="#i-download"/></svg>
          <span>${t("输入取件码取件")}</span>
        </div>
        <div class="quick-card__desc">
          ${t("已经登录的话，不用等别人发链接：直接输 6 位码就能看清单，然后转存到自己的网盘或下载。")}
        </div>
        <div class="quick-fields">
          <label class="quick-field quick-field--grow">
            <span>${t("取件码")}</span>
            <input class="textbox" data-code-input maxlength="12" autocomplete="off"
                   placeholder="${t("6 位字母或数字")}" value="${escapeHtml(state.lookupCode)}">
          </label>
          <button class="btn btn--accent" type="button" data-act="lookup">${t("查询")}</button>
        </div>
        <div data-lookup></div>
      </div>

      <div class="quick-list" data-list></div>`;

    const itemsEl = panelEl.querySelector("[data-items]");
    const uploadsEl = panelEl.querySelector("[data-uploads]");
    const fileInput = panelEl.querySelector("[data-file-input]");
    const lastBox = panelEl.querySelector("[data-last]");

    const paintItems = () => {
      if (!state.pickupItems.length) {
        itemsEl.innerHTML = "";
        return;
      }
      itemsEl.innerHTML = state.pickupItems.map(pickupItemHtml).join("");
      itemsEl.querySelectorAll("[data-item-days]").forEach((select) => {
        select.addEventListener("change", () => {
          const item = state.pickupItems.find((row) => row.id === Number(select.dataset.itemDays));
          if (item) item.days = Number(select.value);
        });
      });
      itemsEl.querySelectorAll("[data-item-uses]").forEach((select) => {
        select.addEventListener("change", () => {
          const item = state.pickupItems.find((row) => row.id === Number(select.dataset.itemUses));
          if (item) item.uses = select.value === "" ? null : Number(select.value);
        });
      });
      itemsEl.querySelectorAll("[data-item-remove]").forEach((btn) => {
        btn.addEventListener("click", () => {
          state.pickupItems = state.pickupItems.filter(
            (row) => row.id !== Number(btn.dataset.itemRemove)
          );
          paintItems();
        });
      });
    };

    const paintUploading = () => {
      if (!state.uploading.length) {
        uploadsEl.hidden = true;
        uploadsEl.innerHTML = "";
        return;
      }
      uploadsEl.hidden = false;
      uploadsEl.innerHTML = state.uploading
        .map((row) => {
          const pct = Math.round((row.ratio || 0) * 100);
          return H`
          <div class="quick-upload" data-up="${row.key}">
            <span class="quick-upload__name" title="${escapeHtml(row.name)}">${escapeHtml(row.name)}</span>
            <span class="quick-upload__state">${
              row.status === "error"
                ? escapeHtml(row.error || t("上传失败"))
                : row.status === "done"
                ? t("已就绪")
                : H`${pct}%`
            }</span>
            <div class="progress"><div class="progress__bar" style="width:${row.status === "error" ? 0 : pct}%"></div></div>
          </div>`;
        })
        .join("");
    };

    /** 加进待生成清单；返回这次被 100 项上限挡掉的数量 */
    const addItems = (rows) => {
      const seen = new Set(state.pickupItems.map((row) => row.id));
      const added = [];
      let skipped = 0;
      for (const row of rows) {
        if (seen.has(row.id)) continue;
        if (state.pickupItems.length >= MAX_ITEMS) {
          skipped += 1;
          continue;
        }
        seen.add(row.id);
        state.pickupItems.push({
          id: row.id,
          name: row.name,
          size: row.size || 0,
          icon: row.icon || "i-file",
          days: state.days,
          uses: state.maxUses,
          uploaded: Boolean(row.uploaded),
        });
        added.push(row.id);
      }
      paintItems();
      // 只给刚加进来的那几行弹一下，已经在列表里的不动
      if (added.length) {
        const ids = new Set(added.map(String));
        playIn(
          [...itemsEl.querySelectorAll(".quick-item-row")].filter((el) => ids.has(el.dataset.item)),
          { stagger: 32, typed: "quick-pop" }
        );
      }
      return skipped;
    };

    /**
     * 真上传：文件和文件夹都走这里。
     *
     * 一律进快传暂存区（quick=true）：不落进任何目录、不进「我的文件」、也不占配额，
     * 取件码引用它们，过期或撤销后由清理任务回收。文件夹只是展开成里面的文件，
     * 不在网盘里建目录 —— 取件码本来也只列文件。
     */
    async function uploadList(entries) {
      const live = entries.filter((item) => item.file);
      if (!live.length) {
        Toast.warn(t("没有可上传的文件"));
        return;
      }
      let overflow = 0;
      for (const item of live) {
        const file = item.file;
        const entry = {
          key: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
          name: item.relPath || file.name,
          ratio: 0,
          status: "uploading",
          error: "",
        };
        state.uploading.push(entry);
        paintUploading();
        try {
          const result = await uploadFile({
            file,
            quick: true,
            onProgress: (loaded) => {
              entry.ratio = file.size ? loaded / file.size : 0;
              paintUploading();
            },
          });
          entry.status = "done";
          entry.ratio = 1;
          overflow += addItems([
            {
              id: result.node.id,
              name: result.node.name,
              size: result.node.size,
              icon: result.node.icon,
              uploaded: true,
            },
          ]);
        } catch (err) {
          entry.status = "error";
          entry.error = err.message || t("上传失败");
        }
        paintUploading();
      }
      // 成功的条目收走，失败的留着给人看原因
      const failed = state.uploading.filter((row) => row.status === "error");
      state.uploading = failed;
      paintUploading();
      if (overflow) {
        Toast.info(
          t("取件码一次最多 {n} 个文件，多出来的 {m} 个没有加进清单", {
            n: MAX_ITEMS,
            m: overflow,
          })
        );
      }
    }

    paintItems();
    paintUploading();

    const dirInput = panelEl.querySelector("[data-dir-input]");
    panelEl.querySelector('[data-act="upload"]').addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      uploadList([...fileInput.files].map((file) => ({ file, relPath: "" })));
      fileInput.value = "";
    });
    panelEl.querySelector('[data-act="upload-dir"]').addEventListener("click", () => dirInput.click());
    dirInput.addEventListener("change", () => {
      // webkitRelativePath 形如「选中的目录/子目录/文件名」，只留文件名（取件码不建目录）
      uploadList([...dirInput.files].map((file) => ({ file, relPath: "" })));
      dirInput.value = "";
    });

    const drop = panelEl.querySelector("[data-drop]");
    const setDragging = (on) => drop.classList.toggle("is-dragging", on);
    ["dragenter", "dragover"].forEach((type) =>
      drop.addEventListener(type, (event) => {
        event.preventDefault();
        setDragging(true);
      })
    );
    ["dragleave", "drop"].forEach((type) =>
      drop.addEventListener(type, () => setDragging(false))
    );
    drop.addEventListener("drop", async (event) => {
      event.preventDefault();
      const dt = event.dataTransfer;
      if (!dt) return;
      let collected;
      try {
        collected = await collectDropped(dt);
      } catch {
        collected = {
          files: [...(dt.files || [])].map((file) => ({ file, relPath: "" })),
          dirs: [],
          truncated: false,
        };
      }
      if (collected.truncated) {
        Toast.warn(t("一次最多处理 {n} 个文件，超出的部分已忽略", { n: DROP_MAX_FILES }));
      }
      if (collected.files.length) uploadList(collected.files);
    });

    panelEl.querySelector('[data-act="pick"]').addEventListener("click", async () => {
      const picked = await pickNodes({ title: t("选择要放进取件码的文件"), mode: "file" });
      if (!picked) return;
      addItems(picked);
    });

    panelEl.querySelector("[data-days]").addEventListener("change", (event) => {
      state.days = Number(event.target.value) || 7;
    });
    panelEl.querySelector("[data-uses]").addEventListener("change", (event) => {
      const raw = event.target.value;
      state.maxUses = raw === "" ? null : Number(raw);
    });

    panelEl.querySelector('[data-act="new"]').addEventListener("click", async () => {
      if (state.uploading.some((row) => row.status === "uploading")) {
        Toast.info(t("还有文件在上传，等传完再生成"));
        return;
      }
      if (!state.pickupItems.length) {
        Toast.info(t("先选一下要交付的文件"));
        return;
      }
      try {
        const data = await api.post("/api/quick/pickups", {
          items: state.pickupItems.map((item) => ({
            nodeId: item.id,
            days: item.days,
            maxUses: item.uses,
          })),
        });
        state.lastPickup = data.pickup;
        state.pickups = [data.pickup, ...state.pickups];
        state.pickupCounts = { ...state.pickupCounts, active: state.pickupCounts.active + 1 };
        state.pickupItems = [];
        state.uploading = [];
        paintPickup();
        playIn(panelEl.querySelectorAll(".quick-share"), { typed: "quick-pop" });
        playIn([panelEl.querySelector(".quick-list > .quick-row")], { typed: "quick-pop" });
        Toast.success(t("取件码已生成"));
      } catch (err) {
        Toast.error(err.message);
      }
    });

    // ---- 输入取件码 ----
    const codeInput = panelEl.querySelector("[data-code-input]");
    const lookupBox = panelEl.querySelector("[data-lookup]");

    const paintLookup = () => {
      if (!state.lookup && !state.lookupError) {
        lookupBox.innerHTML = "";
        return;
      }
      if (state.lookupError) {
        lookupBox.innerHTML = H`<div class="quick-lookup__error">${escapeHtml(state.lookupError)}</div>`;
        return;
      }
      const data = state.lookup;
      const items = data.pickup.items || [];
      lookupBox.innerHTML = H`
        <div class="quick-lookup">
          <div class="quick-lookup__head">
            <span class="quick-code" style="font-size:14px">${escapeHtml(data.pickup.code)}</span>
            <span class="quick-lookup__owner">${t("来自 {owner}", { owner: data.pickup.owner })}</span>
            <span class="grow"></span>
            <button class="btn btn--subtle" type="button" data-act="save">
              <svg class="icon icon--s"><use href="#i-download"/></svg>${t("全部转存到我的文件")}
            </button>
          </div>
          <div class="quick-item-list">
            ${items
              .map(
                (item) => H`
              <div class="quick-item-row${item.alive ? "" : " is-dead"}">
                <svg class="icon quick-item-row__icon"><use href="#${item.icon || "i-file"}"/></svg>
                <div class="quick-item-row__main">
                  <div class="quick-item-row__name">${escapeHtml(item.name)}</div>
                  <div class="quick-item-row__meta">${formatBytes(item.size)} · ${
                    item.expired
                      ? t("已过期")
                      : item.usedUp
                      ? t("下载次数已用完")
                      : `${t("有效期至 {when}", { when: formatTime(item.expiresAt) })}${
                          item.remaining === null ? "" : ` · ${t("还能取 {n} 次", { n: item.remaining })}`
                        }`
                  }</div>
                </div>
                <a class="btn btn--subtle" href="/api/public/quick/pickup/${encodeURIComponent(data.pickup.code)}/download?nodeId=${item.id}"
                   ${item.alive ? "" : 'aria-disabled="true" tabindex="-1" class="btn btn--subtle is-disabled"'}>
                  <svg class="icon icon--s"><use href="#i-download"/></svg>${t("下载")}
                </a>
              </div>`
              )
              .join("")}
          </div>
        </div>`;
      lookupBox.querySelector('[data-act="save"]')?.addEventListener("click", async () => {
        const target = await pickFolder({ title: t("转存到哪个文件夹"), confirmText: t("转存到这里") });
        if (target === undefined) return;
        try {
          const saved = await api.post(
            `/api/quick/pickups/code/${encodeURIComponent(data.pickup.code)}/save`,
            { parentId: target }
          );
          Toast.success(t("已转存 {n} 个文件", { n: saved.saved }));
        } catch (err) {
          Toast.error(err.message);
        }
      });
    };

    const lookup = async () => {
      const code = (codeInput.value || "").trim();
      state.lookupCode = code;
      if (!code) {
        state.lookup = null;
        state.lookupError = "";
        paintLookup();
        return;
      }
      try {
        const data = await api.get(`/api/quick/pickups/code/${encodeURIComponent(code)}`);
        state.lookup = data;
        state.lookupError = "";
      } catch (err) {
        state.lookup = null;
        state.lookupError = err.message;
      }
      paintLookup();
      playIn(lookupBox.querySelectorAll(".quick-lookup, .quick-lookup__error"), {
        typed: "quick-pop",
      });
    };

    panelEl.querySelector('[data-act="lookup"]').addEventListener("click", lookup);
    codeInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        lookup();
      }
    });
    paintLookup();

    // ---- 刚生成的取件码 ----
    if (state.lastPickup) {
      const url = `${siteOrigin()}${state.lastPickup.url}`;
      lastBox.innerHTML = H`
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
      paintQr(lastBox.querySelector("[data-qr]"), url);
      lastBox.querySelector("[data-act='copy-url']").addEventListener("click", async () => {
        const ok = await copyText(url);
        ok ? Toast.success(t("取件链接已复制")) : Toast.error(t("复制失败，请手动选中"));
      });
    }

    // ---- 历史记录 ----
    const listEl = panelEl.querySelector("[data-list]");
    const onHistory = state.pickupScope === "history";
    const rows = state.pickups.filter((item) =>
      onHistory ? item.status !== "active" : item.status === "active"
    );

    const emptyHtml = onHistory
      ? H`<div class="quick-empty">
          <svg class="icon"><use href="#i-clock"/></svg>
          <div>${t("还没有过期或已撤销的取件码")}</div>
        </div>`
      : H`<div class="quick-empty">
          <svg class="icon"><use href="#i-key"/></svg>
          <div>${t("还没有生成过取件码")}</div>
        </div>`;

    listEl.innerHTML =
      listbarHtml(state.pickupScope, state.pickupCounts) +
      (rows.length
        ? rows
            .map((item) => {
              const files = item.items || [];
              const aliveFiles = files.filter((file) => file.alive).length;
              return H`
      <div class="quick-row${item.status === "active" ? "" : " is-dead"}" data-id="${item.id}">
        <span class="quick-row__icon"><svg class="icon"><use href="#i-key"/></svg></span>
        <div class="quick-row__main">
          <div class="quick-row__name">${escapeHtml(item.name)}${item.code ? H`<span class="quick-code" style="font-size:13px;padding:2px 8px;margin-left:8px;letter-spacing:.12em">${escapeHtml(item.code)}</span>` : ""}</div>
          <div class="quick-row__meta">${t("{n} 个文件 · {alive} 个可下载", { n: files.length || item.count || 0, alive: aliveFiles })} · ${formatTime(item.createdAt)}</div>
        </div>
        <span class="quick-row__tag ${item.status === "active" ? "is-active" : "is-dead"}">${t(STATUS_TEXT[item.status] || item.status)}</span>
        <div class="quick-row__actions">
          ${
            onHistory
              ? H`<button class="icon-btn" type="button" data-act="forget" title="${t("删除记录")}">
                  <svg class="icon"><use href="#i-trash"/></svg>
                </button>`
              : H`<button class="icon-btn" type="button" data-act="copy" title="${t("复制取件链接")}">
                  <svg class="icon"><use href="#i-link"/></svg>
                </button>
                <button class="icon-btn" type="button" data-act="revoke" title="${t("撤销")}">
                  <svg class="icon"><use href="#i-close"/></svg>
                </button>`
          }
        </div>
      </div>`;
            })
            .join("")
        : emptyHtml);

    bindListbar(listEl, "pickup", "pickupScope", paintPickup);

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
          message: t("撤销后这个码立刻失效，已经发出去的码也取不下了。记录会收进「历史过期」。"),
          confirmText: t("撤销"),
          danger: true,
        });
        if (!ok) return;
        try {
          await api.del(`/api/quick/pickups/${item.id}`);
          item.status = "revoked";
          state.lastPickup = null;
          state.pickupCounts = {
            active: Math.max(0, state.pickupCounts.active - 1),
            history: state.pickupCounts.history + 1,
          };
          paintPickup();
          Toast.success(t("已撤销，记录收进历史过期"));
        } catch (err) {
          Toast.error(err.message);
        }
      });
      row.querySelector('[data-act="forget"]')?.addEventListener("click", async () => {
        const ok = await Dialog.confirm({
          title: t("删除这条记录"),
          message: t("这条取件码已经失效。删掉记录时，快传暂存区里只给它用的文件也会一起回收；从「我的文件」选进来的文件不受影响。"),
          confirmText: t("删除记录"),
          danger: true,
        });
        if (!ok) return;
        try {
          await api.del(`/api/quick/pickups/${item.id}`);
          state.pickups = state.pickups.filter((p) => p.id !== item.id);
          state.pickupCounts = {
            ...state.pickupCounts,
            history: Math.max(0, state.pickupCounts.history - 1),
          };
          paintPickup();
          Toast.success(t("记录已删除"));
        } catch (err) {
          Toast.error(err.message);
        }
      });
    });
  }

  /* ---------------- 数据 ---------------- */
  async function loadAll() {
    try {
      const [drops, inbox, outbox, pickups, limits, receive] = await Promise.all([
        api.get("/api/quick/drops"),
        api.get("/api/quick/inbox"),
        api.get("/api/quick/outbox"),
        api.get("/api/quick/pickups"),
        api.get("/api/quick/limits").catch(() => null),
        api.get("/api/quick/receive").catch(() => null),
      ]);
      state.drops = drops.items || [];
      state.inbox = inbox.items || [];
      state.unread = inbox.unread || 0;
      state.pending = inbox.pending || 0;
      state.outbox = outbox.items || [];
      state.unseenResults = outbox.unseenResults || 0;
      state.receive = receive?.receive || null;
      state.pickups = pickups.items || [];
      // 角标用服务端算好的数：只拉了前 300 条，自己数会漏
      state.dropCounts = drops.counts || {
        active: state.drops.filter((d) => d.status === "active").length,
        history: state.drops.filter((d) => d.status !== "active").length,
      };
      state.pickupCounts = pickups.counts || {
        active: state.pickups.filter((p) => p.status === "active").length,
        history: state.pickups.filter((p) => p.status !== "active").length,
      };
      if (limits?.limits) {
        state.limits = limits.limits;
        // 管理员调小上限后，默认值也要跟着收回来
        state.days = Math.min(state.days, state.limits.maxDays || state.days);
      }
    } catch (err) {
      Toast.error(err.message);
    }
  }

  root.querySelector('[data-act="refresh"]').addEventListener("click", async () => {
    await loadAll();
    paintBody();
  });

  async function boot() {
    try {
      const terms = await api.get("/api/quick/terms");
      state.termsAccepted = Boolean(terms.accepted);
    } catch {
      // 拿不到状态不把人挡在外面（服务端创建时还会再校验一次）
      state.termsAccepted = true;
    }
    paintBody();
    if (!state.termsAccepted) return;
    await loadAll();
    paintBody();
  }

  boot();
}
