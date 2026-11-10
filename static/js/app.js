/**
 * 应用入口：装配、路由、导航、装饰
 */
import { api, ApiError, formatBytes } from "./api.js";
import { H } from "./i18n.js";
import { applyStatic, initI18n, setLang, t, watchI18n } from "./i18n.js";
import { store } from "./store.js";
import {
  applyAppearance,
  applyCachedAppearance,
  dropCachedBackground,
  patchSettingsLocal,
  resolveTheme,
  watchSystemTheme,
} from "./theme.js";
import { startPetals, stopPetals } from "./petals.js";
import { Dialog } from "./ui/dialog.js";
import { avatarMarkup } from "./ui/avatar.js";
import { renderAnnounce } from "./ui/announce.js";
import { Toast } from "./ui/toast.js";
import { openMenu } from "./ui/menu.js";
import { installState, promptInstall, registerPwa } from "./pwa.js";
import { initLive2DMascot } from "./ui/live2d.js";
import { applyMascotLock, bindMascotActions } from "./ui/mascot-actions.js";
import { primeChatConfig, toggleMascotChat } from "./ui/mascot-chat.js";
import { checkForUpdates } from "./ui/update.js";
import { renderAuth } from "./views/auth.js";
import { renderAdmin } from "./views/admin.js";
import { renderAbout } from "./views/about.js";
import { renderFiles } from "./views/files.js";
import { renderSettings } from "./views/settings.js";
import { renderFeedback } from "./views/feedback.js";
import { renderShares } from "./views/shares.js";
import { renderTrash } from "./views/trash.js";

const NAV_ITEMS = [
  { group: "空间" },
  { route: "files", label: "我的文件", icon: "i-folder" },
  { route: "shares", label: "我的分享", icon: "i-share" },
  { route: "trash", label: "回收站", icon: "i-trash" },
  { group: "系统" },
  { route: "settings", label: "设置", icon: "i-settings" },
  { route: "about", label: "关于", icon: "i-info" },
  { route: "admin", label: "管理后台", icon: "i-shield", adminOnly: true },
];

const TITLES = {
  files: "我的文件",
  shares: "我的分享",
  trash: "回收站",
  settings: "设置",
  feedback: "意见反馈",
  about: "关于",
  admin: "管理后台",
};

const MASCOT_LINES = [
  "今天也一起整理文件吧～",
  "拖文件进来就能上传哦",
  "别忘了清一清回收站呀",
  "换个主题色会更好看呢",
  "累了就休息一下吧",
];

const el = (id) => document.getElementById(id);

/**
 * 登录后要回到哪里：分享页右上角的「登录 / 注册」会带 ?next=<分享页地址>。
 * 只接受站内相对路径（挡掉 //evil.com 这类跳转），并且只取一次，之后把参数从地址栏抹掉。
 */
function takeNextPath() {
  const raw = new URLSearchParams(location.search).get("next") || "";
  if (!raw.startsWith("/") || raw.startsWith("//")) return null;
  try {
    const url = new URL(location.href);
    url.searchParams.delete("next");
    url.searchParams.delete("mode");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
  } catch {
    /* 地址栏改不动也不影响跳转 */
  }
  return raw;
}

/* ------------------------------------------------------------------ */
/* 启动                                                               */
/* ------------------------------------------------------------------ */
async function boot() {
  initI18n();
  registerPwa();
  applyCachedAppearance();
  watchSystemTheme();
  // 后台改完站点设置（公告 / 站名）会广播一次，这里跟着刷新，不用刷新页面
  window.addEventListener("meta:change", (event) => {
    renderAnnounce(el("announce"), event.detail?.meta?.announcement);
  });

  let meta = null;
  try {
    meta = await api.get("/api/meta");
    store.set({ meta });
    el("brand-name").textContent = meta.appName || "CloudNETP";
    el("brand-version").textContent = `v${meta.version || "0.1.0"}`;
    document.title = `${meta.appName || "CloudNETP"} · ${t("樱色云盘")}`;
    renderAnnounce(el("announce"), meta.announcement);
  } catch {
    /* 元信息失败不阻塞登录 */
  }

  try {
    const data = await api.get("/api/me");
    enterApp(data.user);
    // 已登录但带着 next（比如从分享页点登录又发现本来就没掉登录态），回原页
    const next = takeNextPath();
    if (next) location.replace(next);
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      showAuth();
    } else {
      Toast.error(t("无法连接后端服务，请确认 uvicorn 已启动"));
      showAuth();
    }
  } finally {
    el("boot").hidden = true;
  }
}

