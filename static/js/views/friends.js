/**
 * 好友（#/friends）
 *
 * 四个页签：好友 / 收到的申请 / 已发送 / 添加好友。
 * - 收到的申请可以直接同意或拒绝（拒绝即删行）
 * - 已发送的申请可以撤回
 * - 搜索按用户名或昵称模糊匹配，结果带上当前关系，避免重复申请
 */
import { api, escapeHtml, formatTime } from "../api.js";
import { H, t } from "../i18n.js";
import { avatarMarkup } from "../ui/avatar.js";
import { Dialog } from "../ui/dialog.js";
import { Toast } from "../ui/toast.js";
import { refreshNoticeBadge } from "../ui/notice.js";

const TABS = [
  { id: "friends", label: "好友", empty: "还没有好友，去「添加」里搜一下用户名吧" },
  { id: "incoming", label: "收到的", empty: "没有待处理的好友申请" },
  { id: "outgoing", label: "已发送", empty: "还没有发出过好友申请" },
  { id: "add", label: "添加", empty: "输入用户名或昵称搜索" },
];

export function renderFriends(root) {
  const state = {
    tab: "friends",
    data: { friends: [], incoming: [], outgoing: [] },
    query: "",
    results: [],
    searched: false,
    loading: true,
    busy: false,
  };

  root.innerHTML = H`
    <div class="view-head">
      <div class="view-title"><svg class="icon"><use href="#i-users"/></svg>好友</div>
      <span class="view-sub" data-sub></span>
      <div class="grow"></div>
      <button class="icon-btn" type="button" data-act="refresh" title="刷新">
        <svg class="icon"><use href="#i-refresh"/></svg>
      </button>
    </div>
    <div class="view-body">
      <div class="friend-body">
        <div class="segmented" data-tabs>
          ${TABS.map(
            (tab) =>
              H`<button class="segmented__btn ${tab.id === "friends" ? "is-active" : ""}" type="button"
                        data-friend-tab="${tab.id}">${t(tab.label)}<span class="listbar__count" data-count="${tab.id}" hidden>0</span></button>`
          ).join("")}
        </div>
        <div data-pane></div>
      </div>
    </div>`;

  const paneEl = root.querySelector("[data-pane]");
  const subEl = root.querySelector("[data-sub]");
  const tabsEl = root.querySelector("[data-tabs]");

  const counts = () => ({
    friends: state.data.friends.length,
    incoming: state.data.incoming.length,
    outgoing: state.data.outgoing.length,
  });

  function paintTabs() {
    const c = counts();
    tabsEl.querySelectorAll("[data-count]").forEach((el) => {
      const n = c[el.dataset.count] || 0;
      // 「好友」总数一直显示，申请类只在有时显示
      el.hidden = !n;
      el.textContent = n > 99 ? "99+" : String(n);
    });
    subEl.textContent = c.friends ? t("{n} 位好友", { n: c.friends }) : t("还没有好友");
  }

  function rowAvatar(item) {
    return H`<span class="friend-row__avatar">${avatarMarkup(item)}</span>`;
  }

  function friendRow(item) {
    return H`<div class="friend-row" data-user="${item.id}">
        ${rowAvatar(item)}
        <div class="friend-row__main">
          <div class="friend-row__name"><span>${escapeHtml(item.displayName)}</span></div>
          <div class="friend-row__meta">@${escapeHtml(item.username)}${
            item.bio ? H` · ${escapeHtml(item.bio)}` : ""
          }</div>
        </div>
        <div class="friend-row__ops">
          <button class="btn btn--subtle" type="button" data-act="remove" data-id="${item.id}">删除</button>
        </div>
      </div>`;
  }

  function incomingRow(item) {
    return H`<div class="friend-row" data-user="${item.id}">
        ${rowAvatar(item)}
        <div class="friend-row__main">
          <div class="friend-row__name"><span>${escapeHtml(item.displayName)}</span></div>
          <div class="friend-row__meta">@${escapeHtml(item.username)} · ${formatTime(item.since)}</div>
          ${item.message ? H`<div class="friend-row__msg">${escapeHtml(item.message)}</div>` : ""}
        </div>
        <div class="friend-row__ops">
          <button class="btn btn--accent" type="button" data-act="accept" data-request="${item.requestId}">同意</button>
          <button class="btn btn--subtle" type="button" data-act="reject" data-request="${item.requestId}">拒绝</button>
        </div>
      </div>`;
  }

  function outgoingRow(item) {
    return H`<div class="friend-row" data-user="${item.id}">
        ${rowAvatar(item)}
        <div class="friend-row__main">
          <div class="friend-row__name"><span>${escapeHtml(item.displayName)}</span>
            <span class="chip">等待对方处理</span></div>
          <div class="friend-row__meta">@${escapeHtml(item.username)} · ${formatTime(item.since)}</div>
          ${item.message ? H`<div class="friend-row__msg">${escapeHtml(item.message)}</div>` : ""}
        </div>
        <div class="friend-row__ops">
          <button class="btn btn--subtle" type="button" data-act="withdraw" data-id="${item.id}">撤回</button>
        </div>
      </div>`;
  }

  function emptyBlock(text) {
    return H`
      <div class="empty">
        <div class="empty__art"><svg class="icon"><use href="#i-users"/></svg></div>
        <div class="empty__title">${t(text)}</div>
      </div>`;
  }

  function addPane() {
    return H`
      <div class="friend-search">
        <div class="searchbox">
          <span class="textbox">
            <svg class="icon icon--s"><use href="#i-search"/></svg>
            <input type="search" data-search placeholder="搜用户名或昵称…" autocomplete="off"
                   aria-label="搜索用户" value="${escapeHtml(state.query)}">
          </span>
        </div>
        <button class="btn" type="button" data-act="search">搜索</button>
      </div>
      <p class="friend-hint">${t("只能搜到已注册且未被封禁的账号；申请通过后对方会收到一条消息。")}</p>
      <div data-results></div>`;
  }

  function searchRow(item) {
    const relation = item.relation;
    let ops = "";
    if (relation === "friends") ops = H`<span class="chip">${t("已是好友")}</span>`;
    else if (relation === "pending_out") ops = H`<span class="chip">${t("已申请")}</span>`;
    else if (relation === "pending_in")
      ops = H`<button class="btn btn--accent" type="button" data-act="open-incoming">${t("去处理")}</button>`;
    else
      ops = H`<button class="btn btn--accent" type="button" data-act="add" data-id="${item.id}">${t("添加")}</button>`;
    return H`<div class="friend-row" data-user="${item.id}">
        ${rowAvatar(item)}
        <div class="friend-row__main">
          <div class="friend-row__name"><span>${escapeHtml(item.displayName)}</span></div>
          <div class="friend-row__meta">@${escapeHtml(item.username)}${
            item.bio ? H` · ${escapeHtml(item.bio)}` : ""
          }</div>
        </div>
        <div class="friend-row__ops">${ops}</div>
      </div>`;
  }

  function paint() {
    if (state.tab === "add") {
      paneEl.innerHTML = addPane();
      paintResults();
      return;
    }
    const list = state.data[state.tab] || [];
    if (!list.length) {
      paneEl.innerHTML = emptyBlock(TABS.find((x) => x.id === state.tab)?.empty || "暂无内容");
      return;
    }
    const fn =
      state.tab === "friends" ? friendRow : state.tab === "incoming" ? incomingRow : outgoingRow;
    paneEl.innerHTML = H`<div class="friend-list">${list.map(fn).join("")}</div>`;
  }

  function paintResults() {
    const box = paneEl.querySelector("[data-results]");
    if (!box) return;
    if (!state.searched) {
      box.innerHTML = "";
      return;
    }
    if (!state.results.length) {
      box.innerHTML = emptyBlock("没有找到匹配的用户");
      return;
    }
    box.innerHTML = H`<div class="friend-list">${state.results.map(searchRow).join("")}</div>`;
  }

  async function load() {
    try {
      const data = await api.get("/api/friends");
      state.data = {
        friends: data.friends || [],
        incoming: data.incoming || [],
        outgoing: data.outgoing || [],
      };
    } catch (err) {
      Toast.error(err?.message || t("加载好友列表失败"));
    }
    state.loading = false;
    paintTabs();
    paint();
    void refreshNoticeBadge();
  }

  async function act(fn, okText) {
    if (state.busy) return;
    state.busy = true;
    try {
      await fn();
      if (okText) Toast.success(okText);
      await load();
    } catch (err) {
      Toast.error(err?.message || t("操作失败，请稍后重试"));
    } finally {
      state.busy = false;
    }
  }

  tabsEl.querySelectorAll("[data-friend-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.tab = btn.dataset.friendTab;
      tabsEl.querySelectorAll("[data-friend-tab]").forEach((b) => {
        b.classList.toggle("is-active", b === btn);
      });
      paint();
    });
  });

  root.querySelector('[data-act="refresh"]').addEventListener("click", () => void load());

  paneEl.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-act]");
    if (!btn || !paneEl.contains(btn)) return;
    const action = btn.dataset.act;
    if (action === "accept") {
      void act(() => api.post(`/api/friends/requests/${btn.dataset.request}/accept`), t("已添加为好友"));
    } else if (action === "reject") {
      void act(() => api.post(`/api/friends/requests/${btn.dataset.request}/reject`), t("已拒绝"));
    } else if (action === "withdraw") {
      void act(() => api.del(`/api/friends/${btn.dataset.id}`), t("已撤回申请"));
    } else if (action === "remove") {
      void removeFriend(btn.dataset.id);
    } else if (action === "open-incoming") {
      const incoming = tabsEl.querySelector('[data-friend-tab="incoming"]');
      if (incoming) incoming.click();
    } else if (action === "add") {
      void addFriend(btn.dataset.id);
    } else if (action === "search") {
      void doSearch();
    }
  });

  // 搜索框回车 / 输入防抖
  let timer = 0;
  paneEl.addEventListener("input", (event) => {
    const input = event.target.closest("[data-search]");
    if (!input) return;
    state.query = input.value;
    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      if (!root.isConnected) return;
      void doSearch({ keepInput: true });
    }, 380);
  });
  paneEl.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target.closest("[data-search]")) {
      event.preventDefault();
      void doSearch();
    }
  });

  async function doSearch({ keepInput = false } = {}) {
    const q = state.query.trim();
    if (!q) {
      state.searched = false;
      state.results = [];
      paintResults();
      return;
    }
    try {
      const data = await api.get(`/api/friends/search?q=${encodeURIComponent(q)}`);
      if (!root.isConnected) return;
      state.results = data.items || [];
      state.searched = true;
      paintResults();
      if (!keepInput) {
        const input = paneEl.querySelector("[data-search]");
        if (input) input.value = state.query;
      }
    } catch (err) {
      Toast.error(err?.message || t("搜索失败"));
    }
  }

  async function addFriend(id) {
    const message = await Dialog.prompt({
      title: t("发送好友申请"),
      message: t("可以附一句说明，让对方知道你是谁（可留空）"),
      label: t("附言"),
      placeholder: t("例如：我是群里的小明"),
      confirmText: t("发送申请"),
    });
    if (message === null) return;
    await act(async () => {
      await api.post("/api/friends/requests", { userId: Number(id), message });
      // 搜索结果的按钮状态就地改掉，不用重新搜一次
      const hit = state.results.find((x) => x.id === Number(id));
      if (hit) hit.relation = "pending_out";
      paintResults();
    }, t("好友申请已发送"));
  }

  async function removeFriend(id) {
    const ok = await Dialog.confirm({
      title: t("删除好友"),
      message: t("删除后你们就不再是好友，以后可以重新申请。"),
      confirmText: t("删除"),
      danger: true,
    });
    if (!ok) return;
    await act(() => api.del(`/api/friends/${id}`), t("已删除好友"));
  }

  void load();
}
