/**
 * 设置视图：外观 / 装饰 / 账号
 */
import { api, escapeHtml, formatBytes } from "../api.js";
import { passwordProblem } from "../password.js";
import { store, siteOrigin } from "../store.js";
import { Toast } from "../ui/toast.js";
import { Dialog } from "../ui/dialog.js";
import { avatarMarkup } from "../ui/avatar.js";
import {
  ACCENT_PRESETS,
  WALLPAPERS,
  applyAppearance,
  dominantColor,
  patchSettingsLocal,
  rememberBackgroundLuma,
} from "../theme.js";
import { getMascotScale, setMascotScale } from "../ui/live2d.js";
import { isMascotLocked, setMascotLocked } from "../ui/mascot-actions.js";
import { mountAiSettings } from "../ui/ai-config.js";

const THEME_MODES = [
  { value: 0, label: "亮色" },
  { value: 1, label: "暗色" },
  { value: 2, label: "跟随系统" },
];

async function persist(patch) {
  const local = patchSettingsLocal(patch);
  if (!local) return;
  try {
    await api.patch("/api/me/settings", patch);
  } catch {
    Toast.error("设置已应用，但同步到服务器失败");
  }
}

/** 回收站开关的说明文案 */
function trashDesc(s) {
  const days = Number(store.get().meta?.trashRetentionDays) || 30;
  if (s?.trashEnabled === false) return "已关闭：删除会直接彻底删除，无法恢复";
  return `开启中：删除的文件先进入回收站，${days} 天后自动清理`;
}

/* 各模块背景的默认浓度：直接读主题 CSS 变量，换主题/壁纸后默认值自然跟着走 */
const SURFACE_FALLBACK = { nav: 42, titlebar: 46, panel: 62, dialog: 82 };
const SURFACE_VARS = { nav: "--nav-a", titlebar: "--title-a", panel: "--panel-a", dialog: "--dialog-a" };
const clampPercent = (v) => Math.max(0, Math.min(100, Math.round(Number(v) || 0)));
function surfaceDefault(key) {
  const raw = parseFloat(
    getComputedStyle(document.documentElement).getPropertyValue(SURFACE_VARS[key] || "")
  );
  return Number.isFinite(raw) ? Math.round(raw * 100) : SURFACE_FALLBACK[key] ?? 50;
}

/* ------------------------------------------------------------------ */
/* 头像：本地裁成正方形再上传（省流量，也避免服务端收到巨图）           */
/* ------------------------------------------------------------------ */
function loadBitmap(file) {
  if (window.createImageBitmap) {
    return createImageBitmap(file).catch(() => loadViaImg(file));
  }
  return loadViaImg(file);
}

function loadViaImg(file) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve(img);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("图片读取失败，请换一张试试"));
    };
    img.src = url;
  });
}

async function squareBlob(file, side = 256) {
  const bmp = await loadBitmap(file);
  const canvas = document.createElement("canvas");
  canvas.width = side;
  canvas.height = side;
  const ctx = canvas.getContext("2d");
  const w = bmp.width || bmp.naturalWidth;
  const h = bmp.height || bmp.naturalHeight;
  const edge = Math.min(w, h);
  ctx.drawImage(bmp, (w - edge) / 2, (h - edge) / 2, edge, edge, 0, 0, side, side);
  bmp.close?.();
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/webp", 0.92));
  return blob || file;
}

/* ------------------------------------------------------------------ */
/* 背景图：超大图先在本地缩到长边 2560 再传（省流量，服务端还会再兜一次） */
/* ------------------------------------------------------------------ */
const BG_MAX_SIDE = 2560;
const BG_INLINE_LIMIT = 4 * 1024 * 1024;

function loadImageUrl(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("图片读取失败"));
    img.src = url;
  });
}

async function prepareBackground(file) {
  const bmp = await loadBitmap(file);
  const w = bmp.width || bmp.naturalWidth;
  const h = bmp.height || bmp.naturalHeight;
  const long = Math.max(w, h);
  // 体积和尺寸都能接受就发原图，避免二次压缩
  if (file.size <= BG_INLINE_LIMIT && long <= BG_MAX_SIDE) {
    return { blob: file, bitmap: bmp };
  }
  const scale = Math.min(1, BG_MAX_SIDE / long);
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(w * scale));
  canvas.height = Math.max(1, Math.round(h * scale));
  const ctx = canvas.getContext("2d");
  ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/webp", 0.9));
  return { blob: blob || file, bitmap: bmp };
}

