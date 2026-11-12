/**
 * 文件视图：目录树浏览、网格/列表、多选、右键菜单、拖拽上传
 */
import { api, escapeHtml, formatBytes, formatTime } from "../api.js";
import { H, t } from "../i18n.js";
import { store } from "../store.js";
import { Dialog } from "../ui/dialog.js";
import { Toast } from "../ui/toast.js";
import { openMenu } from "../ui/menu.js";
import { pickFolder } from "../ui/picker.js";
import { runTask } from "../ui/tasks.js";
import { canPreview, openViewer } from "../ui/viewer.js";
import { shareItem, shareItems } from "../ui/share.js";
import { enqueue as enqueueUploadsQueue, onChange as onUploadChange } from "../ui/upload-queue.js";

const SORT_LABELS = { name: "名称", size: "大小", updated: "修改时间", created: "创建时间" };
const UPLOAD_CONCURRENCY = 2;

export function renderFiles(root) {
  const state = {
    parentId: null,
    breadcrumb: [{ id: null, name: "我的文件" }],
    items: [],
    view: store.get().user?.settings?.viewMode || "grid",
    sort: "name",
    order: "asc",
    selected: new Set(),
    cursor: -1,
    keyword: "",
    searchMode: false,
    loading: false,
  };

  root.innerHTML = H`
    <div class="view-head">
      <div class="view-title">
        <svg class="icon"><use href="#i-folder"/></svg>
        <span data-title>我的文件</span>
      </div>
      <span class="view-sub" data-quota></span>
      <div class="grow"></div>
      <button class="icon-btn" type="button" data-act="refresh" title="刷新列表">
        <svg class="icon"><use href="#i-refresh"/></svg>
      </button>
      <button class="btn" type="button" data-act="new-folder">
        <svg class="icon icon--s"><use href="#i-plus"/></svg>新建文件夹
      </button>
      <button class="btn btn--accent" type="button" data-act="upload">
        <svg class="icon icon--s"><use href="#i-upload"/></svg>上传
      </button>
      <input type="file" multiple hidden data-file-input>
    </div>

    <div class="crumb-bar">
      <nav class="crumb" data-crumb aria-label="路径"></nav>
      <div class="grow"></div>
      <button class="sort-btn" type="button" data-act="sort" title="切换排序">
        <span data-sort-label>${t("名称")}</span>
        <svg class="icon"><use href="#i-chevron"/></svg>
      </button>
    </div>

    <div class="toolbar">
      <div class="searchbox">
        <span class="textbox">
          <svg class="icon"><use href="#i-search"/></svg>
          <input type="search" placeholder="搜索全部文件…" data-search autocomplete="off">
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

    <div class="files-stage">
      <div class="view-body" data-body></div>
      <div class="files-loading" data-loading hidden>
        <div class="spinner"></div>
        <span>${t("加载中…")}</span>
      </div>
    </div>

    <div class="selbar" data-selbar>
      <span class="selbar__count">${t("已选")} <b data-sel-count>0</b> ${t("项")}</span>
      <button class="btn btn--subtle" type="button" data-act="sel-download">
        <svg class="icon icon--s"><use href="#i-download"/></svg>下载
      </button>
      <button class="btn btn--subtle" type="button" data-act="sel-share">
        <svg class="icon icon--s"><use href="#i-link"/></svg>分享
      </button>
      <button class="btn btn--subtle" type="button" data-act="sel-move">
        <svg class="icon icon--s"><use href="#i-folder"/></svg>移动
      </button>
      <button class="btn btn--danger" type="button" data-act="sel-delete">
        <svg class="icon icon--s"><use href="#i-trash"/></svg>删除
      </button>
      <button class="icon-btn" type="button" data-act="sel-clear" title="取消选择">
        <svg class="icon"><use href="#i-close"/></svg>
      </button>
    </div>

    <div class="drop-mask" data-drop>
      <div class="drop-mask__inner">
        <svg class="icon"><use href="#i-upload"/></svg>
        <span>松手即上传到当前目录，文件夹会保留原有目录结构</span>
      </div>
    </div>
  `;

  const bodyEl = root.querySelector("[data-body]");
  const loadingEl = root.querySelector("[data-loading]");
  const crumbEl = root.querySelector("[data-crumb]");
  const selbar = root.querySelector("[data-selbar]");
  const selCount = root.querySelector("[data-sel-count]");
  const searchEl = root.querySelector("[data-search]");
  const dropMask = root.querySelector("[data-drop]");
  const fileInput = root.querySelector("[data-file-input]");

  /* ------------------------------------------------------------------ */
  /* 数据加载                                                            */
  /* ------------------------------------------------------------------ */
  async function load({ keepScroll = false } = {}) {
    state.loading = true;
    setLoading(true);
    if (!keepScroll) bodyEl.scrollTop = 0;
    try {
      if (state.searchMode) {
        const data = await api.get(`/api/search?q=${encodeURIComponent(state.keyword)}`);
        state.items = data.items || [];
      } else {
        const params = new URLSearchParams({ sort: state.sort, order: state.order });
        if (state.parentId !== null && state.parentId !== undefined) {
          params.set("parentId", String(state.parentId));
        }
        const data = await api.get(`/api/nodes?${params.toString()}`);
        state.items = data.items || [];
        state.breadcrumb = data.breadcrumb || [{ id: null, name: "我的文件" }];
      }
      state.selected.clear();
      state.cursor = -1;
    } catch (err) {
      bodyEl.innerHTML = H`<div class="empty"><div class="empty__art"><svg class="icon"><use href="#i-alert"/></svg></div>
        <div class="empty__title">加载失败</div><div class="empty__desc">${escapeHtml(err.message)}</div></div>`;
      state.loading = false;
      setLoading(false);
      return;
    }
    state.loading = false;
    setLoading(false);
    render();
  }

  /** 切目录 / 首次进入时的加载提示：盖在列表上，带旋转图标 */
  let loadingDepth = 0;
  function setLoading(on) {
    loadingDepth = Math.max(0, loadingDepth + (on ? 1 : -1));
    if (!loadingEl) return;
    const busy = loadingDepth > 0;
    loadingEl.hidden = !busy;
    bodyEl.classList.toggle("is-loading", busy);
  }

  // 上传队列是全局的，切页回来要能看到期间传完的文件
  let refreshTimer = 0;
  onUploadChange(() => {
    if (!root.isConnected) return;
    window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(() => {
      if (!root.isConnected) return;
      load({ keepScroll: true });
      refreshUser();
    }, 600);
  });

  async function refreshUser() {
    try {
      const data = await api.get("/api/me");
      store.set({ user: data.user });
      window.dispatchEvent(new CustomEvent("user:refresh", { detail: data.user }));
      renderQuota();
    } catch {
      /* 忽略 */
    }
  }

  /* ------------------------------------------------------------------ */
  /* 渲染                                                                */
  /* ------------------------------------------------------------------ */
  function renderQuota() {
    const user = store.get().user;
    const quotaEl = root.querySelector("[data-quota]");
    if (user && quotaEl) quotaEl.textContent = `${formatBytes(user.usedBytes)} / ${formatBytes(user.quotaBytes)}`;
    const title = root.querySelector("[data-title]");
    if (title) title.textContent = state.searchMode ? t("搜索结果") : t("我的文件");
    const total = state.items.length;
    if (quotaEl && state.searchMode) quotaEl.textContent = t("找到 {n} 项", { n: total });
  }

  function renderCrumb() {
    if (state.searchMode) {
      crumbEl.innerHTML = `<span class="crumb__item is-current">${t("搜索「{q}」", { q: escapeHtml(state.keyword) })}</span>`;
      return;
    }
    // 根目录下面包屑与标题重复，只留排序按钮
    if (state.breadcrumb.length <= 1) {
      crumbEl.innerHTML = "";
      return;
    }
    crumbEl.innerHTML = state.breadcrumb
      .map(
        (item, index) => H`
        <button class="crumb__item ${index === state.breadcrumb.length - 1 ? "is-current" : ""}"
                type="button" data-crumb-id="${item.id ?? ""}">${escapeHtml(item.id == null ? t(item.name) : item.name)}</button>
        ${index < state.breadcrumb.length - 1 ? '<span class="crumb__sep"><svg class="icon"><use href="#i-chevron"/></svg></span>' : ""}`
      )
      .join("");
    crumbEl.querySelectorAll("[data-crumb-id]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const raw = btn.dataset.crumbId;
        state.parentId = raw === "" ? null : Number(raw);
        state.searchMode = false;
        state.keyword = "";
        searchEl.value = "";
        load();
      });
    });
  }

  function render() {
    renderCrumb();
    renderQuota();
    renderViewToggle();
    renderSortLabel();
    updateSelbar();

    if (!state.items.length) {
      bodyEl.innerHTML = state.searchMode
        ? H`<div class="empty">
             <div class="empty__art"><svg class="icon"><use href="#i-search"/></svg></div>
             <div class="empty__title">没有匹配的文件</div>
             <div class="empty__desc">${t("换个关键词试试，搜索会匹配所有文件夹里的名称。")}</div>
           </div>`
        : H`<div class="empty">
             <div class="empty__art"><svg class="icon"><use href="#i-cloud"/></svg></div>
             <div class="empty__title">这里还空空的</div>
             <div class="empty__desc">把文件拖到窗口里，或点右上角「上传」。<br>相同内容只占一份空间（按 SHA-256 去重）。</div>
             <button class="btn btn--accent" type="button" data-act="upload">
               <svg class="icon icon--s"><use href="#i-upload"/></svg>上传第一个文件
             </button>
           </div>`;
      bodyEl.querySelector('[data-act="upload"]')?.addEventListener("click", () => fileInput.click());
      return;
    }

    bodyEl.innerHTML = state.view === "list" ? renderList() : renderGrid();
    bindItems();
  }

  function metaOf(item) {
    if (item.type === "folder") {
      const count = item.childCount ?? 0;
      return H`${count} 项 · ${formatTime(item.updatedAt)}`;
    }
    return H`${formatBytes(item.size)} · ${formatTime(item.updatedAt)}`;
  }

  function renderGrid() {
    return H`<div class="file-grid">
      ${state.items
        .map(
          (item, index) => H`
        <div class="tile ${state.selected.has(item.id) ? "is-selected" : ""} ${state.cursor === index ? "is-cursor" : ""}"
             data-id="${item.id}" data-type="${item.type}" data-index="${index}" tabindex="0" role="button">
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

  function renderList() {
    const arrow = (key) =>
      state.sort === key
        ? H`<svg class="icon"><use href="#i-chevron"/></svg>`
        : H`<svg class="icon" style="opacity:0"><use href="#i-chevron"/></svg>`;
    return H`<div class="file-list">
      <div class="frow frow--head">
        <span></span><span></span>
        <button type="button" data-sort="name" class="${state.sort === "name" ? H`is-sorted ${state.order === "desc" ? "is-desc" : ""}` : ""}">${t("名称")}${arrow("name")}</button>
        <button type="button" data-sort="size" class="${state.sort === "size" ? H`is-sorted ${state.order === "desc" ? "is-desc" : ""}` : ""}">大小${arrow("size")}</button>
        <button type="button" data-sort="updated" class="${state.sort === "updated" ? H`is-sorted ${state.order === "desc" ? "is-desc" : ""}` : ""}">${t("修改时间")}${arrow("updated")}</button>
        <span></span>
      </div>
      ${state.items
        .map(
          (item, index) => H`
        <div class="frow ${state.selected.has(item.id) ? "is-selected" : ""}"
             data-id="${item.id}" data-type="${item.type}" data-index="${index}" tabindex="0" role="button">
          <span class="frow__check"><input type="checkbox" ${state.selected.has(item.id) ? "checked" : ""} tabindex="-1"></span>
          <span class="frow__icon" data-kind="${(item.icon || "i-file").slice(2)}"><svg class="icon"><use href="#${item.icon || "i-file"}"/></svg></span>
          <span class="frow__name">
            <span title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
            ${item.path ? H`<span class="frow__path">${escapeHtml(item.path)}</span>` : ""}
          </span>
          <span class="frow__size">${item.type === "folder" ? H`${item.childCount ?? 0} 项` : formatBytes(item.size)}</span>
          <span class="frow__time">${formatTime(item.updatedAt)}</span>
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
        if (item) openItem(item);
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

    bodyEl.querySelectorAll("[data-sort]").forEach((btn) => {
      btn.addEventListener("click", () => toggleSort(btn.dataset.sort));
    });
  }

  function renderViewToggle() {
    root.querySelectorAll("[data-view]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.view === state.view);
    });
  }

  function renderSortLabel() {
    const label = root.querySelector("[data-sort-label]");
    const btn = root.querySelector('[data-act="sort"]');
    if (label) label.textContent = t(SORT_LABELS[state.sort] || "名称");
    if (btn) btn.classList.toggle("is-desc", state.order === "desc");
  }

  /* ------------------------------------------------------------------ */
  /* 选择                                                                */
  /* ------------------------------------------------------------------ */
  const findItem = (id) => state.items.find((item) => item.id === id);

  function onItemClick(event, el) {
    const id = Number(el.dataset.id);
    const index = Number(el.dataset.index);
    state.cursor = index;

    if (event.shiftKey && state.selected.size) {
      const anchor = state.items.findIndex((item) => state.selected.has(item.id));
      if (anchor >= 0) {
        const [from, to] = anchor < index ? [anchor, index] : [index, anchor];
        state.selected.clear();
        state.items.slice(from, to + 1).forEach((item) => state.selected.add(item.id));
      }
    } else if (event.ctrlKey || event.metaKey) {
      if (state.selected.has(id)) state.selected.delete(id);
      else state.selected.add(id);
    } else {
      state.selected.clear();
      state.selected.add(id);
    }
    updateSelectionStyles();
  }

  function updateSelectionStyles() {
    bodyEl.querySelectorAll("[data-id]").forEach((el) => {
      const id = Number(el.dataset.id);
      el.classList.toggle("is-selected", state.selected.has(id));
      const box = el.querySelector('input[type="checkbox"]');
      if (box) box.checked = state.selected.has(id);
      el.classList.toggle("is-cursor", Number(el.dataset.index) === state.cursor);
    });
    updateSelbar();
  }

  function updateSelbar() {
    const count = state.selected.size;
    selbar.classList.toggle("is-on", count > 0);
    selCount.textContent = String(count);
  }

  function selectedItems() {
    return state.items.filter((item) => state.selected.has(item.id));
  }

  /* ------------------------------------------------------------------ */
  /* 操作                                                                */
  /* ------------------------------------------------------------------ */
  async function openItem(item) {
    if (item.type === "folder") {
      state.parentId = item.id;
      state.searchMode = false;
      state.keyword = "";
      searchEl.value = "";
      await load();
      return;
    }
    // 文件一律进预览器；不支持的格式由查看器展示「暂不支持此格式」并提供下载
    openViewer(item, { onDownload: downloadItem });
  }

  function downloadItem(item) {
    if (item.type !== "file") {
      Toast.warn("文件夹暂不支持打包下载");
      return;
    }
    const a = document.createElement("a");
    a.href = `/api/nodes/${item.id}/download`;
    a.download = item.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  async function renameItem(item) {
    const value = await Dialog.prompt({
      title: "重命名",
      label: t("新名称"),
      value: item.name,
      confirmText: "保存",
      validate: (text) => {
        const name = text.trim();
        if (!name) return "名称不能为空";
        if (name.length > 255) return "名称过长（上限 255 字符）";
        if (/[\\/:*?"<>|]/.test(name)) return '不能包含 \\ / : * ? " < > |';
        if (name === item.name) return "名称没有变化";
        return null;
      },
    });
    if (!value) return;
    try {
      await api.patch(`/api/nodes/${item.id}`, { name: value.trim() });
      Toast.success("已重命名");
      await load({ keepScroll: true });
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async function moveItems(items) {
    if (!items.length) return;
    const first = items[0];
    const target = await pickFolder({
      title: items.length > 1 ? H`移动 ${items.length} 项到` : "移动到",
      excludeId: first.type === "folder" ? first.id : null,
    });
    if (target === undefined) return; // 取消；null 表示移到根目录
    if (target === state.parentId && !state.searchMode) {
      Toast.info("已经在当前目录了");
      return;
    }

    const result = await runTask({
      title: "正在移动",
      icon: "i-folder",
      items,
      run: (item) => api.post(`/api/nodes/${item.id}/move`, { parentId: target }),
    });

    if (result.failed) {
      Toast.error(`${result.failed} 项移动失败`);
    } else if (result.canceled) {
      Toast.info(`已停止，成功移动 ${result.done} 项`);
    } else {
      Toast.success(items.length > 1 ? H`已移动 ${items.length} 项` : "已移动");
    }
    if (result.done) await load({ keepScroll: true });
  }

  async function deleteItems(items) {
    if (!items.length) return;
    const onlyFolders = items.every((item) => item.type === "folder");
    const trashOn = store.get().user?.settings?.trashEnabled !== false;
    const subject = `「${escapeHtml(items[0].name)}」${
      items.length > 1 ? " 等 " + items.length + " 项" : ""
    }${onlyFolders ? "（含里面的全部内容）" : ""}`;
    const ok = await Dialog.confirm({
      title: items.length > 1 ? H`删除 ${items.length} 项` : "删除文件",
      message: trashOn
        ? H`${subject} 会被移到回收站，可以随时还原。`
        : H`${subject} 会被<b>永久删除</b>，无法恢复（你已关闭回收站）。`,
      confirmText: trashOn ? "移到回收站" : "永久删除",
      danger: true,
    });
    if (!ok) return;
    try {
      for (const item of items) {
        await api.del(`/api/nodes/${item.id}`);
      }
      Toast.success(trashOn ? "已移到回收站" : "已永久删除");
      await load({ keepScroll: true });
      await refreshUser();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  /** 空白处右键：新建文件夹 / 上传 / 刷新 */
  function openBlankMenu(x, y) {
    const where = state.searchMode
      ? t("搜索结果")
      : (state.breadcrumb[state.breadcrumb.length - 1]?.id == null
          ? t(state.breadcrumb[state.breadcrumb.length - 1]?.name || "我的文件")
          : state.breadcrumb[state.breadcrumb.length - 1]?.name) || t("我的文件");
    openMenu({
      x,
      y,
      header: { name: where, meta: state.searchMode ? t("新建会放到根目录") : t("当前目录") },
      items: [
        { label: "新建文件夹", icon: "i-folder", onClick: createFolder },
        { sep: true },
        { label: "上传文件", icon: "i-upload", onClick: () => fileInput.click() },
        { label: "刷新", icon: "i-refresh", onClick: () => load({ keepScroll: true }) },
      ],
    });
  }

  function openItemMenu(x, y, item) {
    const multi = state.selected.size > 1;
    const items = multi
      ? [
          {
            label: H`下载 ${state.selected.size} 项`,
            icon: "i-download",
            onClick: () => selectedItems().filter((s) => s.type === "file").forEach(downloadItem),
          },
          {
            label: H`分享 ${state.selected.size} 项`,
            icon: "i-link",
            onClick: () => shareItems(selectedItems()),
          },
          { label: "移动到…", icon: "i-folder", onClick: () => moveItems(selectedItems()) },
          { sep: true },
          { label: H`删除 ${state.selected.size} 项`, icon: "i-trash", danger: true, onClick: () => deleteItems(selectedItems()) },
        ]
      : [
          ...(item.type === "folder"
            ? [{ label: "打开", icon: "i-folder", onClick: () => openItem(item) }]
            : canPreview(item)
            ? [
                { label: "预览", icon: "i-image", onClick: () => openViewer(item, { onDownload: downloadItem }) },
                { label: "下载", icon: "i-download", onClick: () => downloadItem(item) },
              ]
            : [{ label: "下载", icon: "i-download", onClick: () => downloadItem(item) }]),
          { label: "分享链接", icon: "i-link", onClick: () => shareItem(item) },
          { sep: true },
          { label: "重命名", icon: "i-file", onClick: () => renameItem(item) },
          { label: "移动到…", icon: "i-share", onClick: () => moveItems([item]) },
          { sep: true },
          { label: "删除", icon: "i-trash", danger: true, onClick: () => deleteItems([item]) },
        ];
    openMenu({
      x,
      y,
      header: { name: multi ? H`${state.selected.size} 个项目` : item.name, meta: multi ? "批量操作" : metaOf(item) },
      items,
    });
  }

  async function createFolder() {
    const name = await Dialog.prompt({
      title: "新建文件夹",
      label: t("文件夹名称"),
      placeholder: "例如：2026 秋招资料",
      confirmText: "创建",
      validate: (text) => {
        const value = text.trim();
        if (!value) return t("请填写名称");
        if (value.length > 255) return "名称过长";
        if (/[\\/:*?"<>|]/.test(value)) return '不能包含 \\ / : * ? " < > |';
        return null;
      },
    });
    if (!name) return;
    try {
      await api.post("/api/nodes/folder", { parentId: state.searchMode ? null : state.parentId, name: name.trim() });
      Toast.success("文件夹已创建");
      if (state.searchMode) {
        state.searchMode = false;
        state.keyword = "";
        searchEl.value = "";
      }
      await load({ keepScroll: true });
    } catch (err) {
      Toast.error(err.message);
    }
  }

  /* ------------------------------------------------------------------ */
  /* 上传                                                                */
  /* ------------------------------------------------------------------ */
  /** 上传队列是全局单例（ui/upload-queue.js）：切页、进设置都不影响后台继续传 */
  function uploadFiles(fileList, targetParent = state.parentId) {
    if (state.searchMode) {
      state.searchMode = false;
      state.keyword = "";
      searchEl.value = "";
    }
    enqueueUploadsQueue([...fileList].map((file) => ({ file, parentId: targetParent })));
  }

  function enqueueUploads(entries) {
    if (state.searchMode) {
      state.searchMode = false;
      state.keyword = "";
      searchEl.value = "";
    }
    enqueueUploadsQueue(entries);
  }

  /* ------------------------------------------------------------------ */
  /* 拖入文件 / 文件夹                                                    */
  /* ------------------------------------------------------------------ */
  const DROP_MAX_FILES = 2000;
  const DROP_MAX_DEPTH = 12;
  const DROP_IGNORED = /^(\.DS_Store|Thumbs\.db|desktop\.ini)$/i;

  const entryFile = (entry) => new Promise((resolve, reject) => entry.file(resolve, reject));
  const readDirBatch = (reader) =>
    new Promise((resolve, reject) => reader.readEntries(resolve, reject));

  /** 把 DataTransfer 展开成 [{file, relPath}] 和目录路径列表（空目录也保留） */
  async function collectDropped(dataTransfer) {
    const items = [...(dataTransfer.items || [])];
    const roots = items
      .map((item) => (item.kind === "file" && item.webkitGetAsEntry ? item.webkitGetAsEntry() : null))
      .filter(Boolean);
    const files = [];
    const dirs = [];

    if (!roots.length) {
      return {
        files: [...(dataTransfer.files || [])].map((file) => ({ file, relPath: "" })),
        dirs,
        truncated: false,
      };
    }

    let truncated = false;

    const pushFile = async (entry, prefix) => {
      if (DROP_IGNORED.test(entry.name)) return;
      try {
        files.push({ file: await entryFile(entry), relPath: prefix + entry.name });
      } catch {
        /* 单个文件读不出来就跳过，不影响其它文件 */
      }
    };

    const walk = async (dir, prefix, depth) => {
      if (depth > DROP_MAX_DEPTH) {
        truncated = true;
        return;
      }
      dirs.push(prefix.replace(/\/$/, ""));
      const reader = dir.createReader();
      for (;;) {
        let batch = [];
        try {
          batch = await readDirBatch(reader);
        } catch {
          break;
        }
        if (!batch.length) break;
        for (const child of batch) {
          if (files.length >= DROP_MAX_FILES) {
            truncated = true;
            return;
          }
          if (child.isFile) await pushFile(child, prefix);
          else if (child.isDirectory) await walk(child, `${prefix}${child.name}/`, depth + 1);
        }
      }
    };

    for (const entry of roots) {
      if (entry.isFile) await pushFile(entry, "");
      else if (entry.isDirectory) await walk(entry, `${entry.name}/`, 0);
    }
    return { files, dirs, truncated };
  }

  /** 按相对路径逐级建目录；同名文件夹直接复用，拖第二次不会出现「项目 (1)」 */
  async function ensureDropTree(dirPaths, rootParent) {
    const dirIds = new Map(); // "a/b" -> folderId
    const childCache = new Map(); // parentId -> Map(name -> id)

    if (!state.searchMode) {
      const seeded = new Map();
      for (const item of state.items) if (item.type === "folder") seeded.set(item.name, item.id);
      childCache.set(rootParent, seeded);
    }

    async function childrenOf(parentId) {
      if (!childCache.has(parentId)) {
        const params = new URLSearchParams({ sort: "name", order: "asc" });
        if (parentId !== null && parentId !== undefined) params.set("parentId", String(parentId));
        const data = await api.get(`/api/nodes?${params.toString()}`);
        const map = new Map();
        for (const item of data.items || []) if (item.type === "folder") map.set(item.name, item.id);
        childCache.set(parentId, map);
      }
      return childCache.get(parentId);
    }

    async function ensure(path) {
      if (dirIds.has(path)) return dirIds.get(path);
      let parentId = rootParent;
      let prefix = "";
      for (const part of path.split("/").filter(Boolean)) {
        prefix = prefix ? H`${prefix}/${part}` : part;
        if (dirIds.has(prefix)) {
          parentId = dirIds.get(prefix);
          continue;
        }
        const kids = await childrenOf(parentId);
        let id = kids.get(part);
        if (!id) {
          const res = await api.post("/api/nodes/folder", { parentId, name: part });
          id = res.node.id;
          kids.set(part, id);
        }
        dirIds.set(prefix, id);
        parentId = id;
      }
      return parentId;
    }

    const ordered = [...new Set(dirPaths.filter(Boolean))].sort(
      (a, b) => a.split("/").length - b.split("/").length
    );
    for (const path of ordered) await ensure(path);
    return ensure;
  }

  /** 拖拽入口：平铺文件直接传，带目录的先建结构再按目标目录上传 */
  async function uploadDropped(dataTransfer) {
    let collected;
    try {
      collected = await collectDropped(dataTransfer);
    } catch {
      collected = {
        files: [...(dataTransfer.files || [])].map((file) => ({ file, relPath: "" })),
        dirs: [],
        truncated: false,
      };
    }

    const files = collected.files.filter((item) => item.file && item.file.size > 0);
    if (!files.length && !collected.dirs.length) {
      Toast.warn("没有可上传的文件（空文件会被跳过）");
      return;
    }
    if (collected.truncated) {
      Toast.warn(`一次最多处理 ${DROP_MAX_FILES} 个文件，超出的部分已忽略`);
    }

    const rootParent = state.parentId;
    const nested = files.some((item) => item.relPath.includes("/")) || collected.dirs.length > 0;
    if (!nested) {
      enqueueUploads(files.map((item) => ({ file: item.file, parentId: rootParent })));
      return;
    }

    if (collected.dirs.length) Toast.info(`正在创建 ${collected.dirs.length} 个文件夹…`);
    let ensure;
    try {
      ensure = await ensureDropTree(collected.dirs, rootParent);
    } catch (err) {
      Toast.error(`文件夹创建失败：${err.message || "请稍后重试"}`);
      return;
    }

    const prepared = [];
    for (const item of files) {
      const slash = item.relPath.lastIndexOf("/");
      const dir = slash > 0 ? item.relPath.slice(0, slash) : "";
      prepared.push({ file: item.file, parentId: dir ? await ensure(dir) : rootParent });
    }
    if (!prepared.length) {
      Toast.success(`已创建 ${collected.dirs.length} 个文件夹（空文件未上传）`);
      await load({ keepScroll: true });
      return;
    }
    enqueueUploads(prepared);
  }

  /* ------------------------------------------------------------------ */
  /* 排序 / 视图                                                         */
  /* ------------------------------------------------------------------ */
  function toggleSort(key) {
    if (state.sort === key) {
      state.order = state.order === "asc" ? "desc" : "asc";
    } else {
      state.sort = key;
      state.order = key === "name" ? "asc" : "desc";
    }
    load({ keepScroll: true });
  }

  function openSortMenu(anchor) {
    openMenu({
      anchor,
      align: "end",
      items: Object.entries(SORT_LABELS)
        .filter(([key]) => key !== "created")
        .map(([key, label]) => ({
          label: H`${label}${state.sort === key ? (state.order === "asc" ? " ↑" : " ↓") : ""}`,
          icon: state.sort === key ? "i-check" : undefined,
          onClick: () => toggleSort(key),
        })),
    });
  }

  /* ------------------------------------------------------------------ */
  /* 事件绑定                                                            */
  /* ------------------------------------------------------------------ */
  root.querySelector('[data-act="new-folder"]').addEventListener("click", createFolder);
  root.querySelector('[data-act="refresh"]').addEventListener("click", () => load({ keepScroll: true }));
  root.querySelectorAll('[data-act="upload"]').forEach((btn) =>
    btn.addEventListener("click", () => fileInput.click())
  );
  fileInput.addEventListener("change", () => {
    uploadFiles(fileInput.files);
    fileInput.value = "";
  });

  root.querySelector('[data-act="sort"]').addEventListener("click", (event) =>
    openSortMenu(event.currentTarget)
  );

  root.querySelectorAll("[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.view = btn.dataset.view;
      renderViewToggle();
      render();
      api.patch("/api/me/settings", { viewMode: state.view }).catch(() => {});
      const user = store.get().user;
      if (user?.settings) user.settings.viewMode = state.view;
    });
  });

  root.querySelector('[data-act="sel-clear"]').addEventListener("click", () => {
    state.selected.clear();
    updateSelectionStyles();
  });
  root.querySelector('[data-act="sel-delete"]').addEventListener("click", () => deleteItems(selectedItems()));
  root.querySelector('[data-act="sel-move"]').addEventListener("click", () => moveItems(selectedItems()));
  root.querySelector('[data-act="sel-download"]').addEventListener("click", () =>
    selectedItems().filter((item) => item.type === "file").forEach(downloadItem)
  );
  root.querySelector('[data-act="sel-share"]').addEventListener("click", () => shareItems(selectedItems()));

  // 搜索（防抖）
  let searchTimer = 0;
  searchEl.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      const keyword = searchEl.value.trim();
      if (!keyword) {
        if (state.searchMode) {
          state.searchMode = false;
          state.keyword = "";
          load();
        }
        return;
      }
      state.searchMode = true;
      state.keyword = keyword;
      load();
    }, 260);
  });
  searchEl.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      searchEl.value = "";
      searchEl.dispatchEvent(new Event("input"));
    }
  });

  // 点击空白处取消选择
  bodyEl.addEventListener("mousedown", (event) => {
    if (event.target === bodyEl || event.target.classList.contains("file-grid") || event.target.classList.contains("file-list")) {
      state.selected.clear();
      updateSelectionStyles();
    }
  });

  // 空白处右键：新建文件夹等（点在文件/按钮上时交给各自的菜单）
  bodyEl.addEventListener("contextmenu", (event) => {
    if (event.target.closest("[data-id], [data-more], button, a, input, label")) return;
    event.preventDefault();
    openBlankMenu(event.clientX, event.clientY);
  });

  // 拖拽上传
  let dragDepth = 0;
  const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
  root.addEventListener("dragenter", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    dropMask.classList.add("is-on");
  });
  root.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  });
  root.addEventListener("dragleave", (event) => {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) dropMask.classList.remove("is-on");
  });
  root.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    dropMask.classList.remove("is-on");
    uploadDropped(event.dataTransfer);
  });

  // 键盘
  const onKey = (event) => {
    if (!document.body.contains(root)) {
      document.removeEventListener("keydown", onKey);
      return;
    }
    const tag = document.activeElement?.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA") return;

    if (event.key === "Escape") {
      state.selected.clear();
      updateSelectionStyles();
      return;
    }
    if ((event.key === "Delete" || event.key === "Backspace") && state.selected.size) {
      event.preventDefault();
      deleteItems(selectedItems());
      return;
    }
    if (event.key === "F2" && state.selected.size === 1) {
      event.preventDefault();
      const [item] = selectedItems();
      if (item) renameItem(item);
      return;
    }
    if (event.key === "Backspace" && !state.searchMode && state.parentId !== null) {
      state.parentId = state.breadcrumb[state.breadcrumb.length - 2]?.id ?? null;
      load();
      return;
    }
    if (event.key === "a" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      state.items.forEach((item) => state.selected.add(item.id));
      updateSelectionStyles();
    }
  };
  document.addEventListener("keydown", onKey);

  load();
}
