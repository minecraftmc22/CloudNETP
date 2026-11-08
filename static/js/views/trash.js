/**
 * 回收站视图：列表/网格浏览、搜索、多选、还原、彻底删除、清空
 */
import { api, escapeHtml, formatBytes, formatTime } from "../api.js";
import { H, t } from "../i18n.js";
import { store } from "../store.js";
import { Dialog } from "../ui/dialog.js";
import { Toast } from "../ui/toast.js";
import { openMenu } from "../ui/menu.js";

const RETENTION_FALLBACK = 30;

export function renderTrash(root) {
  const state = {
    items: [],
    view: store.get().user?.settings?.viewMode || "grid",
    selected: new Set(),
    keyword: "",
    cursor: -1,
    loading: false,
  };

  const retention = Number(store.get().meta?.trashRetentionDays) || RETENTION_FALLBACK;
  const trashOn = () => store.get().user?.settings?.trashEnabled !== false;

  root.innerHTML = H`
    <div class="view-head">
      <div class="view-title">
        <svg class="icon"><use href="#i-trash"/></svg>
        <span data-title>回收站</span>
      </div>
      <span class="view-sub" data-summary></span>
      <div class="grow"></div>
      <button class="icon-btn" type="button" data-act="refresh" title="刷新列表">
        <svg class="icon"><use href="#i-refresh"/></svg>
      </button>
      <button class="btn btn--danger" type="button" data-act="empty" disabled>
        <svg class="icon icon--s"><use href="#i-trash"/></svg>清空回收站
      </button>
    </div>

    <div class="toolbar">
      <div class="searchbox">
        <span class="textbox">
          <svg class="icon"><use href="#i-search"/></svg>
          <input type="search" placeholder="在回收站中搜索…" data-search autocomplete="off">
        </span>
      </div>
      <div class="toolbar__spacer"></div>
      <div class="row">
        <button class="icon-btn" type="button" data-view="grid" title="网格视图">
          <svg class="icon"><use href="#i-grid"/></svg>
        </button>
        <button class="icon-btn" type="button" data-view="list" title="列表视图">
          <svg class="icon"><use href="#i-list"/></svg>
        </button>
      </div>
    </div>

    <div class="view-body" data-body></div>

    <div class="selbar" data-selbar>
      <span class="selbar__count">已选 <b data-sel-count>0</b> 项</span>
      <button class="btn btn--subtle" type="button" data-act="sel-restore">
        <svg class="icon icon--s"><use href="#i-restore"/></svg>还原
      </button>
      <button class="btn btn--danger" type="button" data-act="sel-purge">
        <svg class="icon icon--s"><use href="#i-trash"/></svg>彻底删除
      </button>
      <button class="icon-btn" type="button" data-act="sel-clear" title="取消选择">
        <svg class="icon"><use href="#i-close"/></svg>
      </button>
    </div>
  `;

  const bodyEl = root.querySelector("[data-body]");
  const selbar = root.querySelector("[data-selbar]");
  const selCount = root.querySelector("[data-sel-count]");
  const searchEl = root.querySelector("[data-search]");
  const emptyBtn = root.querySelector('[data-act="empty"]');

  /* ------------------------------------------------------------------ */
  /* 数据                                                                */
  /* ------------------------------------------------------------------ */
  async function load() {
    state.loading = true;
    if (!state.items.length) bodyEl.innerHTML = '<div class="empty"><div class="spinner"></div></div>';
    try {
      const data = await api.get("/api/trash");
      state.items = data.items || [];
      for (const id of [...state.selected]) {
        if (!state.items.some((item) => item.id === id)) state.selected.delete(id);
      }
    } catch (err) {
      Toast.error(err.message);
      state.items = [];
    } finally {
      state.loading = false;
      render();
    }
  }

  async function refreshUser() {
    try {
      const data = await api.get("/api/me");
      store.set({ user: data.user });
      window.dispatchEvent(new CustomEvent("user:refresh", { detail: data.user }));
    } catch {
      /* 配额刷新失败不影响主流程 */
    }
  }

  const visibleItems = () =>
    state.keyword
      ? state.items.filter((item) => item.name.toLowerCase().includes(state.keyword.toLowerCase()))
      : state.items;

  /* ------------------------------------------------------------------ */
  /* 渲染                                                                */
  /* ------------------------------------------------------------------ */
  function render() {
    renderSummary();
    renderViewToggle();

    const items = visibleItems();
    if (!items.length) {
      bodyEl.innerHTML = state.keyword
        ? H`<div class="empty">
             <div class="empty__art"><svg class="icon"><use href="#i-search"/></svg></div>
             <div class="empty__title">没有匹配的项目</div>
             <div class="empty__desc">换个关键词试试。</div>
           </div>`
        : trashOn()
        ? H`<div class="empty">
             <div class="empty__art"><svg class="icon"><use href="#i-trash"/></svg></div>
             <div class="empty__title">回收站是空的</div>
             <div class="empty__desc">${t("删除的文件会先放到这里，可以随时还原。")}<br>${t("超过 {days} 天会自动彻底删除。", { days: retention })}</div>
           </div>`
        : H`<div class="empty">
             <div class="empty__art"><svg class="icon"><use href="#i-alert"/></svg></div>
             <div class="empty__title">回收站已关闭</div>
             <div class="empty__desc">现在删除文件会直接彻底删除，不经过回收站。<br>需要恢复保护的话，可以在设置里重新开启。</div>
             <button class="btn" type="button" data-act="go-settings">
               <svg class="icon icon--s"><use href="#i-settings"/></svg>去设置开启
             </button>
           </div>`;
      bodyEl.querySelector('[data-act="go-settings"]')?.addEventListener("click", () => {
        location.hash = "#/settings";
      });
      renderSelbar();
      return;
    }

    bodyEl.innerHTML = state.view === "list" ? renderList(items) : renderGrid(items);
    bindItems();
    renderSelbar();
  }

  function renderSummary() {
    const summary = root.querySelector("[data-summary]");
    const total = state.items.length;
    const bytes = state.items.reduce((sum, item) => (item.type === "file" ? sum + (item.size || 0) : sum), 0);
    summary.textContent = total
      ? H`${trashOn() ? "" : "已关闭 · "}${total} 项${bytes ? H` · 约 ${formatBytes(bytes)}` : ""}`
      : trashOn()
      ? t("暂无内容")
      : "已关闭";
    emptyBtn.disabled = !total;
  }

  function renderViewToggle() {
    root.querySelectorAll("[data-view]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.view === state.view);
    });
  }

  function metaOf(item) {
    const head = item.type === "folder" ? "文件夹" : formatBytes(item.size);
    return H`${head} · ${formatTime(item.deletedAt)}删除`;
  }

  function renderGrid(items) {
    return H`<div class="file-grid">
      ${items
        .map(
          (item, index) => H`
        <div class="tile ${state.selected.has(item.id) ? "is-selected" : ""} ${
          state.cursor === index ? "is-cursor" : ""
        }" data-id="${item.id}" data-type="${item.type}" data-index="${index}" tabindex="0" role="button">
          <div class="tile__icon" data-kind="${(item.icon || "i-file").slice(2)}"><svg class="icon"><use href="#${item.icon || "i-file"}"/></svg></div>
          <div class="tile__name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div>
          <div class="tile__meta">${escapeHtml(metaOf(item))}</div>
          <button class="tile__more" type="button" data-more="${item.id}" title="更多">
            <svg class="icon"><use href="#i-menu"/></svg>
          </button>
        </div>`
        )
        .join("")}
    </div>`;
  }

  function renderList(items) {
    return H`<div class="file-list">
      <div class="frow frow--head">
        <span></span><span></span>
        <span>名称</span>
        <span>大小</span>
        <span>删除时间</span>
        <span></span>
      </div>
      ${items
        .map(
          (item, index) => H`
        <div class="frow ${state.selected.has(item.id) ? "is-selected" : ""}"
             data-id="${item.id}" data-type="${item.type}" data-index="${index}" tabindex="0" role="button">
          <span class="frow__check"><input type="checkbox" ${
            state.selected.has(item.id) ? "checked" : ""
          } tabindex="-1"></span>
          <span class="frow__icon" data-kind="${(item.icon || "i-file").slice(2)}"><svg class="icon"><use href="#${item.icon || "i-file"}"/></svg></span>
          <span class="frow__name">
            <span title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
          </span>
          <span class="frow__size">${item.type === "folder" ? "—" : formatBytes(item.size)}</span>
          <span class="frow__time">${formatTime(item.deletedAt)}</span>
          <button class="frow__more" type="button" data-more="${item.id}" title="更多">
            <svg class="icon"><use href="#i-menu"/></svg>
          </button>
        </div>`
        )
        .join("")}
    </div>`;
  }

  function bindItems() {
    bodyEl.querySelectorAll("[data-id]").forEach((el) => {
      el.addEventListener("click", (event) => onItemClick(event, el));
      el.addEventListener("dblclick", () => {
        const item = findItem(Number(el.dataset.id));
        if (item) restoreItems([item]);
      });
      el.addEventListener("contextmenu", (event) => {
        event.preventDefault();
        const item = findItem(Number(el.dataset.id));
        if (!item) return;
        if (!state.selected.has(item.id)) {
          state.selected.clear();
          state.selected.add(item.id);
          updateSelectionStyles();
        }
        openItemMenu(event.clientX, event.clientY, item);
      });
    });

    bodyEl.querySelectorAll("[data-more]").forEach((btn) => {
      btn.addEventListener("click", (event) => {
        event.stopPropagation();
        const item = findItem(Number(btn.dataset.more));
        if (!item) return;
        if (!state.selected.has(item.id)) {
          state.selected.clear();
          state.selected.add(item.id);
          updateSelectionStyles();
        }
        const rect = btn.getBoundingClientRect();
        openItemMenu(rect.left, rect.bottom + 4, item);
      });
    });
  }

  // 点空白处取消选择（绑定一次，避免重复 render 叠加监听）
  bodyEl.addEventListener("mousedown", (event) => {
    if (!event.target.closest("[data-id]") && !event.target.closest("[data-more]")) {
      if (!state.selected.size) return;
      state.selected.clear();
      state.cursor = -1;
      updateSelectionStyles();
    }
  });

  /* ------------------------------------------------------------------ */
  /* 选择                                                                */
  /* ------------------------------------------------------------------ */
  const findItem = (id) => state.items.find((item) => item.id === id);
  const selectedItems = () => state.items.filter((item) => state.selected.has(item.id));

  function onItemClick(event, el) {
    const id = Number(el.dataset.id);
    const index = Number(el.dataset.index);
    const items = visibleItems();

    // 直接点复选框：只切换这一项
    if (event.target.matches('input[type="checkbox"]')) {
      if (state.selected.has(id)) state.selected.delete(id);
      else state.selected.add(id);
      state.cursor = index;
      updateSelectionStyles();
      return;
    }

    if (event.shiftKey && state.selected.size) {
      const anchorId = [...state.selected][0];
      const anchor = items.findIndex((item) => item.id === anchorId);
      if (anchor >= 0) {
        const [from, to] = anchor < index ? [anchor, index] : [index, anchor];
        state.selected.clear();
        for (let i = from; i <= to; i += 1) state.selected.add(items[i].id);
        state.cursor = index;
        updateSelectionStyles();
        return;
      }
    }

    if (event.ctrlKey || event.metaKey) {
      if (state.selected.has(id)) state.selected.delete(id);
      else state.selected.add(id);
    } else {
      state.selected.clear();
      state.selected.add(id);
    }
    state.cursor = index;
    updateSelectionStyles();
  }

  function updateSelectionStyles() {
    bodyEl.querySelectorAll("[data-id]").forEach((el) => {
      const id = Number(el.dataset.id);
      el.classList.toggle("is-selected", state.selected.has(id));
      el.classList.toggle("is-cursor", Number(el.dataset.index) === state.cursor);
      const box = el.querySelector('input[type="checkbox"]');
      if (box) box.checked = state.selected.has(id);
    });
    renderSelbar();
  }

  function renderSelbar() {
    const count = state.selected.size;
    selCount.textContent = String(count);
    selbar.classList.toggle("is-on", count > 0);
  }

  /* ------------------------------------------------------------------ */
  /* 操作                                                                */
  /* ------------------------------------------------------------------ */
  function openItemMenu(x, y, item) {
    const multi = state.selected.size > 1;
    const items = multi
      ? [
          { label: H`还原 ${state.selected.size} 项`, icon: "i-restore", onClick: () => restoreItems(selectedItems()) },
          { sep: true },
          {
            label: H`彻底删除 ${state.selected.size} 项`,
            icon: "i-trash",
            danger: true,
            onClick: () => purgeItems(selectedItems()),
          },
        ]
      : [
          { label: "还原", icon: "i-restore", onClick: () => restoreItems([item]) },
          { sep: true },
          { label: "彻底删除", icon: "i-trash", danger: true, onClick: () => purgeItems([item]) },
        ];

    openMenu({
      x,
      y,
      header: {
        name: multi ? H`${state.selected.size} 个项目` : item.name,
        meta: multi ? "批量操作" : metaOf(item),
      },
      items,
    });
  }

  async function restoreItems(items) {
    if (!items.length) return;
    const single = items.length === 1;
    try {
      for (const item of items) {
        await api.post(`/api/nodes/${item.id}/restore`);
      }
      Toast.success(single ? H`已还原「${items[0].name}」` : H`已还原 ${items.length} 项`);
      state.selected.clear();
      await load();
      await refreshUser();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async function purgeItems(items) {
    if (!items.length) return;
    const single = items.length === 1;
    const ok = await Dialog.confirm({
      title: single ? "彻底删除" : H`彻底删除 ${items.length} 项`,
      message: single
        ? H`「${escapeHtml(items[0].name)}」将被永久删除，无法恢复。${
            items[0].type === "folder" ? "文件夹里的内容会一起删除。" : ""
          }`
        : H`${items.length} 个项目将被永久删除，无法恢复。文件夹里的内容会一起删除。`,
      confirmText: "彻底删除",
      danger: true,
    });
    if (!ok) return;
    try {
      for (const item of items) {
        await api.del(`/api/nodes/${item.id}/purge`);
      }
      Toast.success(single ? "已彻底删除" : H`已彻底删除 ${items.length} 项`);
      state.selected.clear();
      await load();
      await refreshUser();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async function emptyTrash() {
    if (!state.items.length) return;
    const bytes = state.items.reduce((sum, item) => (item.type === "file" ? sum + (item.size || 0) : sum), 0);
    const ok = await Dialog.confirm({
      title: "清空回收站",
      message: H`回收站里的 ${state.items.length} 个项目${bytes ? H`（约 ${formatBytes(bytes)}）` : ""}将被永久删除，无法恢复。`,
      confirmText: "清空",
      danger: true,
    });
    if (!ok) return;
    try {
      const result = await api.post("/api/trash/empty");
      Toast.success(`已彻底删除 ${result?.removed ?? 0} 项`);
      state.selected.clear();
      await load();
      await refreshUser();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  /* ------------------------------------------------------------------ */
  /* 绑定                                                                */
  /* ------------------------------------------------------------------ */
  emptyBtn.addEventListener("click", emptyTrash);
  root.querySelector('[data-act="refresh"]').addEventListener("click", () => load());
  root.querySelector('[data-act="sel-clear"]').addEventListener("click", () => {
    state.selected.clear();
    state.cursor = -1;
    updateSelectionStyles();
  });
  root.querySelector('[data-act="sel-restore"]').addEventListener("click", () => restoreItems(selectedItems()));
  root.querySelector('[data-act="sel-purge"]').addEventListener("click", () => purgeItems(selectedItems()));

  searchEl.addEventListener("input", () => {
    state.keyword = searchEl.value.trim();
    state.cursor = -1;
    render();
  });

  root.querySelectorAll("[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (state.view === btn.dataset.view) return;
      state.view = btn.dataset.view;
      api.patch("/api/me/settings", { viewMode: state.view }).catch(() => {});
      const user = store.get().user;
      if (user?.settings) user.settings.viewMode = state.view;
      render();
    });
  });

  load();
}