function showAuth() {
  store.set({ user: null });
  el("shell").hidden = true;
  el("mascot").classList.add("is-hidden");
  const root = el("auth-root");
  root.hidden = false;
  stopPetals();
  startPetals();
  renderAuth(root, {
    meta: store.get().meta || {},
    onSuccess: (user, firstAdmin) => {
      root.hidden = true;
      root.innerHTML = "";
      enterApp(user);
      // 从分享页点「登录 / 注册」过来的，登录完直接回那页分享
      const next = takeNextPath();
      if (next) {
        location.replace(next);
        return;
      }
      if (firstAdmin) {
        Dialog.alert({
          title: t("你是第一位用户"),
          message: t("该账号已自动获得<strong>管理员</strong>权限，可以进入「管理后台」调整配额与系统设置。"),
        });
      } else {
        Toast.success(`欢迎回来，${user.username}`);
      }
    },
  });
}

function enterApp(user) {
  store.set({ user, navCollapsed: !!user.settings?.navCollapsed });
  // 账号里存的语言优先于本机设置（换设备也能跟上）
  if (user.settings?.language) {
    setLang(user.settings.language, { persist: true, silent: true });
  }
  el("auth-root").hidden = true;
  el("shell").hidden = false;
  // 登录进来再刷一次：公告可能是刚在后台改的
  renderAnnounce(el("announce"), store.get().meta?.announcement);

  applyAppearance(user.settings || {});
  paintUserChip(user);
  updateEmailTip(user);

  applyNavState(!!user.settings?.navCollapsed);
  renderNav();

  // 登录成功后如果地址栏还停在 #/login 这种非视图路由上，顺手归一化到 #/files，
  // 否则刷新页面时地址栏会一直留着一个不存在的路由
  const hashRoute = location.hash.replace(/^#\/?/, "");
  if (!location.hash || !location.hash.startsWith("#/") || !TITLES[hashRoute]) {
    location.hash = "#/files";
  }
  route();

  if (user.settings?.showPetals === false) stopPetals();
  else startPetals();

  setupMascot(user);
  updateThemeIcon();
  void autoCheckUpdates(user);
}

/**
 * 满足条件时在后台查一次新版本（管理员 + 系统设置开启），每天最多一次。
 * 只提示，不会改代码或重启；失败完全静默，不打扰使用。
 */
async function autoCheckUpdates(user) {
  if (user?.role !== 1 || !store.get().meta?.autoUpdateCheck) return;
  const now = Date.now();
  try {
    const last = Number(localStorage.getItem("cn:update-check-at")) || 0;
    if (now - last < 24 * 60 * 60 * 1000) return;
  } catch {
    return;
  }
  try {
    const data = await checkForUpdates(false);
    if (!data?.ok) return;
    // 只有检查成功才记时间；网络失败下次登录还能再试
    try {
      localStorage.setItem("cn:update-check-at", String(now));
    } catch {
      /* 忽略 */
    }
    if (!data.hasUpdate || !data.latest) return;
    let seen = "";
    try {
      seen = localStorage.getItem("cn:update-seen") || "";
    } catch {
      /* 忽略 */
    }
    if (seen === data.latest) return;
    try {
      localStorage.setItem("cn:update-seen", data.latest);
    } catch {
      /* 忽略 */
    }
    Toast.info(`发现新版本 v${data.latest}，可在「关于」页查看更新信息`);
  } catch {
    /* 网络不可达就静默跳过 */
  }
}

/* ------------------------------------------------------------------ */
/* 顶部用户区                                                         */
/* ------------------------------------------------------------------ */
function paintUserChip(user) {
  if (!user) return;
  const avatarEl = el("user-avatar");
  const nameEl = el("user-name");
  if (avatarEl) avatarEl.innerHTML = avatarMarkup(user);
  if (nameEl) nameEl.textContent = user.displayName || user.username;
  const chip = el("user-chip");
  if (chip) {
    chip.title = user.bio || `${user.displayName || user.username}（@${user.username}）`;
  }
}

/* ------------------------------------------------------------------ */
/* 导航                                                               */
/* ------------------------------------------------------------------ */
function renderNav() {
  const nav = el("nav");
  const { user, route: current } = store.get();
  const quota = user?.quotaBytes || 0;
  const used = user?.usedBytes || 0;
  const pct = quota ? Math.min(100, Math.round((used / quota) * 100)) : 0;

  nav.innerHTML = H`
    ${NAV_ITEMS.map((item) => {
      if (item.group) return H`<div class="nav__group">${t(item.group)}</div>`;
      if (item.adminOnly && user?.role !== 1) return "";
      return H`
        <a class="nav-item ${current === item.route ? "is-active" : ""}"
           href="#/${item.route}" data-route="${item.route}" data-tip="${t(item.label)}"
           aria-label="${t(item.label)}" ${current === item.route ? 'aria-current="page"' : ""}>
          <svg class="icon"><use href="#${item.icon}"/></svg>
          <span class="nav-item__label">${t(item.label)}</span>
        </a>`;
    }).join("")}
    <div class="nav__spacer"></div>
    <div class="nav__storage">
      <div class="nav__storage-title">
        <span>${t("存储空间")}</span>
        <span class="nav__storage-num">${pct}%</span>
      </div>
      <div class="progress"><div class="progress__bar" style="width:${pct}%"></div></div>
      <div class="nav__storage-title" style="margin:8px 0 0">
        <span class="nav__storage-num">${formatBytes(used)}</span>
        <span class="nav__storage-num">${formatBytes(quota)}</span>
      </div>
    </div>`;
}

function applyNavState(collapsed) {
  el("app-body").dataset.nav = collapsed ? "collapsed" : "expanded";
  store.set({ navCollapsed: collapsed });
  el("nav-toggle").title = collapsed ? t("展开导航") : t("折叠导航");
}

let collapseTimer = 0;
function setupNavToggle() {
  el("nav-toggle").addEventListener("click", () => {
    const next = !store.get().navCollapsed;
    applyNavState(next);
    window.clearTimeout(collapseTimer);
    collapseTimer = window.setTimeout(() => {
      api.patch("/api/me/settings", { navCollapsed: next }).catch(() => {});
    }, 400);
  });
}

/* ------------------------------------------------------------------ */
/* 路由                                                               */
/* ------------------------------------------------------------------ */
const PLACEHOLDERS = {
  trash: {
    icon: "i-trash",
    title: "回收站是空的",
    desc: "删除的文件会先进入这里，可还原或彻底删除。",
  },
  admin: {
    icon: "i-shield",
    title: "管理后台",
    desc: "用户管理、配额调整、存储统计与系统设置将在 M7 阶段接入。",
  },
};

function renderPlaceholder(root, { icon, title, desc }) {
  root.innerHTML = H`
    <div class="view-head">
      <div class="view-title"><svg class="icon"><use href="#${icon}"/></svg>${t(title)}</div>
    </div>
    <div class="view-body">
      <div class="empty">
        <div class="empty__art"><svg class="icon"><use href="#${icon}"/></svg></div>
        <div class="empty__title">${t(title)}</div>
        <div class="empty__desc">${t(desc)}</div>
      </div>
    </div>`;
}

function currentRoute() {
  const hash = location.hash.replace(/^#\/?/, "");
  return TITLES[hash] ? hash : "files";
}

function route() {
  const { user } = store.get();
  if (!user) return;
  const name = currentRoute();
  store.set({ route: name });

  if (name === "admin" && user.role !== 1) {
    location.hash = "#/files";
    return;
  }

  const root = el("view-root");
  root.innerHTML = "";

  if (name === "files") renderFiles(root, { user });
  else if (name === "shares") renderShares(root, { user });
  else if (name === "trash") renderTrash(root, { user });
  else if (name === "settings") renderSettings(root);
  else if (name === "feedback") renderFeedback(root);
  else if (name === "about") renderAbout(root);
  else if (name === "admin") renderAdmin(root);
  else renderPlaceholder(root, PLACEHOLDERS.admin);

  document.title = `${t(TITLES[name])} · ${store.get().meta?.appName || "CloudNETP"}`;
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("is-active", item.dataset.route === name);
  });
}

