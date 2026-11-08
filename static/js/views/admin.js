/**
 * 管理后台：概览 / 用户管理 / 审计日志 / 系统设置
 *
 * 只有 role === 1 的用户能进入（路由层已拦截，后端也会二次校验）。
 */
import { api, escapeHtml, formatBytes, formatTime, percent } from "../api.js";
import { H } from "../i18n.js";
import { store } from "../store.js";
import { avatarMarkup } from "../ui/avatar.js";
import { Dialog } from "../ui/dialog.js";
import { openMenu } from "../ui/menu.js";
import { copyText } from "../ui/share.js";
import { Toast } from "../ui/toast.js";
import { checkForUpdates, updateResultHtml, applyResultHtml } from "../ui/update.js";

const TABS = [
  { id: "overview", label: "概览", icon: "i-chart" },
  { id: "users", label: "用户", icon: "i-user" },
  { id: "reports", label: "举报审核", icon: "i-alert" },
  { id: "feedback", label: "意见反馈", icon: "i-mail" },
  { id: "logs", label: "审计日志", icon: "i-clock" },
  { id: "settings", label: "系统设置", icon: "i-settings" },
];

const PAGE_SIZE = 20;
const LOG_PAGE_SIZE = 30;
const GB = 1024 ** 3;
const MB = 1024 ** 2;

const loading = () => H`<div class="admin-loading"><span class="spinner"></span></div>`;

const errorBlock = (msg) => H`
  <div class="empty">
    <div class="empty__art"><svg class="icon"><use href="#i-alert"/></svg></div>
    <div class="empty__title">加载失败</div>
    <div class="empty__desc">${escapeHtml(msg)}</div>
  </div>`;

