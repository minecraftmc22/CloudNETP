/**
 * 管理后台：概览 / 用户管理 / 审计日志 / 系统设置
 *
 * 只有 role === 1 的用户能进入（路由层已拦截，后端也会二次校验）。
 */
import { api, escapeHtml, formatBytes, formatTime, percent } from "../api.js";
import { store } from "../store.js";
import { Dialog } from "../ui/dialog.js";
import { openMenu } from "../ui/menu.js";
import { copyText } from "../ui/share.js";
import { Toast } from "../ui/toast.js";

const TABS = [
  { id: "overview", label: "概览", icon: "i-chart" },
  { id: "users", label: "用户", icon: "i-user" },
  { id: "reports", label: "举报审核", icon: "i-alert" },
  { id: "logs", label: "审计日志", icon: "i-clock" },
  { id: "settings", label: "系统设置", icon: "i-settings" },
];

const PAGE_SIZE = 20;
const LOG_PAGE_SIZE = 30;
const GB = 1024 ** 3;
const MB = 1024 ** 2;

const loading = () => `<div class="admin-loading"><span class="spinner"></span></div>`;

const errorBlock = (msg) => `
  <div class="empty">
    <div class="empty__art"><svg class="icon"><use href="#i-alert"/></svg></div>
    <div class="empty__title">加载失败</div>
    <div class="empty__desc">${escapeHtml(msg)}</div>
  </div>`;

function statCard(icon, label, value, sub) {
  return `
    <div class="stat-card">
      <div class="stat-card__label"><svg class="icon"><use href="#${icon}"/></svg>${label}</div>
      <div class="stat-card__value">${value}</div>
      <div class="stat-card__sub">${sub || ""}</div>
    </div>`;
}

/** 数据库状态文案：dbMode=fallback 表示主库不可用，本地 SQLite 接管中 */
function dbLabel(database, dbMode) {
  if (dbMode === "fallback") return database === "mysql" ? "MySQL·降级中(SQLite接管)" : "SQLite(容灾)";
  return database === "mysql" ? "MySQL" : "SQLite";
}

function pagerHtml(page, total, pageSize) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  return `
    <button type="button" data-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button>
    <span>第 ${page} / ${pages} 页 · 共 ${total} 条</span>
    <button type="button" data-page="${page + 1}" ${page >= pages ? "disabled" : ""}>下一页</button>`;
}

/** 把审计 detail 压成一行摘要 */
function detailLine(raw) {
  if (!raw) return "";
  try {
    const data = JSON.parse(raw);
    if (data && typeof data === "object" && !Array.isArray(data)) {
      return Object.entries(data)
        .slice(0, 4)
        .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)
        .join(" · ");
    }
    return String(data);
  } catch {
    return String(raw);
  }
}

