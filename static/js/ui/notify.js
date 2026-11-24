/**
 * 消息提醒：站内小卡片 + 浏览器桌面通知
 *
 * 数据来自 /api/chat/alerts?after=<上次提醒到的消息 id>：
 * 只回「比上次提醒点更新的未读消息」，一条 join 查询，20 秒轮一次也不重。
 *
 * 三种情形：
 * 1. 页面在前台、且不在那个会话里 → 右下角小卡片（点了直接进会话）
 * 2. 页面在后台（切了标签页 / 最小化）→ 系统通知（要用户授权）
 * 3. 正在那个会话里看 → 什么都不弹（消息流自己会显示）
 *
 * 首次启动不补弹历史：只把「当前最新消息 id」记下来当起点，
 * 否则登录一次会被过去几十条消息刷屏。
 */
import { api } from "../api.js";
import { H, t } from "../i18n.js";
import { store } from "../store.js";

const SEEN_KEY = "cn:alertSeen";
const POLL_MS = 20000;
const MAX_CARDS = 3;

let timer = 0;
let starting = false;
let stackEl = null;
const shown = new Set();
let desktopTimer = 0;

function settings() {
  return store.get().user?.settings || {};
}

function lastSeen() {
  const raw = Number(localStorage.getItem(SEEN_KEY) || 0);
  return Number.isFinite(raw) && raw > 0 ? raw : 0;
}

function setSeen(value) {
  const n = Number(value) || 0;
  if (n > 0) localStorage.setItem(SEEN_KEY, String(n));
}