/* ------------------------------------------------------------------ */
/* 主题切换                                                           */
/* ------------------------------------------------------------------ */
function updateThemeIcon() {
  const { user } = store.get();
  const mode = user?.settings?.themeMode ?? 2;
  const resolved = resolveTheme(mode);
  const use = el("theme-toggle").querySelector("use");
  use.setAttribute("href", resolved === "dark" ? "#i-sun" : "#i-moon");
  el("theme-toggle").title = resolved === "dark" ? t("切换到亮色") : t("切换到暗色");
}

function setupThemeToggle() {
  el("theme-toggle").addEventListener("click", () => {
    const resolved = resolveTheme(store.get().user?.settings?.themeMode ?? 2);
    const nextMode = resolved === "dark" ? 0 : 1;
    patchSettingsLocal({ themeMode: nextMode });
    updateThemeIcon();
    api.patch("/api/me/settings", { themeMode: nextMode }).catch(() => {});
  });
}

/* ------------------------------------------------------------------ */
/* 用户菜单                                                           */
/* ------------------------------------------------------------------ */
/** 用户菜单里的「安装」入口：只在浏览器给出安装能力时出现 */
function installMenuItems() {
  const state = installState();
  if (state.canInstall) {
    return [
      {
        label: t("安装到桌面"),
        icon: "i-plus",
        onClick: async () => {
          const outcome = await promptInstall();
          if (outcome === "accepted") Toast.success(t("安装完成后会以独立窗口打开"));
        },
      },
    ];
  }
  if (state.iosManual) {
    return [
      {
        label: t("添加到主屏幕…"),
        icon: "i-plus",
        onClick: () =>
          Dialog.alert({
            title: t("添加到主屏幕"),
            message:
              t("在 Safari 底部工具栏点「分享」按钮，选择「添加到主屏幕」，就能像 App 一样全屏打开小云盘。"),
          }),
      },
    ];
  }
  return [];
}