function statCard(icon, label, value, sub) {
  return H`
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
  return H`
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
        .map(([k, v]) => H`${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)
        .join(" · ");
    }
    return String(data);
  } catch {
    return String(raw);
  }
}

/* ------------------------------------------------------------------ */
/* 数据库卡片：容灾状态 / 刷新配置 / 手动重连                          */
/* ------------------------------------------------------------------ */
function dbCardMarkup(db) {
  const head = `<div class="settings-card__head"><svg class="icon icon--s"><use href="#i-database"/></svg>数据库</div>`;
  if (!db) {
    return H`<section class="settings-card" data-card="db">${head}
      <div class="admin-hint">数据库状态读取失败，点右上角刷新按钮重试</div>
    </section>`;
  }

  const p = db.primary || {};
  const f = db.fallback || {};
  const w = db.watchdog || {};
  const reach = p.reachable
    ? H`<span class="db-state is-ok">可达</span><small>${p.latencyMs} ms</small>`
    : H`<span class="db-state is-bad">不可达</span><small>超时 ${p.connectTimeout}s</small>`;
  const fallbackState = f.enabled
    ? H`${f.mirrored ? "可信副本" : "尚未镜像过主库"}${f.dirty ? " · 有未回迁数据" : ""}`
    : "未开启";
  const switched = db.switchedAt
    ? H`${formatTime(db.switchedAt)}${db.switchReason ? H` · ${escapeHtml(db.switchReason)}` : ""}`
    : "进程启动后没有切换过";
  const held = w.held ? H`<span class="chip">已暂停自动回切</span>` : "";
  const sqliteOnly = p.isSqlite;

  return H`<section class="settings-card" data-card="db">${head}
    <div class="setting-row">
      <div class="setting-row__main">
        <div class="setting-row__label">当前服务库
          <span class="chip ${db.mode === "fallback" ? "chip--warn" : "chip--accent"}">${escapeHtml(db.modeLabel || db.mode)}</span>
          ${held}
        </div>
        <div class="setting-row__desc">最近切换：${switched}</div>
      </div>
    </div>

    <div class="admin-facts">
      <div class="is-wide"><span>主库</span><b class="mono" title="${escapeHtml(p.url || "")}">${escapeHtml(p.url || "—")}</b></div>
      <div><span>探活</span>${reach}</div>
      <div><span>副本状态</span><b>${fallbackState}</b></div>
      <div class="is-wide"><span>容灾副本</span><b class="mono" title="${escapeHtml(f.path || "")}">${escapeHtml(f.path || "—")}</b><small>${formatBytes(f.bytes || 0)}</small></div>
      <div><span>自动容灾</span><b>${f.enabled ? "开启" : "关闭"}</b><small>${
        w.probeInterval > 0 ? H`探测 ${w.probeInterval}s` : "不自动探测"
      } · ${w.mirrorInterval > 0 ? H`镜像 ${w.mirrorInterval}s` : "不周期镜像"}</small></div>
      <div><span>看门狗</span><b>${w.running ? "运行中" : "未运行"}</b><small>${
        w.lastMirrorAt ? H`上次镜像 ${formatTime(w.lastMirrorAt)}` : "还没镜像过"
      }</small></div>
    </div>

    <div class="setting-row">
      <div class="setting-row__main">
        <div class="setting-row__label">刷新配置</div>
        <div class="setting-row__desc">重读 <code>.env</code> 里的 <code>DATABASE_URL</code> 与容灾参数，重建连接池并重新探活，不用重启服务</div>
      </div>
      <button class="btn" type="button" data-db-act="refresh">刷新配置</button>
    </div>

    <div class="setting-row">
      <div class="setting-row__main">
        <div class="setting-row__label">重新连接 MySQL</div>
        <div class="setting-row__desc">丢弃旧连接池重新握手；当前跑在 SQLite 时会先把本地数据回迁到主库，成功才切回</div>
      </div>
      <button class="btn" type="button" data-db-act="primary">重新连接 MySQL</button>
    </div>

    <div class="setting-row">
      <div class="setting-row__main">
        <div class="setting-row__label">重新连接 SQLite</div>
        <div class="setting-row__desc">${
          sqliteOnly
            ? "当前 DATABASE_URL 本来就是 SQLite 文件，容灾切换只在主库指向远程 MySQL 时有意义"
            : "切到本地容灾库（切之前先镜像一份最新副本）；切过来会暂停自动回切，直到你点上面的按钮"
        }</div>
      </div>
      <button class="btn btn--subtle" type="button" data-db-act="fallback">重新连接 SQLite</button>
    </div>

    <p class="admin-hint">连接串只显示到库名，密码在服务端已脱敏；手动切换与刷新都会记进审计日志。</p>
  </section>`;
}

export function renderAdmin(root) {
  const state = {
    tab: "overview",
    actions: null,
    users: { page: 1, query: "", filter: "all", total: 0 },
    reports: { page: 1, filter: "pending", total: 0 },
    feedback: { page: 1, filter: "pending", total: 0 },
    logs: { page: 1, query: "", action: "", userId: null, username: "", total: 0 },
  };

  root.innerHTML = H`
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
            H`<button class="segmented__btn ${tab.id === "overview" ? "is-active" : ""}" type="button" data-tab="${tab.id}">
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
      if (state.tab === "overview") panel.innerHTML = errorBlock(err.message);
      return;
    }
    // 用户可能在请求返回前就切走了，别把新标签页覆盖掉
    if (state.tab !== "overview") return;
    const { users: u, nodes: n, shares: s, storage: st, system } = data;
    const quotaPct = percent(u.usedBytes, u.quotaBytes || 1);
    const diskPct = percent(st.diskUsed, st.diskTotal || 1);
    setSub(`${system.appName} v${system.version} · ${dbLabel(system.database, system.dbMode)}`);

    panel.innerHTML = H`
      <div class="stat-grid">
        ${statCard(
          "i-user",
          "用户",
          `${u.total}`,
          `今日活跃 ${u.activeToday} · 本周新增 ${u.newThisWeek}${u.banned ? H` · 封禁 ${u.banned}` : ""}`
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
                      (log) => H`
              <div class="admin-feed__row">
                <span class="admin-feed__dot ${log.tone === "info" ? "" : H`is-${log.tone}`}"></span>
                <span class="admin-feed__main"><b>${escapeHtml(log.username || "系统")}</b> · ${escapeHtml(
                        log.label
                      )} ${escapeHtml(detailLine(log.detail))}</span>
                <span class="admin-feed__time">${formatTime(log.createdAt)}</span>
              </div>`
                    )
                    .join("")
                : H`<div class="admin-feed__row"><span class="admin-feed__main muted">还没有审计记录</span></div>`
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
                      (item, index) => H`
              <div class="admin-rank__row">
                <span class="admin-rank__idx">${index + 1}</span>
                <span class="admin-rank__name">${escapeHtml(item.username)}</span>
                <span class="admin-rank__num">${formatBytes(item.usedBytes)} / ${formatBytes(item.quotaBytes)}</span>
              </div>`
                    )
                    .join("")
                : H`<div class="admin-rank__row"><span></span><span class="muted">暂无数据</span><span></span></div>`
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
    panel.innerHTML = H`
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
              H`<button class="admin-chip ${state.users.filter === f.id ? "is-active" : ""}" type="button" data-filter="${f.id}">${f.label}</button>`
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
        tableEl.innerHTML = H`
          <div class="empty">
            <div class="empty__art"><svg class="icon"><use href="#i-user"/></svg></div>
            <div class="empty__title">没有匹配的用户</div>
            <div class="empty__desc">换个关键词，或把筛选切回「全部」。</div>
          </div>`;
        pagerEl.innerHTML = "";
        return;
      }

      tableEl.innerHTML = H`
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
    // 名称优先用昵称（display_name），登录名只在两者不同时作为次要标识
    const shownName = user.displayName || user.username;
    const alias = shownName !== user.username ? H`@${user.username} · ` : "";
    return H`
      <div class="admin-row" data-id="${user.id}">
        <div class="admin-user">
          <span class="admin-avatar">${avatarMarkup({ displayName: shownName, username: user.username, avatarUrl: user.avatarUrl })}</span>
          <div style="min-width:0">
            <div class="admin-user__name" title="${escapeHtml(shownName)}">${escapeHtml(shownName)} ${roleChip}</div>
            <div class="admin-user__meta">${escapeHtml(alias)}#${user.id} · ${user.createdAt ? H`注册于 ${formatTime(user.createdAt)}` : "—"}</div>
          </div>
        </div>
        <div class="admin-space">
          <div class="admin-space__num">${formatBytes(user.usedBytes)} / ${formatBytes(user.quotaBytes)}</div>
          <div class="progress"><div class="progress__bar" style="width:${pct}%"></div></div>
        </div>
        <div class="admin-cell">${user.fileCount} 个${user.trashCount ? H` · 回收站 ${user.trashCount}` : ""}</div>
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
        name: user.displayName || user.username,
        meta: H`@${user.username} · #${user.id} · ${user.role === 1 ? "管理员" : "普通用户"} · ${formatBytes(user.usedBytes)}`,
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
      title: H`调整配额 · ${user.username}`,
      message: H`当前已用 ${formatBytes(user.usedBytes)}。配额低于已用空间时，该用户将无法继续上传。`,
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
      message: H`将为 <b>${escapeHtml(user.username)}</b> 生成一个新的随机密码，该用户所有已登录设备会立即失效。`,
      confirmText: "生成新密码",
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await api.post(`/api/admin/users/${user.id}/password`);
      const copy = await Dialog.confirm({
        title: "新密码（仅显示这一次）",
        message: H`请通过安全渠道转交给 <b>${escapeHtml(res.username)}</b>，关闭后无法再次查看：
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
          ? H`<b>${escapeHtml(user.username)}</b> 将获得管理后台权限，可以查看所有用户与审计日志、修改系统设置。`
          : H`<b>${escapeHtml(user.username)}</b> 将失去管理后台权限，普通功能不受影响。`,
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
        ? H`封禁后 <b>${escapeHtml(user.username)}</b> 会立即被踢下线，且无法再登录。文件不会被删除，解封后恢复。`
        : H`<b>${escapeHtml(user.username)}</b> 将恢复登录权限。`,
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
      title: H`删除用户 · ${user.username}`,
      message: H`将永久删除该账号及其 <b>${user.fileCount}</b> 个文件（${formatBytes(user.usedBytes)}）、
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

  // 举报内容预览用的类型名与图标
  const PREVIEW_LABEL = {
    image: "图片",
    video: "视频",
    audio: "音频",
    pdf: "PDF",
    text: "文本",
    markdown: "Markdown",
    docx: "Word 文档",
    doc: "Word 文档",
    odoc: "Word 文档",
    rtf: "RTF 文档",
    sheet: "表格",
    osheet: "表格",
    slide: "演示文稿",
    ppt: "演示文稿",
    oslide: "演示文稿",
    toolarge: "大文件",
    none: "其他文件",
  };

  const svgIcon = (name) => H`<svg class="icon"><use href="#${name}"/></svg>`;

  async function showReports() {
    panel.innerHTML = H`
      <div class="admin-toolbar">
        <div class="admin-chips">
          ${REPORT_FILTERS.map(
            (f) =>
              H`<button class="admin-chip ${state.reports.filter === f.id ? "is-active" : ""}" type="button" data-filter="${f.id}">${f.label}</button>`
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
        tableEl.innerHTML = H`
          <div class="empty">
            <div class="empty__art"><svg class="icon"><use href="#i-alert"/></svg></div>
            <div class="empty__title">没有举报记录</div>
            <div class="empty__desc">分享页被举报后会出现在这里等待审核。</div>
          </div>`;
        pagerEl.innerHTML = "";
        return;
      }

      tableEl.innerHTML = H`
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
        // 点缩略图直接看原图，不要再弹出详情
        row.querySelector(".admin-thumb")?.addEventListener("click", (event) => event.stopPropagation());
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
    const iconName = report.icon || (report.isFolder ? "i-folder" : "i-file");
    const previewHref = report.nodeId
      ? H`/api/admin/reports/${report.id}/preview?nodeId=${report.nodeId}`
      : H`/api/admin/reports/${report.id}/preview`;
    // 图片直接给缩略图，审核时一眼能看出内容；点开就是原图
    const visual =
      report.previewKind === "image"
        ? H`<a class="admin-thumb" href="${previewHref}" target="_blank" rel="noopener" title="查看原图">
             <img src="/api/admin/reports/${report.id}/thumb?w=96${
            report.nodeId ? H`&nodeId=${report.nodeId}` : ""
          }" alt="" loading="lazy">
           </a>`
        : H`<span class="admin-fileicon" data-kind="${iconName.replace(/^i-/, "")}">${svgIcon(iconName)}</span>`;
    const kindLabel = PREVIEW_LABEL[report.previewKind] || "文件";
    // 单文件能看到确切大小；文件夹/合集只报总量，点开才看清单
    const metaLine = report.blobSha256
      ? H`${kindLabel} · ${formatBytes(report.size)}`
      : H`合集 · 共 ${formatBytes(report.size)}`;
    const hash = report.blobSha256
      ? H`<div class="report-hash" data-copy-hash title="点击复制完整哈希">${escapeHtml(
          report.blobSha256.slice(0, 16)
        )}…${escapeHtml(report.blobSha256.slice(-8))}</div>
         <div class="admin-user__meta">${report.refCount} 处引用</div>`
      : H`<div class="admin-user__meta">合集分享不记单个哈希</div>
         <div class="admin-user__meta">共 ${formatBytes(report.size)}</div>`;
    const reason = report.reason
      ? H`<div class="report-reason" title="${escapeHtml(report.reason)}">${escapeHtml(report.reason)}</div>`
      : H`<div class="report-reason muted">未填写理由</div>`;
    const reporter = `${escapeHtml(report.reporter || "匿名")}${
      report.reporterIp ? H` · ${escapeHtml(report.reporterIp)}` : ""
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
    return H`
      <div class="admin-row admin-row--report" data-id="${report.id}">
        <div class="admin-user">
          ${visual}
          <div style="min-width:0">
            <div class="admin-user__name" title="${escapeHtml(report.firstName)}">${escapeHtml(report.firstName)}</div>
            <div class="admin-user__meta">${metaLine} · ${shareState}</div>
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
      ? H`审核通过后该分享链接会立即失效（文件夹分享不涉及文件哈希）。`
      : H`审核通过后：
        <div class="dialog__hint">· 哈希 <span class="code-display" style="display:inline;padding:2px 8px">${escapeHtml(
          report.blobSha256 || ""
        )}</span> 会被拉黑</div>
        <div class="dialog__hint">· 该文件的所有分享链接立即失效，且不能再被分享、转存</div>
        <div class="dialog__hint">· 文件本体不删除，分享者自己的网盘仍可查看和下载</div>`;
    const ok = await Dialog.confirm({
      title: "确认举报通过？",
      message: H`「<b>${escapeHtml(report.firstName)}</b>」${detail}`,
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
      message: H`将把「<b>${escapeHtml(report.firstName)}</b>」的举报标记为已驳回，分享不受影响。`,
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
      message: H`解除后哈希 <b>${escapeHtml(report.blobSha256 || "")}</b> 对应的文件可以重新被分享。`,
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

  async function showReportDetail(report) {
    let files = null;
    try {
      files = await api.get(`/api/admin/reports/${report.id}/files`);
    } catch (err) {
      Toast.error(err.message);
      return;
    }
    const items = files.items || [];
    const fileRows = items.length
      ? items
          .map((f) => {
            const iconName = f.icon || "i-file";
            const visual =
              f.previewKind === "image" && !f.missing
                ? H`<img class="report-file__thumb" src="/api/admin/reports/${report.id}/thumb?nodeId=${f.nodeId}&w=120" alt="" loading="lazy">`
                : H`<span class="admin-fileicon" data-kind="${iconName.replace(/^i-/, "")}">${svgIcon(
                    iconName
                  )}</span>`;
            const open = f.missing
              ? '<span class="admin-user__meta">内容已删除</span>'
              : H`<a class="btn btn--subtle btn--sm" href="/api/admin/reports/${report.id}/preview?nodeId=${
                  f.nodeId
                }" target="_blank" rel="noopener">打开</a>`;
            return H`
              <div class="report-file">
                ${visual}
                <div class="report-file__main">
                  <div class="report-file__name" title="${escapeHtml(f.label || f.name)}">${escapeHtml(
              f.label || f.name
            )}</div>
                  <div class="admin-user__meta">${PREVIEW_LABEL[f.previewKind] || "文件"} · ${formatBytes(
              f.size
            )}</div>
                </div>
                ${open}
              </div>`;
          })
          .join("")
      : '<div class="admin-user__meta">这个分享里的内容已经被删除了。</div>';

    await Dialog.alert({
      title: H`举报详情 · ${report.firstName}`,
      message: H`
        <div class="dialog__hint">分享链接 /s/${escapeHtml(report.token)}（${
        report.shareActive ? "仍有效" : "已失效"
      }）</div>
        ${
          report.blobSha256
            ? H`<div class="dialog__hint">哈希值：<span class="code-display" style="display:inline;padding:2px 8px">${escapeHtml(
                report.blobSha256
              )}</span></div>`
            : '<div class="dialog__hint">类型：文件夹 / 合集分享</div>'
        }
        <div class="dialog__hint">举报理由：${escapeHtml(report.reason || "未填写")}</div>
        <div class="dialog__hint">举报人：${escapeHtml(report.reporter || "匿名")}${
        report.reporterIp ? H` · IP ${escapeHtml(report.reporterIp)}` : ""
      }</div>
        <div class="dialog__hint">时间：${formatTime(report.createdAt)}${
        report.handledAt
          ? H` · 处理：${escapeHtml(report.handledBy || "—")} ${formatTime(report.handledAt)}`
          : ""
      }</div>
        <div class="report-detail__head">
          被举报内容 · ${items.length ? H`${items.length} 个文件 · ${formatBytes(
          items.reduce((sum, f) => sum + (f.size || 0), 0)
        )}` : "无"}
        </div>
        <div class="report-files">${fileRows}</div>`,
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

  /* ---------------------------------------------------------------- */
  /* 意见反馈                                                          */
  /* ---------------------------------------------------------------- */
  const FEEDBACK_FILTERS = [
    { id: "pending", label: "待处理" },
    { id: "resolved", label: "已处理" },
    { id: "all", label: "全部" },
  ];

  async function showFeedback() {
    panel.innerHTML = H`
      <div class="admin-toolbar">
        <div class="admin-chips">
          ${FEEDBACK_FILTERS.map(
            (f) =>
              H`<button class="admin-chip ${state.feedback.filter === f.id ? "is-active" : ""}" type="button" data-filter="${f.id}">${f.label}</button>`
          ).join("")}
        </div>
        <div class="grow"></div>
        <span class="admin-count" data-count></span>
      </div>
      <div data-list>${loading()}</div>
      <div class="admin-pager" data-pager></div>`;

    const listEl = panel.querySelector("[data-list]");
    const countEl = panel.querySelector("[data-count]");
    const pagerEl = panel.querySelector("[data-pager]");

    panel.querySelectorAll("[data-filter]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.feedback.filter = btn.dataset.filter;
        state.feedback.page = 1;
        panel.querySelectorAll("[data-filter]").forEach((b) => b.classList.toggle("is-active", b === btn));
        loadFeedback();
      });
    });

    async function loadFeedback() {
      listEl.innerHTML = loading();
      const params = new URLSearchParams({
        status: state.feedback.filter,
        page: String(state.feedback.page),
        pageSize: String(PAGE_SIZE),
      });
      let data;
      try {
        data = await api.get(`/api/admin/feedback?${params}`);
      } catch (err) {
        if (state.tab === "feedback") {
          listEl.innerHTML = errorBlock(err.message);
          pagerEl.innerHTML = "";
        }
        return;
      }
      if (state.tab !== "feedback") return;
      countEl.textContent = `${data.total} 条反馈`;
      setSub(`${data.total} 条反馈 · ${
        FEEDBACK_FILTERS.find((f) => f.id === state.feedback.filter)?.label || "全部"
      }`);

      if (!data.items.length) {
        listEl.innerHTML = H`
          <div class="empty">
            <div class="empty__art"><svg class="icon"><use href="#i-mail"/></svg></div>
            <div class="empty__title">暂无反馈</div>
            <div class="empty__desc">用户从「意见反馈」页提交后会出现在这里。</div>
          </div>`;
        pagerEl.innerHTML = "";
        return;
      }

      listEl.innerHTML = H`<div class="feedback-list admin-feedback">${data.items
        .map(
          (item) => H`
          <article class="fb-item" data-fb="${item.id}">
            <div class="fb-item__top">
              <span class="fb-tag fb-tag--${escapeHtml(item.kind)}">${escapeHtml(item.kindLabel)}</span>
              <span class="fb-status ${item.status ? "is-done" : "is-pending"}">${escapeHtml(item.statusLabel)}</span>
              <span class="fb-item__user">${escapeHtml(item.displayName || item.username || "未知用户")}${
                item.displayName && item.displayName !== item.username
                  ? H` <span class="muted">@${escapeHtml(item.username)}</span>`
                  : ""
              }</span>
              <span class="fb-item__time">${escapeHtml(formatTime(item.createdAt))}</span>
            </div>
            ${item.title ? H`<div class="fb-item__title">${escapeHtml(item.title)}</div>` : ""}
            <p class="fb-item__content">${escapeHtml(item.content)}</p>
            ${item.contact ? H`<div class="fb-item__meta">联系方式：${escapeHtml(item.contact)}</div>` : ""}
            <div class="fb-item__actions">
              ${
                item.status
                  ? H`<button class="btn btn--subtle btn--sm" type="button" data-reopen>重新打开</button>`
                  : H`<button class="btn btn--accent btn--sm" type="button" data-resolve>标记已处理</button>`
              }
              <button class="btn btn--subtle btn--sm" type="button" data-delete>删除</button>
            </div>
          </article>`
        )
        .join("")}</div>`;

      listEl.querySelectorAll("[data-fb]").forEach((card) => {
        const id = Number(card.dataset.fb);
        const item = data.items.find((entry) => entry.id === id);
        if (!item) return;

        card.querySelector("[data-resolve]")?.addEventListener("click", () => updateFeedback(item, 1));
        card.querySelector("[data-reopen]")?.addEventListener("click", () => updateFeedback(item, 0));
        card.querySelector("[data-delete]")?.addEventListener("click", async () => {
          const ok = await Dialog.confirm({
            title: "删除这条反馈",
            message: H`来自 <b>${escapeHtml(item.displayName || item.username || "用户")}</b> 的反馈将被永久删除，不可恢复。`,
            confirmText: "删除",
            danger: true,
          });
          if (!ok) return;
          try {
            await api.del(`/api/admin/feedback/${item.id}`);
            Toast.success("反馈已删除");
            loadFeedback();
          } catch (err) {
            Toast.error(err.message);
          }
        });
      });

      pagerEl.innerHTML = pagerHtml(state.feedback.page, data.total, PAGE_SIZE);
      pagerEl.querySelectorAll("[data-page]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const page = Number(btn.dataset.page);
          if (!page || page === state.feedback.page) return;
          state.feedback.page = page;
          loadFeedback();
        });
      });
    }

    async function updateFeedback(item, status) {
      try {
        await api.patch(`/api/admin/feedback/${item.id}`, { status });
        Toast.success(status ? "已标记为已处理" : "已重新打开");
        loadFeedback();
      } catch (err) {
        Toast.error(err.message);
      }
    }

    loadFeedback();
  }

  async function showLogs() {
    const actions = await ensureActions();
    panel.innerHTML = H`
      <div class="admin-toolbar">
        <span class="selectbox">
          <select data-action>
            <option value="">全部操作</option>
            ${actions
              .map(
                (item) =>
                  H`<option value="${escapeHtml(item.action)}" ${
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
        listEl.innerHTML = H`
          <div class="empty">
            <div class="empty__art"><svg class="icon"><use href="#i-clock"/></svg></div>
            <div class="empty__title">没有匹配的记录</div>
            <div class="empty__desc">调整筛选条件，或换个关键词试试。</div>
          </div>`;
        pagerEl.innerHTML = "";
        return;
      }

      listEl.innerHTML = H`
        <div class="log-list">
          ${data.items
            .map(
              (log) => H`
            <div class="log-row" data-id="${log.id}">
              <span class="log-dot ${log.tone === "info" ? "" : H`is-${log.tone}`}"></span>
              <div class="log-main">
                <div class="log-title">
                  ${escapeHtml(log.label)}
                  <span class="log-title__who">${escapeHtml(log.username || "系统")}${log.userId ? H` #${log.userId}` : ""}</span>
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
      title: H`${log.label} · ${log.username || "系统"}`,
      message: H`
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
    let dbInfo = null;
    try {
      const [data, dbData] = await Promise.all([
        api.get("/api/admin/settings"),
        // 数据库卡片单独取：这一项失败不该让整个设置页白屏
        api.get("/api/admin/db").catch(() => null),
      ]);
      settings = data.settings;
      dbInfo = dbData?.db || null;
    } catch (err) {
      panel.innerHTML = errorBlock(err.message);
      return;
    }
    setSub("站点信息、数据库与维护");

    panel.innerHTML = H`
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

      <section class="settings-card" data-card="update">
        <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-refresh"/></svg>版本更新</div>
        <div class="setting-row">
          <div class="setting-row__main">
            <div class="setting-row__label">自动检查更新</div>
            <div class="setting-row__desc">管理员登录后台时每天最多检查一次 GitHub main 分支；只提示，不改代码</div>
          </div>
          <label class="switch">
            <input type="checkbox" data-key="autoUpdateCheck" ${settings.autoUpdateCheck ? "checked" : ""}>
            <span class="switch__track"><span class="switch__knob"></span></span>
          </label>
        </div>
        <div class="setting-row">
          <div class="setting-row__main">
            <div class="setting-row__label">自动更新</div>
            <div class="setting-row__desc">后台每 6 小时检查一次，发现新版本自动拉取并重启服务；仅在 main 分支且工作区干净时执行</div>
          </div>
          <label class="switch">
            <input type="checkbox" data-key="autoUpdate" ${settings.autoUpdate ? "checked" : ""}>
            <span class="switch__track"><span class="switch__knob"></span></span>
          </label>
        </div>
        <div class="setting-row">
          <div class="setting-row__main">
            <div class="setting-row__label">手动更新</div>
            <div class="setting-row__desc">立即执行 git fetch + fast-forward 并重启；有未提交改动会拒绝。私有仓库需要 UPDATE_TOKEN 或可用的 git 凭据</div>
          </div>
          <button class="btn" type="button" data-act="check-update">检查更新</button>
          <button class="btn" type="button" data-act="apply-update">立即更新</button>
        </div>
        <p class="admin-hint" data-update-result hidden></p>
        <div class="admin-card__foot">
          <button class="btn btn--accent" type="button" data-save="update">保存更新设置</button>
        </div>
      </section>

      ${dbCardMarkup(dbInfo)}

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

    /* --- 数据库卡片：刷新配置 / 手动重连 --- */
    function paintDbCard(db) {
      const card = panel.querySelector('[data-card="db"]');
      if (!card) return;
      card.outerHTML = dbCardMarkup(db);
      bindDbCard();
    }

    function bindDbCard() {
      const card = panel.querySelector('[data-card="db"]');
      if (!card) return;

      // 按钮在请求期间可能随卡片一起被重绘，所以要先把引用和文案存下来
      async function run(button, task) {
        const label = button.textContent;
        button.disabled = true;
        button.textContent = "处理中…";
        try {
          await task();
        } catch (err) {
          Toast.error(err.message || "操作失败");
        } finally {
          button.disabled = false;
          button.textContent = label;
        }
      }

      card.querySelector('[data-db-act="refresh"]')?.addEventListener("click", (event) => {
        const button = event.currentTarget;
        run(button, async () => {
          const data = await api.post("/api/admin/db/refresh", {});
          const changed = Object.keys(data.result?.changes || {});
          paintDbCard(data.db);
          if (changed.length) {
            const extra = data.result?.urlChanged ? "（连接串已更换）" : "";
            Toast.success(`配置已刷新：${changed.join("、")}${extra}`);
          } else {
            Toast.info("配置已刷新，没有变化");
          }
        });
      });

      card.querySelector('[data-db-act="primary"]')?.addEventListener("click", async (event) => {
        const button = event.currentTarget;
        const ok = await Dialog.confirm({
          title: "重新连接 MySQL",
          message:
            "会丢弃当前连接池重新握手。<br>如果服务正跑在 SQLite 上，会先把本地库的数据回迁到主库，" +
            "回迁成功才切回；主库连不上则保持现状并报错。",
          confirmText: "重连",
        });
        if (!ok) return;
        run(button, async () => {
          const data = await api.post("/api/admin/db/reconnect", { target: "primary" });
          const rows = data.result?.migratedRows || 0;
          paintDbCard(data.db);
          Toast.success(`已连上 MySQL${rows ? H`，回迁 ${rows} 行` : ""}`);
        });
      });

      card.querySelector('[data-db-act="fallback"]')?.addEventListener("click", async (event) => {
        const button = event.currentTarget;
        const ok = await Dialog.confirm({
          title: "重新连接 SQLite",
          message:
            "会先镜像一份主库的最新副本，然后切到本地数据库服务。<br>" +
            "切过来后自动回切会暂停，直到你点「重新连接 MySQL」或重启服务。",
          confirmText: "切到 SQLite",
          danger: true,
        });
        if (!ok) return;
        run(button, async () => {
          const data = await api.post("/api/admin/db/reconnect", { target: "fallback" });
          const rows = data.result?.mirroredRows || 0;
          paintDbCard(data.db);
          Toast.success(`已切到 SQLite${rows ? H`，镜像 ${rows} 行` : ""}`);
        });
      });
    }

    bindDbCard();

    panel.querySelector('[data-act="purge-logs"]')?.addEventListener("click", purgeLogs);
    panel.querySelector('[data-act="blob-gc"]')?.addEventListener("click", blobGc);

    const updateBtn = panel.querySelector('[data-act="check-update"]');
    const updateResult = panel.querySelector("[data-update-result]");
    updateBtn?.addEventListener("click", async () => {
      updateBtn.disabled = true;
      if (updateResult) {
        updateResult.hidden = false;
        updateResult.innerHTML = H`<span class="update-state">正在检查 GitHub…</span>`;
      }
      try {
        const data = await checkForUpdates(true);
        if (updateResult) updateResult.innerHTML = updateResultHtml(data);
      } catch (err) {
        if (updateResult) {
          updateResult.innerHTML = H`<span class="update-state is-error">检查失败：${escapeHtml(
            err.message || "网络不可达"
          )}</span>`;
        }
      } finally {
        updateBtn.disabled = false;
      }
    });

    const applyBtn = panel.querySelector('[data-act="apply-update"]');
    applyBtn?.addEventListener("click", async () => {
      const ok = await Dialog.confirm({
        title: "立即更新",
        message:
          "将从 GitHub 拉取 main 分支并重启服务，期间会短暂断线。<br>" +
          "要求当前在 <b>main</b> 分支且工作区没有未提交改动；重启后页面会自动重连。",
        confirmText: "开始更新",
        danger: true,
      });
      if (!ok) return;
      applyBtn.disabled = true;
      updateBtn.disabled = true;
      if (updateResult) {
        updateResult.hidden = false;
        updateResult.innerHTML = H`<span class="update-state">正在拉取并合并…</span>`;
      }
      try {
        const data = await api.post("/api/admin/update/apply", { restart: true });
        if (updateResult) updateResult.innerHTML = applyResultHtml(data);
        if (data.ok && data.updated) {
          Toast.info("更新完成，服务正在重启…");
          window.setTimeout(() => location.reload(), 9000);
        }
      } catch (err) {
        if (updateResult) {
          updateResult.innerHTML = H`<span class="update-state is-error">更新失败：${escapeHtml(
            err.message || "网络不可达"
          )}</span>`;
        }
      } finally {
        applyBtn.disabled = false;
        updateBtn.disabled = false;
      }
    });

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
        if (payload.autoUpdateCheck !== undefined) await refreshMeta();
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
            message: H`数据库登记 ${scan.knownBlobs} 个 blob，磁盘 ${scan.diskFiles} 个文件，没有发现孤儿文件。`,
          });
          return;
        }
        const ok = await Dialog.confirm({
          title: "发现孤儿文件",
          message: H`磁盘上有 <b>${scan.orphans}</b> 个未被数据库引用的文件（${formatBytes(
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
    else if (name === "feedback") showFeedback();
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