/** 当前正在看的会话 id（#/chat/<id>），不在聊天页返回 0 */
function activeConvId() {
  const match = location.hash.match(/^#\/chat\/(\d+)/);
  return match ? Number(match[1]) : 0;
}

function visible() {
  return document.visibilityState === "visible" && document.hasFocus();
}

function ensureStack() {
  if (stackEl?.isConnected) {
    liftAboveComposer(stackEl);
    return stackEl;
  }
  stackEl = document.createElement("div");
  stackEl.className = "notify-stack";
  stackEl.setAttribute("role", "status");
  document.body.appendChild(stackEl);
  liftAboveComposer(stackEl);
  return stackEl;
}

/**
 * 聊天页底部是输入区，卡片压在右下角会挡住发送按钮。
 * 有输入区就抬到它上面，没有就回到默认的贴底位置。
 */
function liftAboveComposer(stack) {
  const compose = document.querySelector(".chat-compose");
  if (!compose) {
    stack.style.bottom = "";
    return;
  }
  const rect = compose.getBoundingClientRect();
  const offset = Math.round(window.innerHeight - rect.top) + 10;
  stack.style.bottom = rect.height > 0 ? `${Math.max(18, offset)}px` : "";
}

function pushCard(item) {
  const stack = ensureStack();
  const card = document.createElement("div");
  card.className = "notify-card";
  card.dataset.conv = String(item.conversationId);
  card.innerHTML = H`
    <span class="notify-card__avatar">${item.avatarUrl
      ? `<img src="${item.avatarUrl}" alt="">`
      : `<span class="avatar-initial">${(item.title || "?").slice(0, 1).toUpperCase()}</span>`}</span>
    <span class="notify-card__main">
      <span class="notify-card__title"></span>
      <span class="notify-card__text"></span>
    </span>
    <button class="notify-card__close" type="button" aria-label="${t("关闭提醒")}">
      <svg class="icon icon--s"><use href="#i-close"/></svg>
    </button>`;
  card.querySelector(".notify-card__title").textContent =
    item.kind === "group" ? `${item.title} · ${item.senderName}` : item.title;
  card.querySelector(".notify-card__text").textContent = item.preview || "";
  card.addEventListener("click", (event) => {
    if (event.target.closest(".notify-card__close")) {
      card.remove();
      return;
    }
    card.remove();
    location.hash = `#/chat/${item.conversationId}`;
  });
  stack.appendChild(card);
  // 最多留 3 张，多了从最早的开始丢
  while (stack.children.length > MAX_CARDS) stack.firstElementChild?.remove();
  window.setTimeout(() => card.remove(), 8000);
}

function systemNotify(items) {
  if (!("Notification" in window)) return;
  if (Notification.permission !== "granted") return;
  if (!settings().notifyDesktop) return;
  const first = items[0];
  const count = items.length;
  const body =
    count === 1
      ? `${first.kind === "group" ? `${first.title} · ` : ""}${first.senderName}：${first.preview}`
      : t("你有 {n} 条新消息", { n: count });
  const title = count === 1 ? first.title || t("新消息") : t("新消息");
  try {
    const notification = new Notification(title, {
      body,
      icon: first.avatarUrl || "/assets/favicon-32.png",
      tag: first.conversationId ? `conv-${first.conversationId}` : "chat",
      silent: false,
    });
    notification.onclick = () => {
      window.focus();
      if (first.conversationId) location.hash = `#/chat/${first.conversationId}`;
      notification.close();
    };
    // 点一下就消失，别在系统托盘里堆着
    window.clearTimeout(desktopTimer);
    desktopTimer = window.setTimeout(() => notification.close(), 9000);
  } catch {
    /* 某些浏览器（iOS Safari）不支持直接 new Notification，静默跳过 */
  }
}

function handle(items) {
  const active = activeConvId();
  const onChatPage = location.hash.startsWith("#/chat");
  const targets = items.filter((item) => !(visible() && onChatPage && item.conversationId === active));
  if (!targets.length) return;

  if (visible()) {
    if (settings().notifyInapp === false) return;
    // 同一个会话的多条合成一条，避免一次冒出五六张卡
    const byConv = new Map();
    for (const item of targets) {
      const key = item.conversationId;
      if (byConv.has(key)) {
        const prev = byConv.get(key);
        prev.preview = item.preview;
        prev.senderName = item.senderName;
        prev.count = (prev.count || 1) + 1;
      } else {
        byConv.set(key, { ...item, count: 1 });
      }
    }
    for (const item of byConv.values()) {
      if (shown.has(item.id)) continue;
      shown.add(item.id);
      pushCard(
        item.count > 1
          ? { ...item, preview: t("{n} 条新消息", { n: item.count }) }
          : item
      );
    }
    return;
  }
  systemNotify(targets);
}

async function tick({ silent = false } = {}) {
  const user = store.get().user;
  if (!user) return;
  const after = lastSeen();
  try {
    const data = await api.get(`/api/chat/alerts?after=${after}`);
    const items = data.messages || [];
    const latest = Number(data.latest) || 0;
    if (silent || !after) {
      // 首次启动：只记起点，不补弹历史
      if (latest) setSeen(latest);
      return;
    }
    if (items.length) handle(items);
    if (latest > after) setSeen(latest);
  } catch {
    /* 轮询失败不打扰用户，下一轮再说 */
  }
}

export function startNotifier() {
  if (starting || timer) return;
  starting = true;
  void (async () => {
    await tick({ silent: true });
    starting = false;
    if (timer) return;
    timer = window.setInterval(() => {
      if (!store.get().user) return;
      void tick();
    }, POLL_MS);
  })();
  // 从后台切回来时马上补一次，不用等下一个 20 秒
  document.addEventListener("visibilitychange", onVisible);
}

export function stopNotifier() {
  window.clearInterval(timer);
  timer = 0;
  document.removeEventListener("visibilitychange", onVisible);
  stackEl?.remove();
  stackEl = null;
  shown.clear();
}

function onVisible() {
  if (document.visibilityState === "visible") void tick();
}

export function desktopPermission() {
  if (!("Notification" in window)) return "unsupported";
  return Notification.permission;
}

export async function askDesktopPermission() {
  if (!("Notification" in window)) {
    return "unsupported";
  }
  if (Notification.permission === "granted") return "granted";
  try {
    return await Notification.requestPermission();
  } catch {
    return "denied";
  }
}

/** 改了提醒设置后调用：把「已提醒到哪」重置一下，免得关掉再开会补弹 */
export function resetNotifierCursor() {
  void tick({ silent: true });
}