function setupUserMenu() {
  const chip = el("user-chip");
  chip.addEventListener("click", () => {
    const { user } = store.get();
    if (!user) return;
    openMenu({
      anchor: chip,
      align: "end",
      header: {
        name: user.displayName || user.username,
        meta: [
          `@${user.username}`,
          `${formatBytes(user.usedBytes)} / ${formatBytes(user.quotaBytes)}`,
          user.role === 1 ? t("管理员") : t("普通用户"),
          user.bio ? user.bio.slice(0, 40) : "",
        ]
          .filter(Boolean)
          .join(" · "),
      },
      items: [
        ...installMenuItems(),
        { label: t("设置"), icon: "i-settings", onClick: () => (location.hash = "#/settings") },
        ...(user.role === 1
          ? [{ label: t("管理后台"), icon: "i-shield", onClick: () => (location.hash = "#/admin") }]
          : []),
        { sep: true },
        { label: t("退出登录"), icon: "i-logout", danger: true, onClick: logout },
      ],
    });
  });
}

async function logout() {
  const ok = await Dialog.confirm({
    title: t("退出登录"),
    message: t("确定要退出当前账号吗？"),
    confirmText: t("退出"),
  });
  if (!ok) return;
  try {
    await api.post("/api/auth/logout");
  } catch {
    /* 忽略 */
  }
  stopPetals();
  dropCachedBackground();
  showAuth();
}

/* ------------------------------------------------------------------ */
/* 未绑定邮箱提醒（可永久关闭）                                        */
/* ------------------------------------------------------------------ */
const EMAIL_TIP_KEY = "cloudnetp.emailTipDismissed";

function emailTipDismissed() {
  try {
    return localStorage.getItem(EMAIL_TIP_KEY) === "1";
  } catch {
    return false;
  }
}

