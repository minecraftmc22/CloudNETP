/**
 * 消息中心（#/notices）
 *
 * - 上半部分是「待处理」：管理员的举报 / 申诉 / 反馈队列，加好友申请，点一下就跳过去
 * - 下半部分是通知流：新举报 / 新申诉 / 新反馈 / 好友申请 / 申请通过
 * - 点一条 = 标记已读 + 跳到对应页面；「全部已读」清角标
 *
 * 通知由服务端在事件发生时按收件人写行（见 app/notice_service.py），
 * 所以这里不需要客户端拼装任何事件，照 list 渲染就行。
 */
import { api, escapeHtml, formatTime } from "../api.js";
import { H, t } from "../i18n.js";
import { Toast } from "../ui/toast.js";
import { refreshNoticeBadge } from "../ui/notice.js";

const KIND = {
  report: { icon: "i-alert", label: "举报审核" },
  appeal: { icon: "i-shield", label: "申诉审核" },
  feedback: { icon: "i-mail", label: "意见反馈" },
  friend_request: { icon: "i-user-plus", label: "好友申请" },
  friend_accept: { icon: "i-users", label: "好友" },
  quick: { icon: "i-bolt", label: "快传" },
  chat: { icon: "i-chat", label: "聊天" },
  system: { icon: "i-bell", label: "系统" },
};

const PENDING = [
  { key: "reports", icon: "i-alert", label: "待审核举报", link: "#/admin/reports" },
  { key: "appeals", icon: "i-shield", label: "待审核申诉", link: "#/admin/appeals" },
  { key: "feedback", icon: "i-mail", label: "待处理反馈", link: "#/admin/feedback" },
  { key: "chatReports", icon: "i-chat", label: "聊天举报", link: "#/admin/chat" },
  { key: "friendRequests", icon: "i-user-plus", label: "好友申请", link: "#/friends" },
];