export function renderAdmin(root) {
  const state = {
    tab: "overview",
    actions: null,
    users: { page: 1, query: "", filter: "all", total: 0 },
    reports: { page: 1, filter: "pending", total: 0 },
    logs: { page: 1, query: "", action: "", userId: null, username: "", total: 0 },
  };

  root.innerHTML = `
    <div class="view-head">
      <div class="view-title"><svg class="icon"><use href="#i-shield"/></svg>管理后台</div>
      <span class="view-sub" data-sub>站点运行状态</span>
      <div class="grow"></div>
      <button class="icon-btn" type="button" data-act="refresh" title="刷新当前页">
        <svg class="icon"><use href="#i-refresh"/></svg>
      </button>
    </div>
    <div class="admin-tabs">
      <div class="segmented" data-tabs>
        <div class="segmented__thumb" data-thumb></div>
        ${TABS.map(
          (tab) =>
            `<button class="segmented__btn ${tab.id === "overview" ? "is-active" : ""}" type="button" data-tab="${tab.id}">
               <svg class="icon"><use href="#${tab.icon}"/></svg>${tab.label}
             </button>`
        ).join("")}
      </div>
    </div>
    <div class="view-body admin-body" data-panel></div>`;

  const panel = root.querySelector("[data-panel]");
  const subEl = root.querySelector("[data-sub]");
  const seg = root.querySelector("[data-tabs]");
  const thumb = root.querySelector("[data-thumb]");
  const segBtns = [...seg.querySelectorAll("[data-tab]")];

  function layoutThumb() {
    const active = segBtns.find((b) => b.classList.contains("is-active"));
    if (!active) return;
    thumb.style.width = `${active.offsetWidth}px`;
    thumb.style.transform = `translateX(${active.offsetLeft - 3}px)`;
  }

  function setSub(text) {
    subEl.textContent = text;
  }

  /* ---------------------------------------------------------------- */
  /* 概览                                                              */
  /* ---------------------------------------------------------------- */
  async function showOverview() {
    panel.innerHTML = loading();
    let data;
    try {
      data = await api.get("/api/admin/overview");
    } catch (err) {
      panel.innerHTML = errorBlock(err.message);
      return;
    }
    const { users: u, nodes: n, shares: s, storage: st, system } = data;
    const quotaPct = percent(u.usedBytes, u.quotaBytes || 1);
    const diskPct = percent(st.diskUsed, st.diskTotal || 1);
    setSub(`${system.appName} v${system.version} · ${dbLabel(system.database, system.dbMode)}`);

    panel.innerHTML = `
      <div class="stat-grid">
        ${statCard(
          "i-user",
          "用户",
          `${u.total}`,
          `今日活跃 ${u.activeToday} · 本周新增 ${u.newThisWeek}${u.banned ? ` · 封禁 ${u.banned}` : ""}`
        )}
        ${statCard(
          "i-folder",
          "文件与目录",
          `${n.files}`,
          `文件夹 ${n.folders} · 回收站 ${n.trashItems} 项（${formatBytes(n.trashBytes)}）`
        )}
        ${statCard(
          "i-cloud",
          "逻辑占用",
          formatBytes(u.usedBytes),
          `配额合计 ${formatBytes(u.quotaBytes)} · 占比 ${quotaPct}%`
        )}
        ${statCard(
          "i-database",
          "物理文件",
          `${st.blobs}`,
          `实际落盘 ${formatBytes(st.blobBytes)} · 去重后节省 ${formatBytes(Math.max(0, u.usedBytes - st.blobBytes))}`
        )}
      </div>

      <section class="settings-card">
        <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-cloud"/></svg>存储</div>
        <div class="admin-card__pad">
          <div class="row" style="justify-content:space-between">
            <span class="muted">用户逻辑占用 ${formatBytes(u.usedBytes)} / ${formatBytes(u.quotaBytes)}</span>
            <span class="muted">${quotaPct}%</span>
          </div>
          <div class="progress" style="margin:8px 0 4px;height:6px">
            <div class="progress__bar" style="width:${quotaPct}%"></div>
          </div>
          <div class="admin-facts">
            <div><span>磁盘</span><b>${formatBytes(st.diskUsed)} / ${formatBytes(st.diskTotal)}</b><small>${diskPct}%</small></div>
            <div><span>分享链接</span><b>${s.active} 个有效</b><small>共 ${s.total} 个</small></div>
            <div><span>存储目录</span><b class="mono">${escapeHtml(st.path)}</b></div>
            <div><span>回收站保留</span><b>${system.trashRetentionDays} 天</b></div>
          </div>
        </div>
      </section>

      <div class="admin-split">
        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-clock"/></svg>最近操作</div>
          <div class="admin-feed">
            ${
              data.recentLogs.length
                ? data.recentLogs
                    .map(
                      (log) => `
              <div class="admin-feed__row">
                <span class="admin-feed__dot ${log.tone === "info" ? "" : `is-${log.tone}`}"></span>
                <span class="admin-feed__main"><b>${escapeHtml(log.username || "系统")}</b> · ${escapeHtml(
                        log.label
                      )} ${escapeHtml(detailLine(log.detail))}</span>
                <span class="admin-feed__time">${formatTime(log.createdAt)}</span>
              </div>`
                    )
                    .join("")
                : `<div class="admin-feed__row"><span class="admin-feed__main muted">还没有审计记录</span></div>`
            }
          </div>
        </section>

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-star"/></svg>占用排行</div>
          <div class="admin-rank">
            ${
              data.topUsers.length
                ? data.topUsers
                    .map(
                      (item, index) => `
              <div class="admin-rank__row">
                <span class="admin-rank__idx">${index + 1}</span>
                <span class="admin-rank__name">${escapeHtml(item.username)}</span>
                <span class="admin-rank__num">${formatBytes(item.usedBytes)} / ${formatBytes(item.quotaBytes)}</span>
              </div>`
                    )
                    .join("")
                : `<div class="admin-rank__row"><span></span><span class="muted">暂无数据</span><span></span></div>`
            }
          </div>
        </section>
      </div>`;
  }

  /* ---------------------------------------------------------------- */
  /* 用户                                                              */
  /* ---------------------------------------------------------------- */
  const USER_FILTERS = [
    { id: "all", label: "全部" },
    { id: "active", label: "正常" },
    { id: "banned", label: "已封禁" },
    { id: "admin", label: "管理员" },
  ];

  async function showUsers() {
    panel.innerHTML = `
      <div class="admin-toolbar">
        <span class="searchbox">
          <span class="textbox">
            <svg class="icon icon--s"><use href="#i-search"/></svg>
            <input type="search" data-search placeholder="搜索用户名 / 用户 ID" autocomplete="off">
          </span>
        </span>
        <div class="admin-chips">
          ${USER_FILTERS.map(
            (f) =>
              `<button class="admin-chip ${state.users.filter === f.id ? "is-active" : ""}" type="button" data-filter="${f.id}">${f.label}</button>`
          ).join("")}
        </div>
        <div class="grow"></div>
        <span class="admin-count" data-count></span>
      </div>
      <div data-table>${loading()}</div>
      <div class="admin-pager" data-pager></div>`;

    const tableEl = panel.querySelector("[data-table]");
    const pagerEl = panel.querySelector("[data-pager]");
    const countEl = panel.querySelector("[data-count]");
    const searchEl = panel.querySelector("[data-search]");
    searchEl.value = state.users.query;

    let timer = 0;
    searchEl.addEventListener("input", () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        state.users.query = searchEl.value.trim();
        state.users.page = 1;
        loadUsers();
      }, 280);
    });

    panel.querySelectorAll("[data-filter]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.users.filter = btn.dataset.filter;
        state.users.page = 1;
        panel.querySelectorAll("[data-filter]").forEach((b) => b.classList.toggle("is-active", b === btn));
        loadUsers();
      });
    });

    async function loadUsers() {
      tableEl.innerHTML = loading();
      const params = new URLSearchParams({
        page: String(state.users.page),
        pageSize: String(PAGE_SIZE),
        state: state.users.filter,
      });
      if (state.users.query) params.set("query", state.users.query);

      let data;
      try {
        data = await api.get(`/api/admin/users?${params}`);
      } catch (err) {
        tableEl.innerHTML = errorBlock(err.message);
        pagerEl.innerHTML = "";
        return;
      }
      state.users.total = data.total;
      countEl.textContent = `${data.total} 个账号`;
      setSub(`${data.total} 个账号 · ${state.users.filter === "all" ? "全部状态" : USER_FILTERS.find((f) => f.id === state.users.filter)?.label}`);

      if (!data.items.length) {
        tableEl.innerHTML = `
          <div class="empty">
            <div class="empty__art"><svg class="icon"><use href="#i-user"/></svg></div>
            <div class="empty__title">没有匹配的用户</div>
            <div class="empty__desc">换个关键词，或把筛选切回「全部」。</div>
          </div>`;
        pagerEl.innerHTML = "";
        return;
      }

      tableEl.innerHTML = `
        <div class="admin-table">
          <div class="admin-row admin-row--head">
            <span>用户</span><span>空间</span><span>文件</span><span>最近登录</span><span>状态</span><span></span>
          </div>
          ${data.items.map(userRowHtml).join("")}
        </div>`;

      tableEl.querySelectorAll(".admin-row[data-id]").forEach((row) => {
        const user = data.items.find((item) => item.id === Number(row.dataset.id));
        if (!user) return;
        row.querySelector("[data-menu]")?.addEventListener("click", (event) => {
          event.stopPropagation();
          openUserMenu(event.currentTarget, user, loadUsers);
        });
        row.addEventListener("dblclick", () => openUserLogs(user));
      });

      pagerEl.innerHTML = pagerHtml(state.users.page, data.total, PAGE_SIZE);
      pagerEl.querySelectorAll("[data-page]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const page = Number(btn.dataset.page);
          if (!page || page === state.users.page) return;
          state.users.page = page;
          loadUsers();
        });
      });
    }

    loadUsers();
  }

  function userRowHtml(user) {
    const pct = percent(user.usedBytes, user.quotaBytes || 1);
    const roleChip = user.role === 1 ? '<span class="admin-badge is-ok">管理员</span>' : "";
    const status = user.banned
      ? '<span class="admin-badge is-banned">已封禁</span>'
      : '<span class="admin-badge is-ok">正常</span>';
    return `
      <div class="admin-row" data-id="${user.id}">
        <div class="admin-user">
          <span class="admin-avatar">${escapeHtml((user.username || "?").slice(0, 1).toUpperCase())}</span>
          <div style="min-width:0">
            <div class="admin-user__name">${escapeHtml(user.username)} ${roleChip}</div>
            <div class="admin-user__meta">#${user.id} · ${user.createdAt ? `注册于 ${formatTime(user.createdAt)}` : "—"}</div>
          </div>
        </div>
        <div class="admin-space">
          <div class="admin-space__num">${formatBytes(user.usedBytes)} / ${formatBytes(user.quotaBytes)}</div>
          <div class="progress"><div class="progress__bar" style="width:${pct}%"></div></div>
        </div>
        <div class="admin-cell">${user.fileCount} 个${user.trashCount ? ` · 回收站 ${user.trashCount}` : ""}</div>
        <div class="admin-cell">${formatTime(user.lastLoginAt)}</div>
        <div>${status}</div>
        <div class="admin-actions" style="text-align:right">
          <button class="icon-btn" type="button" data-menu title="更多操作">
            <svg class="icon"><use href="#i-menu"/></svg>
          </button>
        </div>
      </div>`;
  }

  function openUserLogs(user) {
    state.logs = { page: 1, query: "", action: "", userId: user.id, username: user.username, total: 0 };
    switchTab("logs");
  }

  function openUserMenu(anchor, user, reload) {
    const self = store.get().user?.id === user.id;
    openMenu({
      anchor,
      align: "end",
      header: {
        name: user.username,
        meta: `#${user.id} · ${user.role === 1 ? "管理员" : "普通用户"} · ${formatBytes(user.usedBytes)}`,
      },
      items: [
        { label: "查看审计日志", icon: "i-clock", onClick: () => openUserLogs(user) },
        { label: "调整配额", icon: "i-cloud", onClick: () => changeQuota(user, reload) },
        { label: "重置密码", icon: "i-key", onClick: () => resetPassword(user) },
        { sep: true },
        {
          label: user.role === 1 ? "取消管理员" : "设为管理员",
          icon: "i-shield",
          disabled: self,
          onClick: () => setRole(user, reload),
        },
        {
          label: user.banned ? "解封账号" : "封禁账号",
          icon: "i-alert",
          danger: !user.banned,
          disabled: self,
          onClick: () => setBanned(user, reload),
        },
        { sep: true },
        {
          label: "删除用户",
          icon: "i-trash",
          danger: true,
          disabled: self,
          onClick: () => deleteUser(user, reload),
        },
      ],
    });
  }

  async function changeQuota(user, reload) {
    const current = user.quotaBytes / GB;
    const result = await Dialog.form({
      title: `调整配额 · ${user.username}`,
      message: `当前已用 ${formatBytes(user.usedBytes)}。配额低于已用空间时，该用户将无法继续上传。`,
      confirmText: "保存",
      fields: [
        {
          name: "gb",
          label: "配额（GB）",
          type: "number",
          value: String(Math.round(current * 100) / 100),
          validate: (value) => {
            const num = Number(value);
            if (!value.trim() || !Number.isFinite(num) || num <= 0) return "请输入大于 0 的数字";
            if (num > 102400) return "配额不能超过 102400 GB";
            return null;
          },
        },
      ],
    });
    if (!result) return;
    try {
      await api.patch(`/api/admin/users/${user.id}`, {
        quotaBytes: Math.round(Number(result.gb) * GB),
      });
      Toast.success(`已将 ${user.username} 的配额调整为 ${result.gb} GB`);
      reload();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async function resetPassword(user) {
    const ok = await Dialog.confirm({
      title: "重置密码",
      message: `将为 <b>${escapeHtml(user.username)}</b> 生成一个新的随机密码，该用户所有已登录设备会立即失效。`,
      confirmText: "生成新密码",
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await api.post(`/api/admin/users/${user.id}/password`);
      const copy = await Dialog.confirm({
        title: "新密码（仅显示这一次）",
        message: `请通过安全渠道转交给 <b>${escapeHtml(res.username)}</b>，关闭后无法再次查看：
          <div class="code-display">${escapeHtml(res.password)}</div>
          <div class="dialog__hint">数据库里仍只保存哈希，忘记只能重新生成</div>`,
        confirmText: "复制密码",
        cancelText: "关闭",
      });
      if (copy) {
        const done = await copyText(res.password);
        done ? Toast.success("密码已复制") : Toast.error("复制失败，请手动选中");
      }
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async function setRole(user, reload) {
    const next = user.role === 1 ? 0 : 1;
    const ok = await Dialog.confirm({
      title: next === 1 ? "设为管理员" : "取消管理员",
      message:
        next === 1
          ? `<b>${escapeHtml(user.username)}</b> 将获得管理后台权限，可以查看所有用户与审计日志、修改系统设置。`
          : `<b>${escapeHtml(user.username)}</b> 将失去管理后台权限，普通功能不受影响。`,
      confirmText: next === 1 ? "设为管理员" : "取消权限",
      danger: next === 0,
    });
    if (!ok) return;
    try {
      await api.patch(`/api/admin/users/${user.id}`, { role: next });
      Toast.success(next === 1 ? "已设为管理员" : "已取消管理员");
      reload();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async function setBanned(user, reload) {
    const ban = !user.banned;
    const ok = await Dialog.confirm({
      title: ban ? "封禁账号" : "解封账号",
      message: ban
        ? `封禁后 <b>${escapeHtml(user.username)}</b> 会立即被踢下线，且无法再登录。文件不会被删除，解封后恢复。`
        : `<b>${escapeHtml(user.username)}</b> 将恢复登录权限。`,
      confirmText: ban ? "封禁" : "解封",
      danger: ban,
    });
    if (!ok) return;
    try {
      await api.patch(`/api/admin/users/${user.id}`, { banned: ban });
      Toast.success(ban ? "账号已封禁" : "账号已解封");
      reload();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async function deleteUser(user, reload) {
    const ok = await Dialog.confirm({
      title: `删除用户 · ${user.username}`,
      message: `将永久删除该账号及其 <b>${user.fileCount}</b> 个文件（${formatBytes(user.usedBytes)}）、
        ${user.shareCount} 个分享链接、${user.trashCount} 个回收站条目。此操作<b>不可恢复</b>，
        审计日志会保留。`,
      confirmText: "永久删除",
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await api.del(`/api/admin/users/${user.id}`);
      Toast.success(`已删除 ${user.username}（清理 ${res.nodes} 个节点）`);
      reload();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  /* ---------------------------------------------------------------- */
  /* 举报审核                                                          */
  /* ---------------------------------------------------------------- */
  const REPORT_FILTERS = [
    { id: "pending", label: "待处理" },
    { id: "approved", label: "已通过" },
    { id: "rejected", label: "已驳回" },
    { id: "all", label: "全部" },
  ];

  const REPORT_STATUS_BADGE = {
    pending: '<span class="admin-badge is-warn">待处理</span>',
    approved: '<span class="admin-badge is-banned">已拉黑</span>',
    rejected: '<span class="admin-badge is-ok">已驳回</span>',
  };

  async function showReports() {
    panel.innerHTML = `
      <div class="admin-toolbar">
        <div class="admin-chips">
          ${REPORT_FILTERS.map(
            (f) =>
              `<button class="admin-chip ${state.reports.filter === f.id ? "is-active" : ""}" type="button" data-filter="${f.id}">${f.label}</button>`
          ).join("")}
        </div>
        <div class="grow"></div>
        <span class="admin-count" data-count></span>
      </div>
      <div data-table>${loading()}</div>
      <div class="admin-pager" data-pager></div>`;

    const tableEl = panel.querySelector("[data-table]");
    const pagerEl = panel.querySelector("[data-pager]");
    const countEl = panel.querySelector("[data-count]");

    panel.querySelectorAll("[data-filter]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.reports.filter = btn.dataset.filter;
        state.reports.page = 1;
        panel.querySelectorAll("[data-filter]").forEach((b) => b.classList.toggle("is-active", b === btn));
        loadReports();
      });
    });

    async function loadReports() {
      tableEl.innerHTML = loading();
      const params = new URLSearchParams({
        page: String(state.reports.page),
        pageSize: String(PAGE_SIZE),
        status: state.reports.filter,
      });
      let data;
      try {
        data = await api.get(`/api/admin/reports?${params}`);
      } catch (err) {
        tableEl.innerHTML = errorBlock(err.message);
        pagerEl.innerHTML = "";
        return;
      }
      state.reports.total = data.total;
      countEl.textContent = `${data.total} 条举报`;
      setSub(`${data.total} 条举报 · ${
        REPORT_FILTERS.find((f) => f.id === state.reports.filter)?.label || "全部"
      }`);

      if (!data.items.length) {
        tableEl.innerHTML = `
          <div class="empty">
            <div class="empty__art"><svg class="icon"><use href="#i-alert"/></svg></div>
            <div class="empty__title">没有举报记录</div>
            <div class="empty__desc">分享页被举报后会出现在这里等待审核。</div>
          </div>`;
        pagerEl.innerHTML = "";
        return;
      }

      tableEl.innerHTML = `
        <div class="admin-table">
          <div class="admin-row admin-row--head admin-row--report">
            <span>被举报文件</span><span>哈希值</span><span>举报理由 / 举报人</span><span>时间</span><span>状态</span>
          </div>
          ${data.items.map(reportRowHtml).join("")}
        </div>`;

      tableEl.querySelectorAll(".admin-row[data-id]").forEach((row) => {
        const report = data.items.find((item) => item.id === Number(row.dataset.id));
        if (!report) return;
        row.querySelector("[data-copy-hash]")?.addEventListener("click", async (event) => {
          event.stopPropagation();
          const done = await copyText(report.blobSha256 || "");
          done ? Toast.success("哈希已复制") : Toast.error("复制失败，请手动选中");
        });
        row.querySelector("[data-approve]")?.addEventListener("click", (event) => {
          event.stopPropagation();
          approveReport(report, loadReports);
        });
        row.querySelector("[data-reject]")?.addEventListener("click", (event) => {
          event.stopPropagation();
          rejectReport(report, loadReports);
        });
        row.querySelector("[data-unblock]")?.addEventListener("click", (event) => {
          event.stopPropagation();
          unblockHash(report, loadReports);
        });
        row.addEventListener("click", () => showReportDetail(report));
      });

      pagerEl.innerHTML = pagerHtml(state.reports.page, data.total, PAGE_SIZE);
      pagerEl.querySelectorAll("[data-page]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const page = Number(btn.dataset.page);
          if (!page || page === state.reports.page) return;
          state.reports.page = page;
          loadReports();
        });
      });
    }

    loadReports();
  }

  function reportRowHtml(report) {
    const hash = report.blobSha256
      ? `<div class="report-hash" data-copy-hash title="点击复制完整哈希">${escapeHtml(
          report.blobSha256.slice(0, 16)
        )}…${escapeHtml(report.blobSha256.slice(-8))}</div>
         <div class="admin-user__meta">${report.refCount} 处引用 · ${formatBytes(report.size)}</div>`
      : `<div class="admin-user__meta">文件夹分享 · 共 ${formatBytes(report.size)}</div>`;
    const reason = report.reason
      ? `<div class="report-reason" title="${escapeHtml(report.reason)}">${escapeHtml(report.reason)}</div>`
      : `<div class="report-reason muted">未填写理由</div>`;
    const reporter = `${escapeHtml(report.reporter || "匿名")}${
      report.reporterIp ? ` · ${escapeHtml(report.reporterIp)}` : ""
    }`;
    const shareState = report.shareActive
      ? '<span class="admin-badge is-warn">链接仍有效</span>'
      : '<span class="admin-badge">链接已失效</span>';
    const actions = [];
    if (report.status === "pending") {
      actions.push(
        `<button class="btn btn--danger btn--sm" type="button" data-approve>通过并拉黑</button>`,
        `<button class="btn btn--sm" type="button" data-reject>驳回</button>`
      );
    } else if (report.status === "approved" && report.blocked && report.blobSha256) {
      actions.push(`<button class="btn btn--sm" type="button" data-unblock>解除拉黑</button>`);
    }
    return `
      <div class="admin-row admin-row--report" data-id="${report.id}">
        <div class="admin-user">
          <span class="admin-avatar">${report.isFolder ? "📁" : escapeHtml((report.firstName || "?").slice(0, 1).toUpperCase())}</span>
          <div style="min-width:0">
            <div class="admin-user__name" title="${escapeHtml(report.firstName)}">${escapeHtml(report.firstName)}</div>
            <div class="admin-user__meta">${shareState}</div>
          </div>
        </div>
        <div>${hash}</div>
        <div style="min-width:0">
          ${reason}
          <div class="admin-user__meta">${reporter}</div>
        </div>
        <div class="admin-cell">${formatTime(report.createdAt)}</div>
        <div class="report-actions">
          ${REPORT_STATUS_BADGE[report.status] || ""}
          ${actions.join("")}
        </div>
      </div>`;
  }

  async function approveReport(report, reload) {
    const detail = report.isFolder
      ? `审核通过后该分享链接会立即失效（文件夹分享不涉及文件哈希）。`
      : `审核通过后：
        <div class="dialog__hint">· 哈希 <span class="code-display" style="display:inline;padding:2px 8px">${escapeHtml(
          report.blobSha256 || ""
        )}</span> 会被拉黑</div>
        <div class="dialog__hint">· 该文件的所有分享链接立即失效，且不能再被分享、转存</div>
        <div class="dialog__hint">· 文件本体不删除，分享者自己的网盘仍可查看和下载</div>`;
    const ok = await Dialog.confirm({
      title: "确认举报通过？",
      message: `「<b>${escapeHtml(report.firstName)}</b>」${detail}`,
      confirmText: "通过举报并拉黑",
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await api.post(`/api/admin/reports/${report.id}/approve`);
      Toast.success(res.blocked ? "已拉黑该文件哈希" : "已通过，分享已失效");
      reload();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async function rejectReport(report, reload) {
    const ok = await Dialog.confirm({
      title: "驳回举报",
      message: `将把「<b>${escapeHtml(report.firstName)}</b>」的举报标记为已驳回，分享不受影响。`,
      confirmText: "驳回举报",
    });
    if (!ok) return;
    try {
      await api.post(`/api/admin/reports/${report.id}/reject`);
      Toast.success("已驳回该举报");
      reload();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async function unblockHash(report, reload) {
    const ok = await Dialog.confirm({
      title: "解除拉黑",
      message: `解除后哈希 <b>${escapeHtml(report.blobSha256 || "")}</b> 对应的文件可以重新被分享。`,
      confirmText: "解除拉黑",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.post(`/api/admin/blacklist/${report.blobSha256}/unblock`);
      Toast.success("已解除拉黑");
      reload();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  function showReportDetail(report) {
    Dialog.alert({
      title: `举报详情 · ${report.firstName}`,
      message: `
        <div class="dialog__hint">分享链接 /s/${escapeHtml(report.token)}（${report.shareActive ? "仍有效" : "已失效"}）</div>
        <div class="dialog__hint">文件名：${escapeHtml(report.firstName)}</div>
        ${
          report.blobSha256
            ? `<div class="dialog__hint">哈希值：<span class="code-display" style="display:inline;padding:2px 8px">${escapeHtml(report.blobSha256)}</span></div>`
            : '<div class="dialog__hint">类型：文件夹分享</div>'
        }
        <div class="dialog__hint">大小：${formatBytes(report.size)} · 引用：${report.refCount} 处</div>
        <div class="dialog__hint">举报理由：${escapeHtml(report.reason || "未填写")}</div>
        <div class="dialog__hint">举报人：${escapeHtml(report.reporter || "匿名")}${report.reporterIp ? ` · IP ${escapeHtml(report.reporterIp)}` : ""}</div>
        <div class="dialog__hint">时间：${formatTime(report.createdAt)}${report.handledAt ? ` · 处理：${escapeHtml(report.handledBy || "—")} ${formatTime(report.handledAt)}` : ""}</div>`,
      confirmText: "关闭",
    });
  }

  /* ---------------------------------------------------------------- */
  /* 审计日志                                                          */
  /* ---------------------------------------------------------------- */
  async function ensureActions() {
    if (state.actions) return state.actions;
    try {
      const data = await api.get("/api/admin/logs/actions");
      state.actions = data.items || [];
    } catch {
      state.actions = [];
    }
    return state.actions;
  }

  async function showLogs() {
    const actions = await ensureActions();
    panel.innerHTML = `
      <div class="admin-toolbar">
        <span class="selectbox">
          <select data-action>
            <option value="">全部操作</option>
            ${actions
              .map(
                (item) =>
                  `<option value="${escapeHtml(item.action)}" ${
                    state.logs.action === item.action ? "selected" : ""
                  }>${escapeHtml(item.label)}（${item.count}）</option>`
              )
              .join("")}
          </select>
          <svg class="icon icon--s"><use href="#i-chevron-down"/></svg>
        </span>
        <span class="searchbox">
          <span class="textbox">
            <svg class="icon icon--s"><use href="#i-search"/></svg>
            <input type="search" data-search placeholder="搜索用户 / 详情 / 操作名" value="${escapeHtml(state.logs.query)}">
          </span>
        </span>
        <span class="admin-filter-note" data-note ${state.logs.userId ? "" : "hidden"}>
          只看 #${state.logs.userId ?? ""} ${escapeHtml(state.logs.username || "")}
          <button type="button" data-clear-note title="清除用户筛选">
            <svg class="icon"><use href="#i-close"/></svg>
          </button>
        </span>
        <div class="grow"></div>
        <span class="admin-count" data-count></span>
      </div>
      <div data-list>${loading()}</div>
      <div class="admin-pager" data-pager></div>`;

    const listEl = panel.querySelector("[data-list]");
    const pagerEl = panel.querySelector("[data-pager]");
    const countEl = panel.querySelector("[data-count]");
    const searchEl = panel.querySelector("[data-search]");
    const actionEl = panel.querySelector("[data-action]");

    let timer = 0;
    searchEl.addEventListener("input", () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        state.logs.query = searchEl.value.trim();
        state.logs.page = 1;
        loadLogs();
      }, 280);
    });

    actionEl.addEventListener("change", () => {
      state.logs.action = actionEl.value;
      state.logs.page = 1;
      loadLogs();
    });

    panel.querySelector("[data-clear-note]")?.addEventListener("click", () => {
      state.logs.userId = null;
      state.logs.username = "";
      state.logs.page = 1;
      panel.querySelector("[data-note]").hidden = true;
      loadLogs();
    });

    async function loadLogs() {
      listEl.innerHTML = loading();
      const params = new URLSearchParams({
        page: String(state.logs.page),
        pageSize: String(LOG_PAGE_SIZE),
      });
      if (state.logs.action) params.set("action", state.logs.action);
      if (state.logs.query) params.set("query", state.logs.query);
      if (state.logs.userId) params.set("userId", String(state.logs.userId));

      let data;
      try {
        data = await api.get(`/api/admin/logs?${params}`);
      } catch (err) {
        listEl.innerHTML = errorBlock(err.message);
        pagerEl.innerHTML = "";
        return;
      }
      countEl.textContent = `${data.total} 条记录`;
      setSub(`${data.total} 条审计记录`);

      if (!data.items.length) {
        listEl.innerHTML = `
          <div class="empty">
            <div class="empty__art"><svg class="icon"><use href="#i-clock"/></svg></div>
            <div class="empty__title">没有匹配的记录</div>
            <div class="empty__desc">调整筛选条件，或换个关键词试试。</div>
          </div>`;
        pagerEl.innerHTML = "";
        return;
      }

      listEl.innerHTML = `
        <div class="log-list">
          ${data.items
            .map(
              (log) => `
            <div class="log-row" data-id="${log.id}">
              <span class="log-dot ${log.tone === "info" ? "" : `is-${log.tone}`}"></span>
              <div class="log-main">
                <div class="log-title">
                  ${escapeHtml(log.label)}
                  <span class="log-title__who">${escapeHtml(log.username || "系统")}${log.userId ? ` #${log.userId}` : ""}</span>
                </div>
                <div class="log-detail">${escapeHtml(detailLine(log.detail) || "—")}</div>
              </div>
              <span class="log-ip">${escapeHtml(log.ip || "—")}</span>
              <span class="log-time" title="${escapeHtml(log.createdAt || "")}">${formatTime(log.createdAt)}</span>
            </div>`
            )
            .join("")}
        </div>`;

      listEl.querySelectorAll(".log-row").forEach((row) => {
        const log = data.items.find((item) => item.id === Number(row.dataset.id));
        if (log) row.addEventListener("click", () => showLogDetail(log));
      });

      pagerEl.innerHTML = pagerHtml(state.logs.page, data.total, LOG_PAGE_SIZE);
      pagerEl.querySelectorAll("[data-page]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const page = Number(btn.dataset.page);
          if (!page || page === state.logs.page) return;
          state.logs.page = page;
          loadLogs();
        });
      });
    }

    loadLogs();
  }

  function showLogDetail(log) {
    let detail = "";
    if (log.detail) {
      try {
        detail = `<pre class="admin-json">${escapeHtml(JSON.stringify(JSON.parse(log.detail), null, 2))}</pre>`;
      } catch {
        detail = `<pre class="admin-json">${escapeHtml(log.detail)}</pre>`;
      }
    }
    Dialog.alert({
      title: `${log.label} · ${log.username || "系统"}`,
      message: `
        <div class="dialog__hint">#${log.id} · ${escapeHtml(log.createdAt || "")} · IP ${escapeHtml(log.ip || "—")}</div>
        ${detail || '<div class="dialog__hint" style="margin-top:8px">这条记录没有附加信息</div>'}`,
      confirmText: "关闭",
    });
  }

  /* ---------------------------------------------------------------- */
  /* 系统设置                                                          */
  /* ---------------------------------------------------------------- */
  async function showSettings() {
    panel.innerHTML = loading();
    let settings;
    try {
      const data = await api.get("/api/admin/settings");
      settings = data.settings;
    } catch (err) {
      panel.innerHTML = errorBlock(err.message);
      return;
    }
    setSub("站点信息、上传限制与维护");

    panel.innerHTML = `
      <section class="settings-card" data-card="site">
        <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-cloud"/></svg>站点</div>
        <div class="admin-form-grid">
          <label class="field">
            <span class="field__label">站点名称</span>
            <span class="textbox"><input data-key="siteName" maxlength="64" value="${escapeHtml(settings.siteName)}"></span>
          </label>
          <label class="field">
            <span class="field__label">公告</span>
            <span class="textbox"><input data-key="siteAnnouncement" maxlength="500" placeholder="留空表示不显示" value="${escapeHtml(settings.siteAnnouncement)}"></span>
          </label>
        </div>
        <div class="setting-row">
          <div class="setting-row__main">
            <div class="setting-row__label">开放注册</div>
            <div class="setting-row__desc">关闭后只有管理员能创建账号，已登录用户不受影响</div>
          </div>
          <label class="switch">
            <input type="checkbox" data-key="allowRegister" ${settings.allowRegister ? "checked" : ""}>
            <span class="switch__track"><span class="switch__knob"></span></span>
          </label>
        </div>
        <div class="admin-card__foot">
          <button class="btn btn--accent" type="button" data-save="site">保存站点设置</button>
        </div>
      </section>

      <section class="settings-card" data-card="contact">
        <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-mail"/></svg>找回密码兜底联系</div>
        <div class="setting-row">
          <div class="setting-row__main">
            <div class="setting-row__label">什么时候会用到</div>
            <div class="setting-row__desc">用户既没绑邮箱、邮箱又收不到信时，登录页的「忘记密码」会把这两个联系方式显示出来</div>
          </div>
        </div>
        <div class="admin-form-grid">
          <label class="field">
            <span class="field__label">管理员邮箱</span>
            <span class="textbox"><input data-key="contactEmail" maxlength="254" placeholder="you@example.com" value="${escapeHtml(settings.contactEmail || "")}"></span>
          </label>
          <label class="field">
            <span class="field__label">管理员 QQ</span>
            <span class="textbox"><input data-key="contactQq" maxlength="32" placeholder="选填" value="${escapeHtml(settings.contactQq || "")}"></span>
          </label>
        </div>
        <div class="admin-card__foot">
          <button class="btn btn--accent" type="button" data-save="contact">保存联系方式</button>
        </div>
      </section>

      <section class="settings-card" data-card="limits">
        <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-upload"/></svg>配额与上传</div>
        <div class="admin-form-grid">
          <label class="field">
            <span class="field__label">新用户默认配额</span>
            <span class="textbox admin-unit">
              <input data-key="defaultQuotaBytes" data-kind="number" data-multiplier="${GB}" type="number" min="1" step="1"
                     value="${Math.round((settings.defaultQuotaBytes / GB) * 100) / 100}">
              <span class="admin-unit__suffix">GB</span>
            </span>
          </label>
          <label class="field">
            <span class="field__label">单文件上限</span>
            <span class="textbox admin-unit">
              <input data-key="maxFileSizeBytes" data-kind="number" data-multiplier="${MB}" type="number" min="1" step="1"
                     value="${Math.round((settings.maxFileSizeBytes / MB) * 100) / 100}">
              <span class="admin-unit__suffix">MB</span>
            </span>
          </label>
          <label class="field">
            <span class="field__label">分片大小</span>
            <span class="textbox admin-unit">
              <input data-key="chunkSizeBytes" data-kind="number" data-multiplier="${MB}" type="number" min="1" max="32" step="1"
                     value="${Math.round((settings.chunkSizeBytes / MB) * 100) / 100}">
              <span class="admin-unit__suffix">MB</span>
            </span>
          </label>
          <label class="field">
            <span class="field__label">回收站保留天数</span>
            <span class="textbox admin-unit">
              <input data-key="trashRetentionDays" data-kind="number" data-multiplier="1" type="number" min="0" max="3650" step="1"
                     value="${settings.trashRetentionDays}">
              <span class="admin-unit__suffix">天</span>
            </span>
          </label>
        </div>
        <div class="admin-hint">分片大小只影响新上传；回收站填 0 表示不自动清理（仍可手动清空）。</div>
        <div class="admin-card__foot">
          <button class="btn btn--accent" type="button" data-save="limits">保存限制</button>
        </div>
      </section>

      <section class="settings-card">
        <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-database"/></svg>维护</div>
        <div class="setting-row">
          <div class="setting-row__main">
            <div class="setting-row__label">清理审计日志</div>
            <div class="setting-row__desc">删除指定天数之前的记录，当前记录数不会立即变化</div>
          </div>
          <button class="btn" type="button" data-act="purge-logs">清理</button>
        </div>
        <div class="setting-row">
          <div class="setting-row__main">
            <div class="setting-row__label">孤儿文件扫描</div>
            <div class="setting-row__desc">对比磁盘 blob 与数据库记录，清理上传中断留下的残骸</div>
          </div>
          <button class="btn" type="button" data-act="blob-gc">扫描</button>
        </div>
        <div class="setting-row">
          <div class="setting-row__main">
            <div class="setting-row__label">运行环境</div>
            <div class="setting-row__desc mono">${escapeHtml(settings.runtime.storagePath)}</div>
          </div>
          <span class="chip">${dbLabel(settings.runtime.database, settings.runtime.dbMode)} · v${escapeHtml(
      settings.runtime.version
    )}</span>
        </div>
      </section>`;

    panel.querySelectorAll("[data-save]").forEach((btn) => {
      btn.addEventListener("click", () => saveCard(btn));
    });

    panel.querySelector('[data-act="purge-logs"]')?.addEventListener("click", purgeLogs);
    panel.querySelector('[data-act="blob-gc"]')?.addEventListener("click", blobGc);

    async function saveCard(btn) {
      const card = btn.closest("[data-card]");
      let payload;
      try {
        payload = collectCard(card);
      } catch (err) {
        Toast.error(err.message);
        return;
      }
      if (payload.siteName !== undefined && !payload.siteName.trim()) {
        Toast.error("站点名称不能为空");
        return;
      }
      btn.disabled = true;
      try {
        const res = await api.patch("/api/admin/settings", payload);
        Toast.success("设置已保存");
        if (payload.siteName !== undefined || payload.siteAnnouncement !== undefined || payload.allowRegister !== undefined) {
          await refreshMeta();
        }
        void res;
      } catch (err) {
        Toast.error(err.message);
      } finally {
        btn.disabled = false;
      }
    }

    async function purgeLogs() {
      const days = await Dialog.prompt({
        title: "清理审计日志",
        message: "将删除指定天数之前的全部记录，此操作不可撤销。",
        label: "保留最近多少天",
        value: "90",
        confirmText: "开始清理",
        validate: (value) => {
          const num = Number(value);
          if (!Number.isInteger(num) || num < 1) return "请输入大于 0 的整数";
          if (num > 3650) return "最多保留 3650 天";
          return null;
        },
      });
      if (days === null) return;
      try {
        const res = await api.post("/api/admin/logs/purge", { days: Number(days) });
        Toast.success(`已清理 ${res.removed} 条记录`);
        state.actions = null;
      } catch (err) {
        Toast.error(err.message);
      }
    }

    async function blobGc() {
      try {
        const scan = await api.post("/api/admin/maintenance/blob-gc", { apply: false });
        if (!scan.orphans) {
          await Dialog.alert({
            title: "扫描完成",
            message: `数据库登记 ${scan.knownBlobs} 个 blob，磁盘 ${scan.diskFiles} 个文件，没有发现孤儿文件。`,
          });
          return;
        }
        const ok = await Dialog.confirm({
          title: "发现孤儿文件",
          message: `磁盘上有 <b>${scan.orphans}</b> 个未被数据库引用的文件（${formatBytes(
            scan.orphanBytes
          )}），其中 <b>${scan.removable}</b> 个放置超过 1 小时，可以安全删除。<br>
            另有 ${scan.tmpStale} 个超过 6 小时的临时上传文件会被一并清理。`,
          confirmText: "确认清理",
          danger: true,
        });
        if (!ok) return;
        const res = await api.post("/api/admin/maintenance/blob-gc", { apply: true });
        Toast.success(`已清理 ${res.removed} 个文件，释放 ${formatBytes(res.freedBytes)}`);
      } catch (err) {
        Toast.error(err.message);
      }
    }
  }

  function collectCard(card) {
    const payload = {};
    card.querySelectorAll("[data-key]").forEach((input) => {
      const key = input.dataset.key;
      if (input.type === "checkbox") {
        payload[key] = input.checked;
        return;
      }
      if (input.dataset.kind === "number") {
        const raw = input.value.trim();
        const num = Number(raw);
        if (!raw || !Number.isFinite(num) || num < 0) {
          throw new Error("请填写有效的非负数字");
        }
        payload[key] = Math.round(num * Number(input.dataset.multiplier || 1));
        return;
      }
      payload[key] = input.value.trim();
    });
    // 分片大小后端有 64 KiB ~ 32 MiB 的硬限制，这里先挡一道
    if (payload.chunkSizeBytes !== undefined && (payload.chunkSizeBytes < 64 * 1024 || payload.chunkSizeBytes > 32 * MB)) {
      throw new Error("分片大小需在 1 ~ 32 MB 之间");
    }
    return payload;
  }

  async function refreshMeta() {
    try {
      const meta = await api.get("/api/meta");
      store.set({ meta });
      const name = meta.appName || "CloudNETP";
      const brand = document.getElementById("brand-name");
      if (brand) brand.textContent = name;
      const version = document.getElementById("brand-version");
      if (version) version.textContent = `v${meta.version}`;
      document.title = `管理后台 · ${name}`;
    } catch {
      /* 同步失败不影响设置已保存 */
    }
  }

  /* ---------------------------------------------------------------- */
  /* 切换                                                              */
  /* ---------------------------------------------------------------- */
  function switchTab(name) {
    state.tab = name;
    segBtns.forEach((btn) => btn.classList.toggle("is-active", btn.dataset.tab === name));
    window.requestAnimationFrame(layoutThumb);
    if (name === "overview") showOverview();
    else if (name === "users") showUsers();
    else if (name === "reports") showReports();
    else if (name === "logs") showLogs();
    else showSettings();
  }

  segBtns.forEach((btn) => btn.addEventListener("click", () => switchTab(btn.dataset.tab)));
  root.querySelector('[data-act="refresh"]').addEventListener("click", () => {
    state.actions = null;
    switchTab(state.tab);
  });

  // 窗口尺寸变化时 app.js 的全局 resize 监听会重排 segmented 滑块，这里不必重复挂
  window.requestAnimationFrame(layoutThumb);
  switchTab("overview");
}