function updateEmailTip(user) {
  const tip = el("email-tip");
  if (!tip) return;
  tip.hidden = !(user?.needEmail && !emailTipDismissed());
}

function setupEmailTip() {
  const tip = el("email-tip");
  if (!tip) return;
  tip.querySelector("[data-email-go]").addEventListener("click", () => {
    location.hash = "#/settings";
  });
  tip.querySelector("[data-email-dismiss]").addEventListener("click", () => {
    try {
      localStorage.setItem(EMAIL_TIP_KEY, "1");
    } catch {
      /* 隐私模式下写不进去也没关系，本次会话仍然隐藏 */
    }
    tip.hidden = true;
    Toast.info(t("已关闭提醒，需要时可在设置里绑定邮箱"));
  });
}

/* ------------------------------------------------------------------ */
/* 看板娘（Live2D，没有模型时退回云朵 SVG）                            */
/* ------------------------------------------------------------------ */
let mascotTimer = 0;
let bubbleTimer = 0;
let mascotLive = null;

function saySomething() {
  const bubble = el("mascot-bubble");
  if (!bubble) return;
  bubble.textContent = t(MASCOT_LINES[(Math.random() * MASCOT_LINES.length) | 0]);
  bubble.classList.add("is-on");
  window.clearTimeout(bubbleTimer);
  bubbleTimer = window.setTimeout(() => bubble.classList.remove("is-on"), 5200);
}

function setupMascot(user) {
  const mascot = el("mascot");
  const enabled = user.settings?.showMascot !== false;
  mascot.classList.toggle("is-hidden", !enabled);
  applyMascotLock();

  if (!mascotLive) {
    mascotLive = initLive2DMascot({
      host: mascot,
      stage: el("mascot-stage"),
      pick: el("mascot-pick"),
      pickLabel: el("mascot-pick-label"),
      onPoke: () => saySomething(),
    });
  }

  bindMascotActions({
    onChat: () => toggleMascotChat(),
    onClose: () => hideMascot(),
  });
  // 先问一次 AI 总开关：关掉时把「对话」按钮收起来（拉不到就按开启处理）
  primeChatConfig();

  mascotLive?.setActive(enabled);
  if (!enabled) return;

  window.clearTimeout(mascotTimer);
  mascotTimer = window.setTimeout(saySomething, 1400);
}

/** 看板娘操作条上的「关闭」：收起并记进用户设置，下次登录也不再冒出来 */
function hideMascot() {
  el("mascot")?.classList.add("is-hidden");
  const user = store.get().user;
  if (user) {
    patchSettingsLocal({ showMascot: false });
    api.patch("/api/me/settings", { showMascot: false }).catch(() => {
      Toast.warn(t("已经收起来了，但同步到服务器失败"));
    });
  }
  Toast.info(t("看板娘已收起，可在「设置 → 装饰」里重新打开"));
}

/* ------------------------------------------------------------------ */
/* 全局事件                                                           */
/* ------------------------------------------------------------------ */
// 语言切换：导航、静态壳、当前视图全部按新语言重画（视图模板里都走 t()/H）
watchI18n(() => {
  applyStatic();
  renderNav();
  const { user } = store.get();
  if (user) route();
});

window.addEventListener("session:expired", () => {
  if (store.get().user) {
    Toast.warn(t("登录状态已失效，请重新登录"));
    showAuth();
  }
});

// 文件页改动配额后，刷新导航栏的存储条
window.addEventListener("user:refresh", (event) => {
  const user = event.detail;
  if (!user) return;
  store.set({ user });
  paintUserChip(user);
  updateEmailTip(user);
  renderNav();
});

window.addEventListener("hashchange", route);
window.addEventListener("resize", () => {
  const seg = document.querySelector(".segmented__thumb");
  if (seg) {
    const btn = seg.parentElement.querySelector(".segmented__btn.is-active");
    if (btn) {
      seg.style.width = `${btn.offsetWidth}px`;
      seg.style.transform = `translateX(${btn.offsetLeft - 3}px)`;
    }
  }
});

setupNavToggle();
setupThemeToggle();
setupUserMenu();
setupEmailTip();
boot();