export function renderNotices(root, { user } = {}) {
  const state = {
    items: [],
    pending: {},
    filter: "all", // all / unread
    error: "",
  };
  const isAdmin = user?.role === 1 || user?.isSuper;

  root.innerHTML = H`
    <div class="view-head">
      <div class="view-title"><svg class="icon"><use href="#i-bell"/></svg>消息中心</div>
      <span class="view-sub" data-sub></span>
      <div class="grow"></div>
      <button class="btn btn--subtle" type="button" data-act="read-all" hidden>全部已读</button>
      <button class="icon-btn" type="button" data-act="refresh" title="刷新">
        <svg class="icon"><use href="#i-refresh"/></svg>
      </button>
    </div>
    <div class="view-body">
      <div class="notice-body">
        <div class="notice-pending" data-pending hidden></div>
        <div class="segmented" data-filter>
          <button class="segmented__btn is-active" type="button" data-scope="all">全部</button>
          <button class="segmented__btn" type="button" data-scope="unread">未读</button>
        </div>
        <div data-list><div class="admin-loading"><span class="spinner"></span></div></div>
        <div class="notice-foot" data-foot hidden>
          <button class="btn btn--subtle btn--sm" type="button" data-act="clear-read">清空已读通知</button>
        </div>
      </div>
    </div>`;

  const listEl = root.querySelector("[data-list]");
  const pendingEl = root.querySelector("[data-pending]");
  const subEl = root.querySelector("[data-sub]");
  const footEl = root.querySelector("[data-foot]");
  const readAllEl = root.querySelector('[data-act="read-all"]');
  const filterEl = root.querySelector("[data-filter]");

  const unreadCount = () => state.items.filter((n) => !n.read).length;

  function paintPending() {
    // 管理员的三个审核队列恒显示（0 也显示，方便知道「暂时没活」）；
    // 好友申请只在真有时才占位，不打扰普通用户
    const rows = (isAdmin ? PENDING : PENDING.filter((i) => i.key === "friendRequests")).filter(
      (item) => isAdmin || Number(state.pending[item.key] || 0) > 0
    );
    if (!rows.length) {
      pendingEl.hidden = true;
      return;
    }
    pendingEl.hidden = false;
    pendingEl.innerHTML = H`${rows
      .map((item) => {
        const n = Number(state.pending[item.key] || 0);
        return H`<button class="notice-tile ${n ? "" : "is-zero"}" type="button"
                        data-pending-link="${item.link}">
            <span class="notice-tile__icon"><svg class="icon icon--s"><use href="#${item.icon}"/></svg></span>
            <span class="notice-tile__main">
              <span class="notice-tile__num">${n}</span>
              <span class="notice-tile__label">${t(item.label)}</span>
            </span>
          </button>`;
      })
      .join("")}`;
    pendingEl.querySelectorAll("[data-pending-link]").forEach((btn) => {
      btn.addEventListener("click", () => {
        location.hash = btn.dataset.pendingLink;
      });
    });
  }

  function itemHtml(item) {
    const meta = KIND[item.kind] || KIND.system;
    return H`<button class="notice-item ${item.read ? "" : "is-unread"}" type="button" data-id="${item.id}"
                   data-link="${escapeHtml(item.link || "")}">
        <span class="notice-item__icon"><svg class="icon"><use href="#${meta.icon}"/></svg></span>
        <span class="notice-item__main">
          <span class="notice-item__title">${escapeHtml(item.title)}</span>
          ${item.body ? H`<span class="notice-item__body">${escapeHtml(item.body)}</span>` : ""}
        </span>
        <span class="notice-item__time">${formatTime(item.createdAt)}</span>
      </button>`;
  }

  function paintList() {
    const items =
      state.filter === "unread" ? state.items.filter((n) => !n.read) : state.items.slice();
    if (!items.length) {
      listEl.innerHTML = H`
        <div class="empty">
          <div class="empty__art"><svg class="icon"><use href="#i-bell"/></svg></div>
          <div class="empty__title">${state.filter === "unread" ? t("没有未读消息") : t("还没有消息")}</div>
          <div class="empty__desc">${t("新的举报、申诉、意见反馈和好友申请都会出现在这里")}</div>
        </div>`;
    } else {
      listEl.innerHTML = H`<div class="notice-list">${items.map(itemHtml).join("")}</div>`;
    }
    listEl.querySelectorAll("[data-id]").forEach((btn) => {
      btn.addEventListener("click", () => void openItem(Number(btn.dataset.id), btn.dataset.link));
    });
    // 页头 / 页脚状态
    const unread = unreadCount();
    readAllEl.hidden = unread <= 0;
    readAllEl.textContent = unread ? t("全部已读（{n}）", { n: unread }) : t("全部已读");
    footEl.hidden = !state.items.some((n) => n.read);
    subEl.textContent = unread ? t("{n} 条未读", { n: unread }) : t("全部已读");
  }

  async function openItem(id, link) {
    const item = state.items.find((n) => n.id === id);
    if (item && !item.read) {
      item.read = true;
      try {
        await api.post("/api/notices/read", { ids: [id] });
      } catch {
        item.read = false;
      }
      paintList();
      void refreshNoticeBadge();
    }
    if (link) location.hash = link;
  }

  async function load() {
    try {
      const data = await api.get("/api/notices?limit=100");
      state.items = data.items || [];
      state.pending = data.pending || {};
      state.error = "";
    } catch (err) {
      state.items = [];
      state.error = err?.message || String(err);
    }
    if (state.error) {
      listEl.innerHTML = H`
        <div class="empty">
          <div class="empty__art"><svg class="icon"><use href="#i-alert"/></svg></div>
          <div class="empty__title">${t("加载失败")}</div>
          <div class="empty__desc">${escapeHtml(state.error)}</div>
        </div>`;
      return;
    }
    paintPending();
    paintList();
  }

  filterEl.querySelectorAll("[data-scope]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.filter = btn.dataset.scope;
      filterEl.querySelectorAll("[data-scope]").forEach((b) => {
        b.classList.toggle("is-active", b === btn);
      });
      paintList();
    });
  });

  readAllEl.addEventListener("click", async () => {
    try {
      await api.post("/api/notices/read", {});
      state.items.forEach((n) => {
        n.read = true;
      });
      paintList();
      void refreshNoticeBadge();
      Toast.success(t("已全部标记为已读"));
    } catch {
      Toast.error(t("操作失败，请稍后重试"));
    }
  });

  root.querySelector('[data-act="refresh"]').addEventListener("click", () => void load());
  root.querySelector('[data-act="clear-read"]').addEventListener("click", async () => {
    try {
      await api.del("/api/notices/read");
      state.items = state.items.filter((n) => !n.read);
      paintList();
      Toast.info(t("已清空已读通知"));
    } catch {
      Toast.error(t("清空失败，请稍后重试"));
    }
  });

  void load();
}