export function renderSettings(root) {
  const { user } = store.get();
  const s = user?.settings || {};
  const mode = s.themeMode ?? 2;
  const accent = (s.accentColor || "#c084fc").toLowerCase();
  const isPreset = ACCENT_PRESETS.some((c) => c.value.toLowerCase() === accent);
  // custom 但没图（比如别的设备删掉了）就按默认壁纸显示，别留一片空白
  const wallpaper = s.wallpaper === "custom" && !s.backgroundImage ? "sakura" : s.wallpaper || "sakura";
  const displayName = (user?.displayName || user?.username || "").trim();
  const maskedPreview = displayName.slice(0, 2) || "—";
  const savedOpacity = s.surfaceOpacity || {};
  const mascotScale = getMascotScale();
  const opacityRows = [
    ["nav", "侧栏"],
    ["titlebar", "标题栏"],
    ["panel", "内容面板"],
    ["dialog", "弹窗"],
  ].map(([key, name]) => {
    const saved = Number(savedOpacity[key]);
    return { key, name, value: Number.isFinite(saved) ? clampPercent(saved) : surfaceDefault(key) };
  });

  root.innerHTML = `
    <div class="view-head">
      <div class="view-title"><svg class="icon"><use href="#i-settings"/></svg>设置</div>
      <span class="view-sub">外观与账号</span>
    </div>
    <div class="view-body">
      <div class="settings">

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-palette"/></svg>外观</div>
          <div class="settings-card__body">
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">主题模式</div>
                <div class="setting-row__desc">跟随系统时会随 Windows 深色模式自动切换</div>
              </div>
              <div class="segmented segmented--wide" data-theme-seg>
                <div class="segmented__thumb" data-thumb></div>
                ${THEME_MODES.map(
                  (m) =>
                    `<button class="segmented__btn ${m.value === mode ? "is-active" : ""}" type="button" data-mode="${m.value}">${m.label}</button>`
                ).join("")}
              </div>
            </div>
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">主题色</div>
                <div class="setting-row__desc">用于选中状态、按钮与进度条；也可以从背景图里取</div>
              </div>
              <div class="accent-tools">
                <div class="swatch-row" data-swatches>
                  ${ACCENT_PRESETS.map(
                    (c) =>
                      `<button class="swatch ${c.value === accent ? "is-active" : ""}"
                         type="button" data-color="${c.value}" title="${c.name}"
                         style="background:${c.value}"></button>`
                  ).join("")}
                  <label class="swatch swatch--pick ${isPreset ? "" : "is-active"}"
                         data-swatch-pick title="自定义颜色"
                         style="${isPreset ? "" : `background:${escapeHtml(accent)}`}">
                    <input type="color" value="${escapeHtml(accent)}" data-color-pick aria-label="自定义主题色">
                  </label>
                </div>
                <button class="btn btn--subtle btn--sm accent-tools__pick" type="button"
                        data-act="pick-bg" ${s.backgroundImage ? "" : "disabled"}>
                  <svg class="icon icon--s"><use href="#i-image"/></svg>从背景取色
                </button>
              </div>
            </div>
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">背景壁纸</div>
                <div class="setting-row__desc">云母（Mica）底层的取色来源；也可以换成自己的图片</div>
              </div>
              <div class="wallpaper-row" data-wallpapers>
                ${WALLPAPERS.map(
                  (w) =>
                    `<button class="wallpaper-opt wp-${w.id} ${w.id === wallpaper ? "is-active" : ""}"
                       type="button" data-wallpaper="${w.id}">
                       ${w.id === "plain" ? '<svg class="icon icon--s"><use href="#i-close"/></svg>' : ""}
                       <span class="wallpaper-opt__name">${w.name}</span>
                     </button>`
                ).join("")}
                <button class="wallpaper-opt wp-custom ${wallpaper === "custom" ? "is-active" : ""}"
                        type="button" data-wallpaper="custom" data-bg-tile title="上传自己的背景图">
                  <span class="wp-custom__thumb" data-bg-thumb
                        ${s.backgroundImage ? `style="background-image:url('${escapeHtml(s.backgroundImage)}')"` : ""}></span>
                  <svg class="icon icon--s wp-custom__plus" data-bg-plus ${s.backgroundImage ? "hidden" : ""}><use href="#i-plus"/></svg>
                  <span class="wallpaper-opt__name" data-bg-name>${s.backgroundImage ? "自定义" : "上传图片"}</span>
                  <span class="wp-custom__busy" data-bg-busy hidden><span class="spinner"></span></span>
                </button>
              </div>
            </div>
            <div class="wallpaper-actions" data-bg-actions ${s.backgroundImage ? "" : "hidden"}>
              <button class="btn btn--subtle btn--sm" type="button" data-act="bg-replace">换一张</button>
              <button class="btn btn--subtle btn--sm" type="button" data-act="bg-remove">移除图片</button>
              <span class="muted" data-bg-note>上传后会从图片里取出现最多的颜色当主题色</span>
            </div>
            <input type="file" accept="image/*" hidden data-bg-input>
            <div class="setting-row setting-row--opacity">
              <div class="setting-row__main">
                <div class="setting-row__label">模块不透明度</div>
                <div class="setting-row__desc">分别控制各块背景的实心程度，0% 完全透明、100% 完全不透明</div>
              </div>
              <div class="opacity-list" data-opacity-list>
                ${opacityRows
                  .map(
                    (r) => `<label class="opacity-item">
                      <span class="opacity-item__name">${r.name}</span>
                      <input type="range" min="0" max="100" step="1" value="${r.value}"
                             data-opacity="${r.key}" style="--fill:${r.value}%"
                             aria-label="${r.name}不透明度">
                      <span class="opacity-item__val" data-opacity-val="${r.key}">${r.value}%</span>
                    </label>`
                  )
                  .join("")}
                <div class="opacity-foot">
                  <span class="muted">调低后壁纸会透出来，文字对比度也会下降</span>
                  <button class="btn btn--subtle btn--sm" type="button" data-act="opacity-reset">恢复默认</button>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-star"/></svg>装饰</div>
          <div class="settings-card__body">
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">花瓣飘落</div>
                <div class="setting-row__desc">极淡的樱瓣粒子，系统开启「减少动态效果」时自动停用</div>
              </div>
              <label class="switch">
                <input type="checkbox" data-toggle="showPetals" ${s.showPetals === false ? "" : "checked"}>
                <span class="switch__track"><span class="switch__knob"></span></span>
              </label>
            </div>
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">看板娘（Live2D）</div>
                <div class="setting-row__desc">右下角的 Live2D 模型；把模型文件夹放进服务器的
                  <code>live2d/model/</code> 就会自动出现，不用改代码<span data-live2d-hint></span></div>
              </div>
              <label class="switch">
                <input type="checkbox" data-toggle="showMascot" ${s.showMascot === false ? "" : "checked"}>
                <span class="switch__track"><span class="switch__knob"></span></span>
              </label>
            </div>
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">看板娘大小</div>
                <div class="setting-row__desc">拖动滑块缩放；看板娘本身也可以直接拖到任意位置，大小与位置按设备记住</div>
              </div>
              <div class="opacity-list mascot-size-list">
                <label class="opacity-item">
                  <span class="opacity-item__name">缩放</span>
                  <input type="range" min="60" max="180" step="5" value="${mascotScale}" data-mascot-size
                         style="--fill:${Math.round(((mascotScale - 60) / 120) * 100)}%"
                         aria-label="看板娘大小">
                  <span class="opacity-item__val" data-mascot-size-val>${mascotScale}%</span>
                </label>
              </div>
            </div>
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">锁定看板娘（鼠标穿透）</div>
                <div class="setting-row__desc">锁定后鼠标直接穿过她，不会挡住下面的文件；也解锁在这里</div>
              </div>
              <label class="switch">
                <input type="checkbox" data-mascot-lock ${isMascotLocked() ? "checked" : ""}>
                <span class="switch__track"><span class="switch__knob"></span></span>
              </label>
            </div>
          </div>
        </section>

        <section class="settings-card" data-ai-card>
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-chat"/></svg>看板娘 · 对话设置</div>
          <div class="settings-card__note">
            给她接上大模型，右下角就能聊两句。密钥只存在你自己的账号里（服务端加密保存，接口只回掩码）；
            填好地址和密钥后点「测试连接」会自动拉取模型列表
          </div>
          <div class="settings-card__body" data-ai-body>
            <div class="settings-card__note settings-loading"><span class="spinner"></span> 正在读取配置…</div>
          </div>
        </section>

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-trash"/></svg>文件</div>
          <div class="settings-card__body">
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">回收站</div>
                <div class="setting-row__desc" data-trash-desc>${trashDesc(s)}</div>
              </div>
              <label class="switch">
                <input type="checkbox" data-toggle="trashEnabled" ${s.trashEnabled === false ? "" : "checked"}>
                <span class="switch__track"><span class="switch__knob"></span></span>
              </label>
            </div>
          </div>
        </section>

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-database"/></svg>WebDAV</div>
          <div class="settings-card__body">
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">挂载地址</div>
                <div class="setting-row__desc">资源管理器「映射网络驱动器」、Finder「连接服务器」都填这个地址</div>
              </div>
              <div class="dav-addr">
                <code data-dav-url>—</code>
                <button class="btn btn--subtle" type="button" data-act="dav-copy">复制</button>
              </div>
            </div>
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">登录凭据<span
                  class="chip chip--accent chip--action" role="button" tabindex="0" data-act="dav-activate"
                  ${user?.davReady ? "hidden" : ""}>待激活</span></div>
                <div class="setting-row__desc">用户名 <b data-dav-user>—</b> · 密码就是当前账号的登录密码<span
                  data-dav-hint ${user?.davReady ? "hidden" : ""}><br>点右侧「待激活」输入一次密码即可启用，不用重新登录</span></div>
              </div>
              <button class="btn" type="button" data-act="dav-help">连接方法</button>
            </div>
          </div>
        </section>

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-share"/></svg>分享</div>
          <div class="settings-card__body">
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">分享页显示完整昵称</div>
                <div class="setting-row__desc">
                  关闭时别人只能看到昵称前两位（当前显示为「${escapeHtml(maskedPreview)}」），头像也不会外露
                </div>
              </div>
              <label class="switch">
                <input type="checkbox" data-toggle="shareShowName" ${s.shareShowName ? "checked" : ""}>
                <span class="switch__track"><span class="switch__knob"></span></span>
              </label>
            </div>
          </div>
        </section>

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-user"/></svg>账号</div>
          <div class="settings-card__body">

            <div class="profile">
              <button class="profile__avatar" type="button" data-act="avatar" title="更换头像">
                <span class="profile__avatar-inner" data-avatar>${avatarMarkup(user)}</span>
                <span class="profile__avatar-mask">
                  <svg class="icon icon--s"><use href="#i-image"/></svg>
                  <span>更换</span>
                </span>
                <span class="profile__spinner" data-avatar-busy hidden><span class="spinner"></span></span>
              </button>
              <div class="profile__fields">
                <label class="field">
                  <span class="field__label">昵称</span>
                  <span class="textbox">
                    <input type="text" data-field="displayName" maxlength="32" autocomplete="nickname"
                           value="${escapeHtml(user?.displayName || user?.username || "")}">
                  </span>
                </label>
                <label class="field">
                  <span class="field__label">简介</span>
                  <span class="textbox">
                    <input type="text" data-field="bio" maxlength="200" autocomplete="off"
                           placeholder="一句话介绍自己（最多 200 字）" value="${escapeHtml(user?.bio || "")}">
                  </span>
                </label>
                <div class="profile__foot">
                  <span class="muted">登录名 <b>${escapeHtml(user?.username || "—")}</b> 不可修改 ·
                    已用 ${formatBytes(user?.usedBytes || 0)} / ${formatBytes(user?.quotaBytes || 0)}</span>
                  <span style="flex:1"></span>
                  <button class="btn btn--subtle" type="button" data-act="avatar-clear"
                          ${user?.avatarUrl ? "" : "hidden"}>移除头像</button>
                  <button class="btn btn--accent" type="button" data-act="profile-save">保存资料</button>
                </div>
              </div>
              <input type="file" accept="image/*" hidden data-avatar-input>
            </div>

            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">邮箱</div>
                <div class="setting-row__desc" data-email-desc>${
                  user?.email
                    ? `${escapeHtml(user.email)}${user.emailVerified ? "" : "（未验证）"} · 可用于登录和找回密码`
                    : "还没有绑定 · 忘记密码时只能联系管理员"
                }</div>
              </div>
              <button class="btn" type="button" data-act="email">${user?.email ? "换绑" : "绑定邮箱"}</button>
            </div>
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">登录密码</div>
                <div class="setting-row__desc">修改后其它设备上的登录状态会立即失效</div>
              </div>
              <button class="btn" type="button" data-act="password">修改密码</button>
            </div>
            <a class="setting-row setting-row--link" href="#/about">
              <div class="setting-row__main">
                <div class="setting-row__label">关于</div>
                <div class="setting-row__desc" data-version>CloudNETP</div>
              </div>
              <span class="chip">${user?.role === 1 ? "管理员" : "普通用户"}</span>
              <svg class="icon icon--s setting-row__chevron"><use href="#i-chevron"/></svg>
            </a>
          </div>
        </section>

      </div>
    </div>
  `;

  root.querySelector("[data-version]").textContent =
    `${store.get().meta?.appName || "CloudNETP"} v${store.get().meta?.version || "0.1.0"}`;

  /* --- WebDAV 挂载 --- */
  const davUrl = `${siteOrigin()}/dav/`;
  const davHost = davUrl.replace(/^https?:\/\//, "");
  // UNC 写法：\\主机@端口\dav（资源管理器的「映射网络驱动器」也吃这个）
  const davUnc = (() => {
    try {
      const url = new URL(davUrl);
      const port = url.port || (url.protocol === "https:" ? 443 : 80);
      return `\\\\${url.hostname}@${port}\\dav`;
    } catch {
      return davHost;
    }
  })();
  root.querySelector("[data-dav-url]").textContent = davHost;
  root.querySelector("[data-dav-user]").textContent = user?.username || "—";

  root.querySelector('[data-act="dav-activate"]')?.addEventListener("click", async () => {
    const values = await Dialog.form({
      title: "启用 WebDAV",
      message:
        "Windows 的 WebDAV 客户端只接受 Digest 认证，需要用你的密码生成一份凭据。<br>这里只做一次校验，不会保存明文。",
      confirmText: "启用",
      fields: [
        { name: "password", label: "当前密码", type: "password", placeholder: "登录密码" },
      ],
    });
    if (!values?.password) return;
    try {
      await api.post("/api/me/dav/activate", { password: values.password });
      const fresh = await api.get("/api/me");
      store.set({ user: fresh.user });
      const badge = root.querySelector('[data-act="dav-activate"]');
      const hint = root.querySelector("[data-dav-hint]");
      if (badge) badge.hidden = true;
      if (hint) hint.hidden = true;
      Toast.success("WebDAV 已启用，现在可以挂载了");
    } catch (err) {
      Toast.error(err.message);
    }
  });

  root.querySelector('[data-act="dav-copy"]')?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(davUrl);
      Toast.success("挂载地址已复制");
    } catch {
      Toast.error("复制失败，请手动选中地址");
    }
  });

  /* --- 绑定邮箱 --- */
  root.querySelector('[data-act="email"]')?.addEventListener("click", async () => {
    const values = await Dialog.form({
      title: user?.email ? "换绑邮箱" : "绑定邮箱",
      message:
        "邮箱用来登录和找回密码。邮件服务还没接入，现在绑定的邮箱会标记为「未验证」。",
      confirmText: "保存",
      fields: [
        {
          name: "email",
          label: "邮箱地址",
          type: "email",
          placeholder: "you@example.com",
          value: user?.email || "",
        },
      ],
    });
    if (!values?.email) return;
    try {
      const res = await api.patch("/api/me/email", { email: values.email.trim() });
      store.set({ user: res.user });
      window.dispatchEvent(new CustomEvent("user:refresh", { detail: res.user }));
      root.querySelector("[data-email-desc]").textContent = `${res.user.email}（未验证） · 可用于登录和找回密码`;
      const btn = root.querySelector('[data-act="email"]');
      if (btn) btn.textContent = "换绑";
      Toast.success("邮箱已保存");
    } catch (err) {
      Toast.error(err.message);
    }
  });

  root.querySelector('[data-act="dav-help"]')?.addEventListener("click", () => {
    Dialog.alert({
      title: "连接 WebDAV",
      tone: "info",
      confirmText: "知道了",
      message: `
        <div class="dav-help">
          <div class="dav-help__row"><b>Windows</b>资源管理器右键「此电脑」→ 映射网络驱动器 →
            文件夹填 <code>${davUrl}</code>，或直接写 <code>${davUnc}</code>；
            勾选「使用其他凭据连接」，账号密码见上一行。</div>
          <div class="dav-help__row"><b>macOS</b>Finder → 前往 → 连接服务器（⌘K）→
            输入 <code>${davUrl}</code> → 以「注册用户」登录。</div>
          <div class="dav-help__row"><b>手机</b>Documents / RaiDrive / ES 文件浏览器里新增 WebDAV 连接，地址同上。</div>
          <div class="dav-help__row dav-help__note">Windows 的 WebDAV 客户端默认不允许在 HTTP 上发送
            明文 Basic 密码（会报 <code>0x80070043</code>），本站已改用 Digest 认证，<b>不需要改注册表</b>。
            公网使用仍建议套 HTTPS；通过 WebDAV 删除的文件同样会进回收站。</div>
        </div>`,
    });
  });

  /* --- 主题模式 --- */
  const seg = root.querySelector("[data-theme-seg]");
  const thumb = root.querySelector("[data-thumb]");
  const segBtns = [...seg.querySelectorAll("[data-mode]")];

  function layoutThumb() {
    const active = segBtns.find((b) => b.classList.contains("is-active"));
    if (!active) return;
    thumb.style.width = `${active.offsetWidth}px`;
    thumb.style.transform = `translateX(${active.offsetLeft - 3}px)`;
  }

  segBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      const value = Number(btn.dataset.mode);
      segBtns.forEach((b) => b.classList.toggle("is-active", b === btn));
      layoutThumb();
      persist({ themeMode: value });
    });
  });

  /* --- 主题色：预设 / 自定义取色器 / 从背景取色 --- */
  const pickInput = root.querySelector("[data-color-pick]");
  const pickSwatch = root.querySelector("[data-swatch-pick]");
  const pickFromBg = root.querySelector('[data-act="pick-bg"]');

  function markAccent(color) {
    const hex = (color || "").toLowerCase();
    const preset = ACCENT_PRESETS.find((c) => c.value.toLowerCase() === hex);
    root.querySelectorAll("[data-color]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.color.toLowerCase() === hex);
    });
    if (pickSwatch) {
      pickSwatch.classList.toggle("is-active", !preset);
      pickSwatch.style.background = preset ? "" : hex;
    }
    if (pickInput) pickInput.value = hex;
  }

  function applyAccent(color) {
    const hex = String(color || "").toLowerCase();
    if (!/^#[0-9a-f]{6}$/.test(hex)) return;
    markAccent(hex);
    persist({ accentColor: hex });
  }

  root.querySelectorAll("[data-color]").forEach((btn) => {
    btn.addEventListener("click", () => applyAccent(btn.dataset.color));
  });

  // 取色器拖动时只本地预览，松手（change）才落库，避免一路刷接口
  pickInput?.addEventListener("input", () => {
    const hex = pickInput.value.toLowerCase();
    markAccent(hex);
    patchSettingsLocal({ accentColor: hex });
  });
  pickInput?.addEventListener("change", () => applyAccent(pickInput.value));

  pickFromBg?.addEventListener("click", async () => {
    const url = store.get().user?.settings?.backgroundImage;
    if (!url) {
      Toast.error("先上传一张背景图");
      return;
    }
    pickFromBg.disabled = true;
    try {
      const img = await loadImageUrl(url);
      const color = dominantColor(img);
      if (!color) {
        Toast.error("这张图取不出颜色，换一张试试");
        return;
      }
      applyAccent(color);
      Toast.success(`已取到 ${color}，也可以自己挑一个`);
    } catch {
      Toast.error("背景图读取失败，请重新上传");
    } finally {
      pickFromBg.disabled = false;
    }
  });

  /* --- 壁纸：预设 / 自定义图片 --- */
  const wpButtons = [...root.querySelectorAll("[data-wallpaper]")];
  const bgInput = root.querySelector("[data-bg-input]");
  const bgTile = root.querySelector("[data-bg-tile]");
  const bgBusy = root.querySelector("[data-bg-busy]");
  const bgThumb = root.querySelector("[data-bg-thumb]");
  const bgName = root.querySelector("[data-bg-name]");
  const bgActions = root.querySelector("[data-bg-actions]");

  function selectWallpaper(id) {
    wpButtons.forEach((b) => b.classList.toggle("is-active", b.dataset.wallpaper === id));
    persist({ wallpaper: id });
  }

  function openBgPicker() {
    bgInput?.click();
  }

  wpButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.wallpaper;
      // 还没图时点「自定义」= 直接选图；有图才是切换壁纸
      if (id === "custom" && !store.get().user?.settings?.backgroundImage) {
        openBgPicker();
        return;
      }
      selectWallpaper(id);
    });
  });

  /** 上传成功后就地刷新这块 UI（不整页重渲染，免得滚动位置跳走） */
  function refreshBackgroundUi(settings) {
    const url = settings?.backgroundImage || "";
    if (bgThumb) bgThumb.style.backgroundImage = url ? `url("${url}")` : "";
    const plus = root.querySelector("[data-bg-plus]");
    if (plus) plus.hidden = !!url;
    if (bgName) bgName.textContent = url ? "自定义" : "上传图片";
    if (bgActions) bgActions.hidden = !url;
    if (pickFromBg) pickFromBg.disabled = !url;
  }

  bgInput?.addEventListener("change", async () => {
    const file = bgInput.files?.[0];
    bgInput.value = "";
    if (!file) return;
    if (!/^image\//.test(file.type)) {
      Toast.error("请选择图片文件");
      return;
    }
    if (file.size > 16 * 1024 * 1024) {
      Toast.error("图片太大了，请选 16 MB 以内的");
      return;
    }

    if (bgBusy) bgBusy.hidden = false;
    if (bgTile) bgTile.classList.add("is-busy");
    let bitmap = null;
    try {
      const prepared = await prepareBackground(file);
      bitmap = prepared.bitmap;
      const res = await api.raw("/api/me/background", prepared.blob, "PUT");
      applyUser(res.user);
      applyAppearance(res.user.settings);
      refreshBackgroundUi(res.user.settings);
      selectWallpaper("custom");
      // 图片还在手上，顺手把平均亮度算掉，蒙层不用等二次加载再调
      if (bitmap && res.user.settings?.backgroundImage) {
        rememberBackgroundLuma(res.user.settings.backgroundImage, bitmap);
      }

      const color = bitmap ? dominantColor(bitmap) : null;
      if (color) {
        applyAccent(color);
        Toast.success(`背景已更新，主题色取自图片 ${color}`);
      } else {
        Toast.success("背景已更新");
      }
    } catch (err) {
      Toast.error(err?.message || "背景上传失败");
    } finally {
      bitmap?.close?.();
      if (bgBusy) bgBusy.hidden = true;
      if (bgTile) bgTile.classList.remove("is-busy");
    }
  });

  root.querySelector('[data-act="bg-replace"]')?.addEventListener("click", openBgPicker);

  root.querySelector('[data-act="bg-remove"]')?.addEventListener("click", async () => {
    const ok = await Dialog.confirm({
      title: "移除背景图片",
      message: "移除后回到默认壁纸，图片文件会从服务器上删掉。",
      confirmText: "移除",
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await api.del("/api/me/background");
      applyUser(res.user);
      applyAppearance(res.user.settings);
      refreshBackgroundUi(res.user.settings);
      selectWallpaper(res.user.settings?.wallpaper || "sakura");
      Toast.success("背景图片已移除");
    } catch (err) {
      Toast.error(err.message);
    }
  });

  /* --- 模块不透明度 --- */
  const opInputs = [...root.querySelectorAll("[data-opacity]")];
  const currentOpacity = () => ({ ...(store.get().user?.settings?.surfaceOpacity || {}) });

  function paintOpacity(input) {
    const value = clampPercent(input.value);
    input.style.setProperty("--fill", `${value}%`);
    const out = root.querySelector(`[data-opacity-val="${input.dataset.opacity}"]`);
    if (out) out.textContent = `${value}%`;
  }

  opInputs.forEach((input) => {
    paintOpacity(input);
    // 拖动只走本地预览，松手才落库，避免一路刷接口
    input.addEventListener("input", () => {
      paintOpacity(input);
      patchSettingsLocal({
        surfaceOpacity: { ...currentOpacity(), [input.dataset.opacity]: clampPercent(input.value) },
      });
    });
    input.addEventListener("change", () => {
      persist({ surfaceOpacity: currentOpacity() });
    });
  });

  root.querySelector('[data-act="opacity-reset"]')?.addEventListener("click", () => {
    opInputs.forEach((input) => {
      input.value = surfaceDefault(input.dataset.opacity);
      paintOpacity(input);
    });
    persist({ surfaceOpacity: {} });
  });

  /* --- 看板娘大小 --- */
  const mascotSize = root.querySelector("[data-mascot-size]");
  const mascotSizeVal = root.querySelector("[data-mascot-size-val]");

  function paintMascotSize() {
    if (!mascotSize) return;
    const value = Number(mascotSize.value);
    mascotSize.style.setProperty("--fill", `${Math.round(((value - 60) / 120) * 100)}%`);
    if (mascotSizeVal) mascotSizeVal.textContent = `${value}%`;
  }

  mascotSize?.addEventListener("input", () => {
    paintMascotSize();
    setMascotScale(mascotSize.value);
  });

  /* --- 看板娘锁定（鼠标穿透）：和悬停条上的「锁定」共用一份状态 --- */
  const lockSwitch = root.querySelector("[data-mascot-lock]");
  lockSwitch?.addEventListener("change", () => setMascotLocked(lockSwitch.checked));
  const onMascotLock = (event) => {
    if (!root.isConnected) {
      window.removeEventListener("mascot:lock", onMascotLock);
      return;
    }
    if (lockSwitch) lockSwitch.checked = Boolean(event.detail?.locked);
  };
  window.addEventListener("mascot:lock", onMascotLock);

  /* --- 看板娘 · 对话设置（异步读配置） --- */
  const aiCard = root.querySelector("[data-ai-card]");
  if (aiCard) mountAiSettings(aiCard);

  /* --- Live2D 模型目录概览（目录里新增模型会自动出现） --- */
  (async () => {
    const hint = root.querySelector("[data-live2d-hint]");
    if (!hint) return;
    try {
      const data = await api.get("/api/live2d/models");
      const names = (data.models || []).map((m) => m.name);
      hint.textContent = names.length
        ? ` · 已发现 ${names.length} 个：${names.join("、")}`
        : " · 目录里还没有模型，先用云朵兜底";
    } catch {
      /* 拿不到就不提示，不影响设置项本身 */
    }
  })();

  /* --- 开关 --- */
  root.querySelectorAll("[data-toggle]").forEach((input) => {
    input.addEventListener("change", () => {
      const key = input.dataset.toggle;
      if (key === "trashEnabled") return; // 回收站开关有独立的二次确认流程
      persist({ [key]: input.checked });
      if (key === "showMascot") {
        document.getElementById("mascot")?.classList.toggle("is-hidden", !input.checked);
      }
    });
  });

  /* --- 回收站开关（关闭需服务端验证码确认） --- */
  const trashSwitch = root.querySelector('[data-toggle="trashEnabled"]');
  trashSwitch?.addEventListener("change", async () => {
    if (trashSwitch.checked) {
      const ok = await switchTrash(true);
      if (!ok) trashSwitch.checked = false;
      return;
    }
    // 未通过确认前先回弹成"开"，避免开关状态骗人
    trashSwitch.checked = true;
    const done = await confirmDisableTrash();
    if (done) trashSwitch.checked = false;
  });

  async function switchTrash(enabled, code = "") {
    try {
      const res = await api.post("/api/me/trash/switch", { enabled, code });
      applyTrashState(res.settings);
      Toast.success(enabled ? "回收站已开启" : "回收站已关闭，之后删除将直接彻底删除");
      return true;
    } catch (err) {
      Toast.error(err.message);
      return false;
    }
  }

  async function confirmDisableTrash() {
    let challenge;
    try {
      challenge = await api.post("/api/me/trash/challenge");
    } catch (err) {
      Toast.error(err.message);
      return false;
    }

    const minutes = Math.max(1, Math.round((challenge.expiresIn || 300) / 60));

    for (let attempt = 0; attempt < 3; attempt += 1) {
      const code = await Dialog.prompt({
        title: "关闭回收站",
        message: `
          关闭后删除的文件<b>不会</b>进入回收站，而是直接彻底删除，无法恢复。<br>
          请把下面这串验证码原样输入到输入框，完成确认：
          <div class="code-display" data-code>${challenge.code}</div>
          <div class="dialog__hint">由服务器生成，${minutes} 分钟内有效，验证一次即作废</div>`,
        label: "验证码",
        placeholder: "输入上方 6 位字符",
        confirmText: "确认关闭",
        validate: (value) => {
          const text = value.trim();
          if (!text) return "请输入验证码";
          if (text.length !== 6) return "验证码为 6 位字符";
          return null;
        },
      });
      if (code === null) return false;

      try {
        const res = await api.post("/api/me/trash/switch", {
          enabled: false,
          code: code.trim().toUpperCase(),
        });
        applyTrashState(res.settings);
        Toast.success("回收站已关闭，之后删除将直接彻底删除");
        return true;
      } catch (err) {
        Toast.error(err.message);
        if (attempt >= 2) return false;
        // 码已失效/过期：换一张新的，别让用户拿着废码反复试
        if (/重新获取/.test(err.message)) {
          try {
            challenge = await api.post("/api/me/trash/challenge");
          } catch (e) {
            Toast.error(e.message);
            return false;
          }
        }
      }
    }
    return false;
  }

  function applyTrashState(settings) {
    const current = store.get().user;
    if (current) {
      current.settings = { ...current.settings, ...settings };
      store.set({ user: current });
      window.dispatchEvent(new CustomEvent("user:refresh", { detail: current }));
    }
    const desc = root.querySelector("[data-trash-desc]");
    if (desc) desc.textContent = trashDesc(store.get().user?.settings || {});
  }

  /* --- 个人资料：昵称 / 简介 / 头像 --- */
  const avatarInput = root.querySelector("[data-avatar-input]");
  const avatarBox = root.querySelector("[data-avatar]");
  const avatarBusy = root.querySelector("[data-avatar-busy]");
  const clearBtn = root.querySelector('[data-act="avatar-clear"]');

  function applyUser(next) {
    if (!next) return;
    store.set({ user: next });
    window.dispatchEvent(new CustomEvent("user:refresh", { detail: next }));
    if (avatarBox) avatarBox.innerHTML = avatarMarkup(next);
    if (clearBtn) clearBtn.hidden = !next.avatarUrl;
  }

  root.querySelector('[data-act="avatar"]')?.addEventListener("click", () => avatarInput?.click());

  avatarInput?.addEventListener("change", async () => {
    const file = avatarInput.files?.[0];
    avatarInput.value = "";
    if (!file) return;
    if (!/^image\//.test(file.type)) {
      Toast.error("请选择图片文件");
      return;
    }
    if (file.size > 12 * 1024 * 1024) {
      Toast.error("图片太大了，请选 12 MB 以内的");
      return;
    }
    if (avatarBusy) avatarBusy.hidden = false;
    try {
      const blob = await squareBlob(file, 256); // 先在前端裁方压好，服务端再兜底处理
      const res = await api.raw("/api/me/avatar", blob, "PUT");
      applyUser(res.user);
      Toast.success("头像已更新");
    } catch (err) {
      Toast.error(err?.message || "头像上传失败");
    } finally {
      if (avatarBusy) avatarBusy.hidden = true;
    }
  });

  clearBtn?.addEventListener("click", async () => {
    const ok = await Dialog.confirm({
      title: "移除头像",
      message: "移除后会显示昵称首字，随时可以重新上传。",
      confirmText: "移除",
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await api.del("/api/me/avatar");
      applyUser(res.user);
      Toast.success("头像已移除");
    } catch (err) {
      Toast.error(err.message);
    }
  });

  root.querySelector('[data-act="profile-save"]')?.addEventListener("click", async () => {
    const nameEl = root.querySelector('[data-field="displayName"]');
    const bioEl = root.querySelector('[data-field="bio"]');
    const name = nameEl.value.trim();
    if (!name) {
      Toast.error("昵称不能为空");
      nameEl.focus();
      return;
    }
    try {
      const res = await api.patch("/api/me/profile", { displayName: name, bio: bioEl.value.trim() });
      applyUser(res.user);
      Toast.success("资料已保存");
    } catch (err) {
      Toast.error(err.message);
    }
  });

  root.querySelector('[data-field="displayName"]')?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") root.querySelector('[data-act="profile-save"]')?.click();
  });

  /* --- 修改密码 --- */
  root.querySelector('[data-act="password"]')?.addEventListener("click", async () => {
    const result = await Dialog.form({
      title: "修改密码",
      message: "修改成功后，其它设备上的登录状态会立即失效。",
      confirmText: "保存",
      fields: [
        { name: "oldPassword", label: "当前密码", type: "password", placeholder: "当前密码" },
        {
          name: "newPassword",
          label: "新密码",
          type: "password",
          placeholder: "至少 8 位，含数字与小写字母",
          validate: (v) => passwordProblem(v),
        },
        {
          name: "confirmPassword",
          label: "确认新密码",
          type: "password",
          placeholder: "再输入一次",
          validate: (v) => (v ? null : "请再输入一次新密码"),
        },
      ],
    });
    if (!result) return;
    if (result.newPassword !== result.confirmPassword) {
      Toast.error("两次输入的新密码不一致");
      return;
    }
    try {
      await api.put("/api/me/password", {
        old_password: result.oldPassword,
        new_password: result.newPassword,
      });
      Toast.success("密码已更新");
    } catch (err) {
      Toast.error(err.message || "修改失败");
    }
  });

  window.requestAnimationFrame(layoutThumb);
}
