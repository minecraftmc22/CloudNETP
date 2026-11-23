/**
 * 消息中心角标：标题栏铃铛 + 底部导航「好友」上的待处理数。
 *
 * 只做两件事：
 * 1. 轮询 /api/notices/unread（45 秒一次，页面回到前台时立刻补一次）
 * 2. 把数字画到铃铛和数据导航项上；点铃铛直接进 #/notices
 *
 * 轮询很轻（两个 count），所以没必要做 SSE。
 */
import { api } from "../api.js";
import { t } from "../i18n.js";
import { store } from "../store.js";

const POLL_MS = 45_000;
const state = { unread: 0, pending: {}, chat: 0, timer: 0 };

function paint() {
  const btn = document.getElementById("notice-btn");
  if (btn) {
    const n = state.unread;
    const dot = btn.querySelector("[data-notice-dot]");
    if (dot) {
      dot.hidden = n <= 0;
      dot.textContent = n > 99 ? "99+" : String(n);
    }
    btn.classList.toggle("has-unread", n > 0);
    const label = n ? t("消息中心（{n} 条未读）", { n }) : t("消息中心");
    btn.title = label;
    btn.setAttribute("aria-label", label);
  }
  // 好友项上的小角标：待处理的好友申请数
  const badge = document.querySelector('.nav-item[data-route="friends"] [data-nav-badge]');
  if (badge) {
    const fr = Number(state.pending?.friendRequests || 0);
    badge.textContent = fr > 9 ? "9+" : String(fr);
    badge.hidden = fr <= 0;
  }
  // 聊天项：未读消息数（跟铃铛一起刷，不用为它单独挂一个计时器）
  const chatBadge = document.querySelector('.nav-item[data-route="chat"] [data-nav-badge]');
  if (chatBadge) {
    const n = Number(state.chat || 0);
    chatBadge.textContent = n > 99 ? "99+" : String(n);
    chatBadge.hidden = n <= 0;
  }
}

export async function refreshNoticeBadge() {
  if (!store.get().user) {
    state.unread = 0;
    state.pending = {};
    state.chat = 0;
    paint();
    return;
  }
  try {
    const [data, chat] = await Promise.all([
      api.get("/api/notices/unread"),
      // 聊天未读单独一个接口：通知接口不该被聊天消息拖慢
      api.get("/api/chat/unread").catch(() => null),
    ]);
    state.unread = Number(data.unread) || 0;
    state.pending = data.pending || {};
    state.chat = Number(chat?.unread) || 0;
    paint();
  } catch {
    /* 网络抖动就保持上一次的数字，别把角标清空 */
  }
}

export async function markAllNoticesRead() {
  try {
    await api.post("/api/notices/read", {});
  } catch {
    /* 失败也照常刷新，让用户看到真实状态 */
  }
  await refreshNoticeBadge();
}

export function startNoticeWatch() {
  stopNoticeWatch();
  paint();
  void refreshNoticeBadge();
  state.timer = window.setInterval(() => {
    if (document.visibilityState === "visible") void refreshNoticeBadge();
  }, POLL_MS);
}

export function stopNoticeWatch() {
  if (state.timer) {
    window.clearInterval(state.timer);
    state.timer = 0;
  }
  state.unread = 0;
  state.pending = {};
  paint();
}

export function initNoticeBell() {
  const btn = document.getElementById("notice-btn");
  if (!btn) return;
  btn.addEventListener("click", () => {
    if (location.hash === "#/notices") {
      void refreshNoticeBadge();
      return;
    }
    location.hash = "#/notices";
  });
  // 从后台切回前台立刻补一次，别让人等满一个轮询周期
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state.timer) void refreshNoticeBadge();
  });
  // 好友页处理完申请 / 后台处理完审核，都会广播这个事件让角标即时更新
  window.addEventListener("notice:refresh", () => void refreshNoticeBadge());
}
