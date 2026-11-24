/**
 * 设置视图：外观 / 装饰 / 账号
 */
import { api, ApiError, escapeHtml, formatBytes, formatTime } from "../api.js";
import { H, LANGS, getLang, langTag, setLang, t } from "../i18n.js";
import { passwordProblem } from "../password.js";
import { store, siteOrigin } from "../store.js";
import { Toast } from "../ui/toast.js";
import { Dialog } from "../ui/dialog.js";
import { avatarMarkup } from "../ui/avatar.js";
import { copyText } from "../ui/clipboard.js";
import { sendCode } from "../ui/email-code.js";
import {
  createCredential,
  describeError as describeWebauthnError,
  supported as webauthnSupported,
} from "../ui/webauthn.js";
import {
  ACCENT_GRADIENTS,
  ACCENT_PRESETS,
  WALLPAPERS,
  accentCompanion,
  accentStyleOf,
  applyAppearance,
  dominantColors,
  patchSettingsLocal,
  rememberBackgroundLuma,
  snapshotDataUrl,
} from "../theme.js";
import { getMascotScale, isMascotWheelZoom, setMascotScale, setMascotWheelZoom } from "../ui/live2d.js";
import { askDesktopPermission, desktopPermission, resetNotifierCursor } from "../ui/notify.js";
import { isMascotLocked, setMascotLocked } from "../ui/mascot-actions.js";
import { mountAiSettings } from "../ui/ai-config.js";
import { fuzzyScore, highlight, splitTokens } from "../ui/fuzzy.js";
import { pickFolder } from "../ui/picker.js";

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
  return t("开启中：删除的文件先进入回收站，{days} 天后自动清理", { days });
}

/* 各模块背景的默认浓度：直接读主题 CSS 变量，换主题/壁纸后默认值自然跟着走
   卡片没有固定 alpha（设置卡、关于卡、统计卡底色各不相同），滑块是倍数，默认 1 = 100% */
/** 邮箱行描述：未验证时只留地址，红字提醒交给 emailWarnText */
function emailDescText(user) {
  const mail = String(user?.email || "");
  if (!mail) return t("还没有绑定 · 忘记密码时只能联系管理员");
  return user.emailVerified ? `${escapeHtml(mail)} · ${t("可用于登录和找回密码")}` : escapeHtml(mail);
}

/** 未验证邮箱的红字提醒；已验证 / 未绑定返回空串（调用方据此隐藏） */
function emailWarnText(user) {
  if (!user?.email || user.emailVerified) return "";
  return store.get().meta?.mailReady
    ? t("邮箱尚未验证，暂时不能用于找回密码和两步验证")
    : t("邮箱尚未验证，暂时不能用于找回密码和两步验证 · 站点未配置邮箱服务，无法发送验证码");
}

const SURFACE_FALLBACK = { nav: 42, titlebar: 46, panel: 62, dialog: 82, card: 100 };
const SURFACE_VARS = {
  nav: "--nav-a",
  titlebar: "--title-a",
  panel: "--panel-a",
  dialog: "--dialog-a",
  card: "--card-a",
};
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

/** 上一次渲染挂出去的事件监听收尾器（#view-root 与 window 都是常驻的） */
let settingsViewAbort = null;

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

export function renderSettings(root, { tab } = {}) {
  // #view-root 是常驻节点，挂它/window 上的监听要自己收尾，否则每次进设置页都会叠一套
  settingsViewAbort?.abort();
  const view = new AbortController();
  settingsViewAbort = view;
  const keep = { signal: view.signal };

  const { user } = store.get();
  // 通用取值工具：danger 区那段在 if (davUrlEl) { } 之外，必须放在函数顶部
  const q = (sel) => root.querySelector(sel);
  const currentUser = () => store.get().user || {};
  // 注销时要不要邮箱码：绑过且验证过邮箱、站点也配了邮件服务
  const deleteNeedsCode = Boolean(user?.emailVerified && store.get().meta?.mailReady);
  const s = user?.settings || {};
  const mode = s.themeMode ?? 2;
  const accent = (s.accentColor || "#c084fc").toLowerCase();
  const isPreset = ACCENT_PRESETS.some((c) => c.value.toLowerCase() === accent);
  const accentStyle = accentStyleOf(s);
  const gradColors = accentStyle.colors.length >= 2
    ? accentStyle.colors
    : [accent, accentCompanion(accent)];
  // custom 但没图（比如别的设备删掉了）就按默认壁纸显示，别留一片空白
  const wallpaper = s.wallpaper === "custom" && !s.backgroundImage ? "sakura" : s.wallpaper || "sakura";
  const displayName = (user?.displayName || user?.username || "").trim();
  const maskedPreview = displayName.slice(0, 2) || "—";
  const savedOpacity = s.surfaceOpacity || {};
  const mascotScale = getMascotScale();
  // WebDAV：管理员总开关 + 用户自己的开关（老接口没有 davServerEnabled 时按开启处理）
  const davServerOn = user?.davServerEnabled !== false;
  const davSelfOn = s.webdavEnabled !== false;
  const opacityRows = [
    ["nav", "侧栏"],
    ["titlebar", "标题栏"],
    ["panel", "内容面板"],
    ["card", "卡片"],
    ["dialog", "弹窗"],
  ].map(([key, name]) => {
    const saved = Number(savedOpacity[key]);
    return { key, name, value: Number.isFinite(saved) ? clampPercent(saved) : surfaceDefault(key) };
  });

  root.innerHTML = H`
    <div class="view-head">
      <div class="view-title"><svg class="icon"><use href="#i-settings"/></svg>设置</div>
      <span class="view-sub">外观与账号</span>
    </div>
    <div class="view-body">
      <div class="settings">

        <div class="settings-tabs">
          <div class="segmented" data-settings-tabs>
            <div class="segmented__thumb" data-tab-thumb></div>
            <button class="segmented__btn is-active" type="button" data-settings-tab="appearance">
              <svg class="icon icon--s"><use href="#i-palette"/></svg>外观
            </button>
            <button class="segmented__btn" type="button" data-settings-tab="mascot">
              <svg class="icon icon--s"><use href="#i-star"/></svg>看板娘
            </button>
            <button class="segmented__btn" type="button" data-settings-tab="files">
              <svg class="icon icon--s"><use href="#i-share"/></svg>文件与分享
            </button>
            <button class="segmented__btn" type="button" data-settings-tab="quick">
              <svg class="icon icon--s"><use href="#i-bolt"/></svg>快传
            </button>
            <button class="segmented__btn" type="button" data-settings-tab="account">
              <svg class="icon icon--s"><use href="#i-user"/></svg>账号
            </button>
          </div>
        </div>

        <div class="settings-search" data-settings-search-wrap>
          <div class="searchbox">
            <span class="textbox">
              <svg class="icon icon--s"><use href="#i-search"/></svg>
              <input type="search" data-settings-search placeholder="搜索设置项…" autocomplete="off"
                     aria-label="搜索设置项">
            </span>
          </div>
          <button class="settings-search__clear icon-btn" type="button" data-settings-clear hidden
                  title="清空搜索">
            <svg class="icon"><use href="#i-close"/></svg>
          </button>
          <div class="settings-hits" data-settings-hits hidden></div>
        </div>

        <div class="settings-pane" data-settings-pane="appearance">

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-palette"/></svg>外观</div>
          <div class="settings-card__body">
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">界面语言</div>
                <div class="setting-row__desc">切换后立即生效，并记到这个账号；换设备会自动跟随</div>
              </div>
              <div class="segmented segmented--wide" data-lang-seg>
                ${LANGS.map(
                  (l) =>
                    `<button class="segmented__btn ${l.id === getLang() ? "is-active" : ""}"
                             type="button" data-lang="${l.id}">${l.label}</button>`
                ).join("")}
              </div>
            </div>
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">主题模式</div>
                <div class="setting-row__desc">跟随系统时会随 Windows 深色模式自动切换</div>
              </div>
              <div class="segmented segmented--wide" data-theme-seg>
                <div class="segmented__thumb" data-thumb></div>
                ${THEME_MODES.map(
                  (m) =>
                    H`<button class="segmented__btn ${m.value === mode ? "is-active" : ""}" type="button" data-mode="${m.value}">${t(m.label)}</button>`
                ).join("")}
              </div>
            </div>
            <div class="setting-row setting-row--stack accent-row">
              <div class="setting-row__main">
                <div class="setting-row__label">主题色</div>
                <div class="setting-row__desc">用于按钮、选中状态和卡片底色；可以单色，也可以渐变（最多 5 个颜色）</div>
              </div>
              <div class="accent-editor" data-accent-editor>
                <div class="accent-editor__head">
                  <div class="accent-mode" data-accent-mode>
                    <button class="accent-mode__btn ${accentStyle.mode === "solid" ? "is-active" : ""}"
                            type="button" data-accent-mode-btn="solid">${t("单色")}</button>
                    <button class="accent-mode__btn ${accentStyle.mode === "gradient" ? "is-active" : ""}"
                            type="button" data-accent-mode-btn="gradient">${t("渐变")}</button>
                  </div>
                  <div class="accent-editor__acts">
                    <button class="btn btn--subtle btn--sm accent-editor__pick" type="button"
                            data-act="pick-bg" ${s.backgroundImage ? "" : "disabled"}>
                      <svg class="icon icon--s"><use href="#i-image"/></svg>${t("从背景取色")}
                    </button>
                    <button class="btn btn--subtle btn--sm accent-editor__pick accent-editor__pick--ai"
                            type="button" data-act="pick-ai"
                            title="${t("用「看板娘 · 对话设置」里的 AI 接口取色")}">
                      <svg class="icon icon--s"><use href="#i-spark"/></svg><span data-ai-pick-label>${t("AI 取色")}</span>
                    </button>
                  </div>
                </div>
                <div class="accent-solid" data-accent-solid ${accentStyle.mode === "gradient" ? "hidden" : ""}>
                  <div class="swatch-row" data-swatches>
                    ${ACCENT_PRESETS.map(
                      (c) =>
                        H`<button class="swatch ${c.value === accent ? "is-active" : ""}"
                           type="button" data-color="${c.value}" title="${t(c.name)}"
                           style="background:${c.value}"></button>`
                    ).join("")}
                    <label class="swatch swatch--pick ${isPreset ? "" : "is-active"}"
                           data-swatch-pick title="${t("打开取色板挑颜色")}"
                           style="${isPreset ? "" : H`background:${escapeHtml(accent)}`}">
                      <input type="color" value="${escapeHtml(accent)}" data-color-pick aria-label="${t("自定义主题色")}">
                    </label>
                  </div>
                  <input class="accent-hex" data-accent-hex maxlength="7" spellcheck="false"
                         value="${escapeHtml(accent)}" placeholder="#c084fc"
                         aria-label="${t("自定义颜色，填 #rrggbb")}">
                </div>
                <div class="accent-gradient" data-accent-gradient ${accentStyle.mode === "solid" ? "hidden" : ""}>
                  <div class="accent-stops" data-accent-stops>
                    ${gradColors
                      .map(
                        (c, i) => H`<span class="accent-stop" style="--stop:${c}">
                          <span class="accent-stop__chip" title="${t("打开取色板挑颜色")}">
                            <input type="color" value="${c}" data-accent-stop="${i}"
                                   aria-label="${t("渐变颜色 {n}", { n: i + 1 })}">
                          </span>
                          <input class="accent-hex accent-hex--stop" value="${c}" data-accent-stop-hex="${i}"
                                 maxlength="7" spellcheck="false" aria-label="${t("渐变颜色 {n}", { n: i + 1 })}">
                          <button class="accent-stop__del" type="button" data-accent-stop-del="${i}"
                                  title="${t("移除这个颜色")}" aria-label="${t("移除这个颜色")}"
                                  ${gradColors.length <= 2 ? "disabled" : ""}>
                            <svg class="icon icon--s"><use href="#i-close"/></svg>
                          </button>
                        </span>`
                      )
                      .join("")}
                  </div>
                  <div class="accent-gradient__foot">
                    <button class="btn btn--subtle btn--sm" type="button" data-accent-add
                            ${gradColors.length >= 5 ? "disabled" : ""}>${t("添加颜色")}</button>
                    <div class="grad-presets" data-accent-presets>
                      ${ACCENT_GRADIENTS.map(
                        (g) => H`<button class="grad-preset" type="button"
                            data-accent-preset="${g.colors.join(",")}"
                            style="background:linear-gradient(135deg, ${g.colors.join(", ")})"
                            title="${t(g.name)}" aria-label="${t(g.name)}"></button>`
                      ).join("")}
                    </div>
                  </div>
                </div>
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
                    H`<button class="wallpaper-opt wp-${w.id} ${w.id === wallpaper ? "is-active" : ""}"
                       type="button" data-wallpaper="${w.id}" title="${t(w.name)}">
                       ${w.id === "plain" ? '<svg class="icon icon--s"><use href="#i-close"/></svg>' : ""}
                       <span class="wallpaper-opt__name">${t(w.name)}</span>
                     </button>`
                ).join("")}
                <button class="wallpaper-opt wp-custom ${wallpaper === "custom" ? "is-active" : ""}"
                        type="button" data-wallpaper="custom" data-bg-tile title="${t("上传自己的背景图")}">
                  <span class="wp-custom__thumb" data-bg-thumb
                        ${s.backgroundImage ? H`style="background-image:url('${escapeHtml(s.backgroundImage)}')"` : ""}></span>
                  <svg class="icon icon--s wp-custom__plus" data-bg-plus ${s.backgroundImage ? "hidden" : ""}><use href="#i-plus"/></svg>
                  <span class="wallpaper-opt__name" data-bg-name>${s.backgroundImage ? t("自定义") : t("上传图片")}</span>
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
                <div class="setting-row__desc">分别控制各块背景的实心程度，0% 完全透明；卡片以当前默认观感为 100%，往下拖会越来越透</div>
              </div>
              <div class="opacity-list" data-opacity-list>
                ${opacityRows
                  .map(
                    (r) => H`<label class="opacity-item">
                      <span class="opacity-item__name">${t(r.name)}</span>
                      <input type="range" min="0" max="100" step="1" value="${r.value}"
                             data-opacity="${r.key}" style="--fill:${r.value}%"
                             aria-label="${t("{name} 不透明度", { name: t(r.name) })}">
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

        </div>

        <div class="settings-pane" data-settings-pane="mascot">

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
                <div class="setting-row__label">滚轮缩放</div>
                <div class="setting-row__desc">鼠标停在她身上时，滚轮上下就能改大小（一格约 5%）；关掉后滚轮照常滚动页面</div>
              </div>
              <label class="switch">
                <input type="checkbox" data-mascot-wheel ${isMascotWheelZoom() ? "checked" : ""}>
                <span class="switch__track"><span class="switch__knob"></span></span>
              </label>
            </div>
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">${t("重置看板娘位置")}</div>
                <div class="setting-row__desc">${t("把她放回右下角的默认位置；大小、模型和显示开关都不变")}</div>
              </div>
              <button class="btn btn--sm" type="button" data-mascot-reset>${t("重置位置")}</button>
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
            管理员也可以提供一套默认接口，没自己配置时自动使用
          </div>
          <div class="settings-card__body" data-ai-body>
            <div class="settings-card__note settings-loading"><span class="spinner"></span> 正在读取配置…</div>
          </div>
        </section>

        </div>

        <div class="settings-pane" data-settings-pane="files">

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
            ${
              davServerOn
                ? H`
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">启用我的 WebDAV</div>
                <div class="setting-row__desc">关掉后你的账号暂时不能挂载，网盘里的文件不受影响，随时可以再打开</div>
              </div>
              <label class="switch">
                <input type="checkbox" data-toggle="webdavEnabled" ${davSelfOn ? "checked" : ""}>
                <span class="switch__track"><span class="switch__knob"></span></span>
              </label>
            </div>
            <div data-dav-body ${davSelfOn ? "" : "hidden"}>
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
            </div>`
                : H`<div class="settings-card__note">
              <svg class="icon icon--s"><use href="#i-info"/></svg>
              <span>管理员已关闭 WebDAV，暂时不能挂载。需要的话请联系管理员在「管理后台 → 系统设置」里打开。</span>
            </div>`
            }
          </div>
        </section>

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-share"/></svg>分享</div>
          <div class="settings-card__body">
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">分享页显示完整昵称</div>
                <div class="setting-row__desc">
                  ${t("关闭时别人只能看到昵称前两位（当前显示为「{name}」），头像也不会外露", {
                    name: escapeHtml(maskedPreview),
                  })}
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
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-bell"/></svg>消息提醒</div>
          <div class="settings-card__body">
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">站内提醒</div>
                <div class="setting-row__desc">收到新消息时在右下角弹一张小卡片，点一下直接进那个会话（正在看的会话不打扰）</div>
              </div>
              <label class="switch">
                <input type="checkbox" data-toggle="notifyInapp" ${s.notifyInapp === false ? "" : "checked"}>
                <span class="switch__track"><span class="switch__knob"></span></span>
              </label>
            </div>
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">浏览器通知<span class="chip" data-notify-perm hidden></span></div>
                <div class="setting-row__desc" data-notify-desc>页面在后台或最小化时用系统通知提醒你。第一次开启需要允许浏览器通知。</div>
              </div>
              <label class="switch">
                <input type="checkbox" data-toggle="notifyDesktop" ${s.notifyDesktop ? "checked" : ""}>
                <span class="switch__track"><span class="switch__knob"></span></span>
              </label>
            </div>
          </div>
        </section>

        </div>

        <div class="settings-pane" data-settings-pane="quick">

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-bolt"/></svg>发给用户</div>
          <div class="settings-card__body">
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">允许别人发文件给我</div>
                <div class="setting-row__desc">关掉后，别人在「快传 → 发给用户」里会看到你已经关闭接收；已经发过来、还没处理的那几条，照样可以同意或拒绝</div>
              </div>
              <label class="switch">
                <input type="checkbox" data-toggle="quickReceiveEnabled" ${s.quickReceiveEnabled === false ? "" : "checked"}>
                <span class="switch__track"><span class="switch__knob"></span></span>
              </label>
            </div>
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">自动同意，直接存进我的文件</div>
                <div class="setting-row__desc">打开后不再需要你点「同意」，文件直接落到下面这个位置；想先看看内容再决定就关掉它</div>
              </div>
              <label class="switch">
                <input type="checkbox" data-toggle="quickAutoAccept" ${s.quickAutoAccept ? "checked" : ""}>
                <span class="switch__track"><span class="switch__knob"></span></span>
              </label>
            </div>
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">收到后存到</div>
                <div class="setting-row__desc" data-quick-folder-path>我的文件 / 快传的文件</div>
              </div>
              <div class="row" style="gap:8px;flex:none">
                <button class="btn btn--subtle" type="button" data-act="quick-folder">
                  <svg class="icon icon--s"><use href="#i-folder"/></svg>选择文件夹
                </button>
                <button class="btn btn--subtle" type="button" data-act="quick-folder-reset" hidden>恢复默认</button>
              </div>
            </div>
          </div>
        </section>

        </div>

        <div class="settings-pane" data-settings-pane="account">

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
                  <span class="muted">${t("登录名 <b>{name}</b> 不可修改 · 已用 {used} / {quota}", {
                    name: escapeHtml(user?.username || "—"),
                    used: formatBytes(user?.usedBytes || 0),
                    quota: formatBytes(user?.quotaBytes || 0),
                  })}</span>
                  <span style="flex:1"></span>
                  <button class="btn btn--subtle" type="button" data-act="open-profile">${t("个人主页")}</button>
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
                <div class="setting-row__desc" data-email-desc>${emailDescText(user)}</div>
                <div class="setting-row__warn" data-email-warn ${emailWarnText(user) ? "" : "hidden"}>
                  <span class="setting-row__warn-main">
                    <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-alert"/></svg>
                    <span data-email-warn-text>${escapeHtml(emailWarnText(user))}</span>
                  </span>
                  <button class="btn btn--subtle setting-row__warn-btn" type="button"
                          data-act="email-verify" ${store.get().meta?.mailReady ? "" : "hidden"}>去验证</button>
                </div>
              </div>
              <button class="btn" type="button" data-act="email">${user?.email ? t("换绑") : t("绑定邮箱")}</button>
            </div>
            <div class="setting-panel" data-email-panel hidden>
              <div data-email-step="password">
                <label class="field">
                  <span class="field__label">账户密码</span>
                  <span class="textbox">
                    <input type="password" data-email-password autocomplete="current-password"
                           placeholder="先验证一次密码，确认是本人操作" maxlength="128">
                  </span>
                </label>
                <div class="setting-panel__foot">
                  <button class="btn btn--subtle" type="button" data-act="email-cancel">取消</button>
                  <button class="btn btn--accent" type="button" data-act="email-next">下一步</button>
                </div>
              </div>
              <div data-email-step="code" hidden>
                <label class="field">
                  <span class="field__label">新邮箱</span>
                  <span class="textbox">
                    <input type="email" data-email-address autocomplete="email" maxlength="254"
                           placeholder="you@example.com" value="${escapeHtml(user?.email || "")}">
                  </span>
                </label>
                <div class="field" data-email-code-row>
                  <span class="field__label">邮箱验证码</span>
                  <div class="code-row">
                    <span class="textbox">
                      <input type="text" inputmode="numeric" autocomplete="one-time-code"
                             maxlength="6" data-email-code placeholder="6 位数字">
                    </span>
                    <button class="btn" type="button" data-act="email-send"
                            data-idle-label="发送验证码">发送验证码</button>
                  </div>
                </div>
                <p class="setting-panel__hint" data-email-hint hidden></p>
                <div class="setting-panel__foot">
                  <button class="btn btn--subtle" type="button" data-act="email-back">上一步</button>
                  <button class="btn btn--accent" type="button" data-act="email-save">完成绑定</button>
                </div>
              </div>
            </div>

            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">登录密码</div>
                <div class="setting-row__desc">修改后其它设备上的登录状态会立即失效</div>
              </div>
              <button class="btn" type="button" data-act="password">修改密码</button>
            </div>
            <div class="setting-panel" data-password-panel hidden>
              <label class="field">
                <span class="field__label">当前密码</span>
                <span class="textbox">
                  <input type="password" data-pwd-old autocomplete="current-password" maxlength="128">
                </span>
              </label>
              <label class="field">
                <span class="field__label">新密码</span>
                <span class="textbox">
                  <input type="password" data-pwd-new autocomplete="new-password" maxlength="128"
                         placeholder="至少 8 位，含数字与小写字母">
                </span>
              </label>
              <label class="field">
                <span class="field__label">确认新密码</span>
                <span class="textbox">
                  <input type="password" data-pwd-confirm autocomplete="new-password" maxlength="128"
                         placeholder="再输入一次">
                </span>
              </label>
              <div class="field" data-pwd-email-row hidden>
                <span class="field__label">邮箱验证码</span>
                <div class="code-row">
                  <span class="textbox">
                    <input type="text" inputmode="numeric" autocomplete="one-time-code"
                           maxlength="6" data-pwd-code placeholder="6 位数字">
                  </span>
                  <button class="btn btn--subtle" type="button" data-act="pwd-send"
                          data-idle-label="发送验证码">发送验证码</button>
                </div>
              </div>
              <div class="field">
                <span class="field__label">图形验证码</span>
                <div class="captcha">
                  <span class="textbox">
                    <input type="text" autocomplete="off" spellcheck="false" maxlength="8"
                           data-pwd-captcha placeholder="输入右侧字符">
                  </span>
                  <button class="captcha__img" type="button" data-pwd-captcha-refresh title="点击换一张">
                    <img data-pwd-captcha-img alt="验证码">
                  </button>
                </div>
              </div>
              <p class="setting-panel__hint" data-pwd-note hidden></p>
              <div class="setting-panel__foot">
                <button class="btn btn--subtle" type="button" data-act="pwd-cancel">取消</button>
                <button class="btn btn--accent" type="button" data-act="pwd-save">保存</button>
              </div>
            </div>

            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">两步验证</div>
                <div class="setting-row__desc" data-twofa-desc>
                  开启后，用密码登录还要再过一次通行密钥或邮箱验证码
                </div>
              </div>
              <label class="switch">
                <input type="checkbox" data-twofa-toggle>
                <span class="switch__track"><span class="switch__knob"></span></span>
              </label>
            </div>
            <div class="setting-panel" data-twofa-panel hidden>
              <p class="setting-panel__hint" data-twofa-note></p>
              <label class="field">
                <span class="field__label">账户密码</span>
                <span class="textbox">
                  <input type="password" data-twofa-password autocomplete="current-password" maxlength="128">
                </span>
              </label>
              <div class="field" data-twofa-email-row hidden>
                <span class="field__label">邮箱验证码</span>
                <div class="code-row">
                  <span class="textbox">
                    <input type="text" inputmode="numeric" autocomplete="one-time-code"
                           maxlength="6" data-twofa-code placeholder="6 位数字">
                  </span>
                  <button class="btn" type="button" data-act="twofa-send"
                          data-idle-label="发送验证码">发送验证码</button>
                </div>
              </div>
              <div class="field" data-twofa-captcha-row hidden>
                <span class="field__label">图形验证码</span>
                <div class="captcha">
                  <span class="textbox">
                    <input type="text" autocomplete="off" spellcheck="false" maxlength="8" data-twofa-captcha>
                  </span>
                  <button class="captcha__img" type="button" data-twofa-captcha-refresh title="点击换一张">
                    <img data-twofa-captcha-img alt="验证码">
                  </button>
                </div>
              </div>
              <div class="setting-panel__foot">
                <button class="btn btn--subtle" type="button" data-act="twofa-cancel">取消</button>
                <button class="btn btn--accent" type="button" data-act="twofa-save">确认</button>
              </div>
            </div>
            <a class="setting-row setting-row--link" href="#/about">
              <div class="setting-row__main">
                <div class="setting-row__label">关于</div>
                <div class="setting-row__desc" data-version>CloudNETP</div>
              </div>
              <span class="chip">${user?.role === 1 ? t("管理员") : t("普通用户")}</span>
              <svg class="icon icon--s setting-row__chevron"><use href="#i-chevron"/></svg>
            </a>
          </div>
        </section>

        <section class="settings-card" data-card="passkeys">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-shield"/></svg>通行密钥</div>
          <div class="settings-card__body">
            <p class="setting-panel__hint" data-passkey-note hidden></p>
            <div class="passkey-list" data-passkey-list>
              <p class="setting-panel__hint">正在读取…</p>
            </div>
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">添加通行密钥</div>
                <div class="setting-row__desc">
                  用 Windows Hello、手机指纹或硬件密钥作为登录的第二因子
                </div>
              </div>
              <button class="btn" type="button" data-act="passkey-add">添加</button>
            </div>
            <div class="setting-panel" data-passkey-panel hidden>
              <label class="field">
                <span class="field__label">设备备注</span>
                <span class="textbox">
                  <input type="text" data-passkey-label maxlength="40" placeholder="例如：我的笔记本">
                </span>
              </label>
              <label class="field">
                <span class="field__label">账户密码</span>
                <span class="textbox">
                  <input type="password" data-passkey-password autocomplete="current-password" maxlength="128"
                         placeholder="验证身份后才能添加">
                </span>
              </label>
              <div class="field" data-passkey-email-row hidden>
                <span class="field__label">或者用邮箱验证码</span>
                <div class="code-row">
                  <span class="textbox">
                    <input type="text" inputmode="numeric" autocomplete="one-time-code"
                           maxlength="6" data-passkey-code placeholder="6 位数字">
                  </span>
                  <button class="btn btn--subtle" type="button" data-act="passkey-send"
                          data-idle-label="发送验证码">发送验证码</button>
                </div>
              </div>
              <p class="setting-panel__hint" data-passkey-hint hidden></p>
              <div class="setting-panel__foot">
                <button class="btn btn--subtle" type="button" data-act="passkey-cancel">取消</button>
                <button class="btn btn--accent" type="button" data-act="passkey-create">开始添加</button>
              </div>
            </div>
          </div>
        </section>

        <section class="settings-card" data-card="devices">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-monitor"/></svg>${t("登录设备管理")}</div>
          <div class="settings-card__body">
            <p class="setting-panel__hint">${
              t("每一台登录过的设备都会记在这里。发现不是自己的设备，可以单独下线它。")
            }</p>
            <div class="passkey-list" data-device-list>
              <p class="setting-panel__hint">正在读取…</p>
            </div>
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">${t("下线其他设备")}</div>
                <div class="setting-row__desc">${t("把除当前设备以外的登录全部下线，对方下次请求就会掉线")}</div>
              </div>
              <button class="btn" type="button" data-act="revoke-others">${t("下线其他设备")}</button>
            </div>
          </div>
        </section>

        <section class="settings-card settings-card--danger" data-card="danger">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-alert"/></svg>${t("危险区域")}</div>
          <div class="settings-card__body">
            <div class="setting-row">
              <div class="setting-row__main">
                <div class="setting-row__label">${t("注销账号")}</div>
                <div class="setting-row__desc">${t(
                  "永久删除这个账号和里面的全部文件、分享、快传、对话记录；操作不可撤销，也没法帮你找回。"
                )}</div>
              </div>
              <button class="btn btn--danger" type="button" data-act="delete-account">${t("注销账号")}</button>
            </div>

            <div class="setting-panel" data-delete-panel hidden>
              <p class="setting-panel__hint">${
                deleteNeedsCode
                  ? t("确认要注销的话，请再验证一次身份：输入登录密码，并填写发送到绑定邮箱的验证码。")
                  : t("确认要注销的话，请输入登录密码来确认是本人操作。")
              }</p>
              <label class="field">
                <span class="field__label">${t("登录密码")}</span>
                <span class="textbox">
                  <input type="password" data-delete-password autocomplete="current-password"
                         maxlength="128" placeholder="${t("当前登录密码")}">
                </span>
              </label>
              <div class="field" data-delete-code-row ${deleteNeedsCode ? "" : "hidden"}>
                <span class="field__label">${t("邮箱验证码")}</span>
                <div class="code-row">
                  <span class="textbox">
                    <input type="text" inputmode="numeric" autocomplete="one-time-code"
                           maxlength="6" data-delete-code placeholder="${t("6 位数字")}">
                  </span>
                  <button class="btn" type="button" data-act="delete-send"
                          data-idle-label="发送验证码">${t("发送验证码")}</button>
                </div>
              </div>
              <p class="setting-panel__hint" data-delete-hint ${deleteNeedsCode ? "hidden" : ""}>${
                deleteNeedsCode
                  ? ""
                  : t("这个账号没有已验证的邮箱，或站点未配置邮件服务，输入密码即可确认是本人操作。")
              }</p>
              <div class="setting-panel__foot">
                <button class="btn btn--subtle" type="button" data-act="delete-cancel">${t("取消")}</button>
                <button class="btn btn--danger" type="button" data-act="delete-confirm">${t("永久删除")}</button>
              </div>
            </div>
          </div>
        </section>

        </div>

      </div>
    </div>
  `;

  root.querySelector("[data-version]").textContent =
    `${store.get().meta?.appName || "CloudNETP"} v${store.get().meta?.version || "0.1.0"}`;

  /* --- WebDAV 挂载（管理员关掉总开关时整块换成提示，不往下绑定） --- */
  const davUrlEl = root.querySelector("[data-dav-url]");
  if (davUrlEl) {
  const davUrl = `${siteOrigin()}/dav/`;
  const davHost = davUrl.replace(/^https?:\/\//, "");
  // UNC 写法：\\主机@端口\dav（资源管理器的「映射网络驱动器」也吃这个）
  const davUnc = (() => {
    try {
      const url = new URL(davUrl);
      const port = url.port || (url.protocol === "https:" ? 443 : 80);
      return H`\\\\${url.hostname}@${port}\\dav`;
    } catch {
      return davHost;
    }
  })();
  davUrlEl.textContent = davHost;
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
    // 走统一的 copyText：http 内网里 navigator.clipboard 不存在，用 execCommand 兜底
    if (await copyText(davUrl)) {
      Toast.success("挂载地址已复制");
    } else {
      Toast.error("复制失败，请手动选中地址");
    }
  });

  /* --- 账号安全：邮箱 / 密码 / 两步验证 / 通行密钥 --- */
  // 这几块共享一份服务端状态（GET /api/me/security），避免每个面板各查一遍
  const meta = store.get().meta || {};
  let security = null;
  let twofaWant = false;
  let pwdCaptchaId = "";
  let twofaCaptchaId = "";

  const emailPanel = q("[data-email-panel]");
  const pwdPanel = q("[data-password-panel]");
  const twofaPanel = q("[data-twofa-panel]");
  const passkeyPanel = q("[data-passkey-panel]");

  const emailFactorOn = () =>
    Boolean(security?.emailFactorAvailable ?? (currentUser().emailVerified && meta.mailReady));
  const passkeyList = () => security?.passkeys || [];

  function setEmailStep(step) {
    emailPanel?.querySelectorAll("[data-email-step]").forEach((node) => {
      node.hidden = node.dataset.emailStep !== step;
    });
  }

  async function fetchCaptcha(which) {
    try {
      const data = await api.get("/api/auth/captcha");
      if (which === "pwd") {
        pwdCaptchaId = data.captchaId;
        const img = q("[data-pwd-captcha-img]");
        if (img) img.src = data.image;
      } else {
        twofaCaptchaId = data.captchaId;
        const img = q("[data-twofa-captcha-img]");
        if (img) img.src = data.image;
      }
    } catch {
      /* 拿不到图就让后端在提交时报错，别把面板卡住 */
    }
  }

  function updateEmailDesc(next) {
    const desc = q("[data-email-desc]");
    if (desc) desc.innerHTML = emailDescText(next);
    const warn = q("[data-email-warn]");
    const warnText = emailWarnText(next);
    if (warn) {
      warn.hidden = !warnText;
      const span = warn.querySelector("[data-email-warn-text]");
      if (span) span.textContent = warnText;
    }
    const btn = q('[data-act="email"]');
    if (btn) btn.textContent = next?.email ? t("换绑") : t("绑定邮箱");
  }

  /* 邮箱：先验证密码，再填新邮箱 + 验证码（「换绑」与红字旁的「去验证」共用） */
  function openEmailPanel() {
    if (!emailPanel) return;
    emailPanel.hidden = false;
    setEmailStep("password");
    q("[data-email-password]").value = "";
    q("[data-email-address]").value = currentUser().email || "";
    q("[data-email-code]").value = "";
    const hint = q("[data-email-hint]");
    if (hint) hint.hidden = true;
    q("[data-email-password]")?.focus();
  }

  q('[data-act="email"]')?.addEventListener("click", () => {
    if (!emailPanel) return;
    if (!emailPanel.hidden) {
      emailPanel.hidden = true;
      return;
    }
    openEmailPanel();
  });

  /* 未验证邮箱旁的「去验证」：直接展开面板，邮箱已预填，走完流程即完成验证 */
  q('[data-act="email-verify"]')?.addEventListener("click", openEmailPanel);

  q('[data-act="email-cancel"]')?.addEventListener("click", () => {
    if (emailPanel) emailPanel.hidden = true;
  });

  q('[data-act="email-back"]')?.addEventListener("click", () => setEmailStep("password"));

  q('[data-act="email-next"]')?.addEventListener("click", () => {
    if (!q("[data-email-password]").value) {
      Toast.error("请输入账户密码");
      return;
    }
    setEmailStep("code");
    q("[data-email-address]")?.focus();
  });

  q('[data-act="email-send"]')?.addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    const address = q("[data-email-address]").value.trim();
    if (!address) {
      Toast.error("请先填写邮箱");
      return;
    }
    try {
      await sendCode({
        url: "/api/me/email/code",
        scene: "bind",
        body: { target: address, password: q("[data-email-password]").value },
        button: btn,
      });
      const hint = q("[data-email-hint]");
      if (hint) {
        hint.hidden = false;
        hint.textContent = t("验证码已发送到 {email}", { email: address });
      }
      q("[data-email-code]")?.focus();
    } catch (err) {
      Toast.error(err?.message || "发送失败，请稍后再试");
    }
  });

  q('[data-act="email-save"]')?.addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    const address = q("[data-email-address]").value.trim();
    const code = q("[data-email-code]").value.trim();
    if (!address) {
      Toast.error("请填写邮箱");
      return;
    }
    if (emailFactorOn() && meta.mailReady && !code) {
      Toast.error("请填写邮箱验证码");
      return;
    }
    btn.disabled = true;
    try {
      const res = await api.patch("/api/me/email", { email: address, code });
      applyUser(res.user);
      updateEmailDesc(res.user);
      emailPanel.hidden = true;
      Toast.success(res.user.emailVerified ? "邮箱已绑定并验证" : "邮箱已保存（未验证）");
      refreshSecurity();
    } catch (err) {
      Toast.error(err?.message || "保存失败");
    } finally {
      btn.disabled = false;
    }
  });

  /* 密码：旧密码 + 邮箱验证码（有邮箱因子时）+ 图形验证码 */
  q('[data-act="password"]')?.addEventListener("click", () => {
    if (!pwdPanel) return;
    const opening = pwdPanel.hidden;
    pwdPanel.hidden = !opening;
    if (!opening) return;
    ["[data-pwd-old]", "[data-pwd-new]", "[data-pwd-confirm]", "[data-pwd-code]", "[data-pwd-captcha]"].forEach(
      (sel) => {
        const node = q(sel);
        if (node) node.value = "";
      }
    );
    const withEmail = emailFactorOn();
    const emailRow = q("[data-pwd-email-row]");
    if (emailRow) emailRow.hidden = !withEmail;
    const note = q("[data-pwd-note]");
    if (note) {
      note.hidden = withEmail;
      note.textContent = currentUser().email
        ? t("邮件服务未配置，本次改用图形验证码")
        : t("该账号未绑定邮箱，本次仅校验图形验证码");
    }
    fetchCaptcha("pwd");
    q("[data-pwd-old]")?.focus();
  });

  q('[data-act="pwd-cancel"]')?.addEventListener("click", () => {
    if (pwdPanel) pwdPanel.hidden = true;
  });
  q("[data-pwd-captcha-refresh]")?.addEventListener("click", () => fetchCaptcha("pwd"));

  q('[data-act="pwd-send"]')?.addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    try {
      await sendCode({ url: "/api/me/email/code", scene: "password", button: btn });
      Toast.success("验证码已发送，请查收邮件");
      q("[data-pwd-code]")?.focus();
    } catch (err) {
      Toast.error(err?.message || "发送失败，请稍后再试");
    }
  });

  q('[data-act="pwd-save"]')?.addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    const oldPassword = q("[data-pwd-old]").value;
    const newPassword = q("[data-pwd-new]").value;
    const confirm = q("[data-pwd-confirm]").value;
    const code = q("[data-pwd-code]").value.trim();
    const captcha = q("[data-pwd-captcha]").value.trim();

    if (!oldPassword) {
      Toast.error("请输入当前密码");
      return;
    }
    const problem = passwordProblem(newPassword);
    if (problem) {
      Toast.error(problem);
      return;
    }
    if (newPassword !== confirm) {
      Toast.error("两次输入的新密码不一致");
      return;
    }
    if (emailFactorOn() && !code) {
      Toast.error("请填写邮箱验证码");
      return;
    }
    if (!captcha) {
      Toast.error("请输入图形验证码");
      return;
    }

    btn.disabled = true;
    try {
      await api.put("/api/me/password", {
        old_password: oldPassword,
        new_password: newPassword,
        emailCode: code,
        captchaId: pwdCaptchaId,
        captcha,
      });
      Toast.success("密码已更新，其它设备需要重新登录");
      pwdPanel.hidden = true;
    } catch (err) {
      Toast.error(err?.message || "修改失败");
      // 图形码一次一用：失败后立刻换一张并清空输入
      q("[data-pwd-captcha]").value = "";
      fetchCaptcha("pwd");
    } finally {
      btn.disabled = false;
    }
  });

  /* 两步验证开关 */
  function paintTwofa() {
    const toggle = q("[data-twofa-toggle]");
    const enabled = Boolean(security?.twoFactorEnabled ?? currentUser().twoFactorEnabled);
    if (toggle) toggle.checked = enabled;
    const desc = q("[data-twofa-desc]");
    if (desc) {
      desc.textContent = enabled
        ? t("已开启：密码登录后还需要通行密钥或邮箱验证码")
        : t("开启后，用密码登录还要再过一次通行密钥或邮箱验证码");
    }
  }

  function openTwofaPanel(want) {
    twofaWant = want;
    if (!twofaPanel) return;
    if (want && !emailFactorOn() && passkeyList().length === 0) {
      Toast.error("请先绑定并验证邮箱，或添加一个通行密钥");
      return;
    }
    twofaPanel.hidden = false;
    q("[data-twofa-password]").value = "";
    q("[data-twofa-code]").value = "";
    q("[data-twofa-captcha]").value = "";
    const withEmail = emailFactorOn();
    const emailRow = q("[data-twofa-email-row]");
    const captchaRow = q("[data-twofa-captcha-row]");
    if (emailRow) emailRow.hidden = !withEmail;
    if (captchaRow) captchaRow.hidden = withEmail;
    if (!withEmail) fetchCaptcha("twofa");
    const note = q("[data-twofa-note]");
    if (note) {
      note.textContent = want
        ? t("开启前请确认身份：密码 + {by}", { by: withEmail ? t("邮箱验证码") : t("图形验证码") })
        : t("关闭两步验证：密码 + {by}", { by: withEmail ? t("邮箱验证码") : t("图形验证码") });
    }
    q("[data-twofa-password]")?.focus();
  }

  q("[data-twofa-toggle]")?.addEventListener("change", (event) => {
    const input = event.currentTarget;
    const want = input.checked;
    // 先回滚视觉状态，等服务端确认后再勾上，避免「看起来开了其实没开」
    input.checked = !want;
    openTwofaPanel(want);
  });

  q('[data-act="twofa-cancel"]')?.addEventListener("click", () => {
    if (twofaPanel) twofaPanel.hidden = true;
    paintTwofa();
  });
  q("[data-twofa-captcha-refresh]")?.addEventListener("click", () => fetchCaptcha("twofa"));

  q('[data-act="twofa-send"]')?.addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    try {
      await sendCode({ url: "/api/me/email/code", scene: "twofactor", button: btn });
      Toast.success("验证码已发送，请查收邮件");
      q("[data-twofa-code]")?.focus();
    } catch (err) {
      Toast.error(err?.message || "发送失败，请稍后再试");
    }
  });

  q('[data-act="twofa-save"]')?.addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    const password = q("[data-twofa-password]").value;
    const code = q("[data-twofa-code]").value.trim();
    const captcha = q("[data-twofa-captcha]").value.trim();
    if (!password) {
      Toast.error("请输入账户密码");
      return;
    }
    if (emailFactorOn() && !code) {
      Toast.error("请填写邮箱验证码");
      return;
    }
    if (!emailFactorOn() && !captcha) {
      Toast.error("请输入图形验证码");
      return;
    }
    btn.disabled = true;
    try {
      const res = await api.put("/api/me/2fa", {
        enabled: twofaWant,
        password,
        emailCode: code,
        captchaId: twofaCaptchaId,
        captcha,
      });
      applyUser(res.user);
      if (security) security.twoFactorEnabled = Boolean(res.twoFactorEnabled);
      twofaPanel.hidden = true;
      paintTwofa();
      Toast.success(res.twoFactorEnabled ? "两步验证已开启" : "两步验证已关闭");
    } catch (err) {
      Toast.error(err?.message || "操作失败");
      if (!emailFactorOn()) {
        q("[data-twofa-captcha]").value = "";
        fetchCaptcha("twofa");
      }
    } finally {
      btn.disabled = false;
    }
  });

  /* 通行密钥 */
  /* --- 登录设备管理 --- */
  function renderDevices(items) {
    const box = q("[data-device-list]");
    if (!box) return;
    if (!items.length) {
      box.innerHTML = H`<p class="setting-panel__hint">${t("还没有登录记录")}</p>`;
      return;
    }
    box.innerHTML = items
      .map(
        (item) => H`<div class="passkey-item device-item${item.current ? " device-item--current" : ""}">
          <div class="passkey-item__main">
            <div class="passkey-item__label">
              ${escapeHtml(item.device || t("未知设备"))}
              ${item.current ? H`<span class="chip chip--accent">${t("当前设备")}</span>` : ""}
            </div>
            <div class="passkey-item__meta">${t("最近活跃 {time} · IP {ip}", {
              time: formatTime(item.lastSeenAt),
              ip: escapeHtml(item.ip || "—"),
            })}</div>
          </div>
          ${
            item.current
              ? ""
              : H`<button class="btn btn--subtle" type="button" data-act="device-revoke"
                  data-sid="${item.id}">${t("下线")}</button>`
          }
        </div>`
      )
      .join("");
    const othersBtn = q('[data-act="revoke-others"]');
    if (othersBtn) othersBtn.disabled = items.every((x) => x.current);
  }

  async function refreshDevices() {
    try {
      const data = await api.get("/api/me/sessions");
      renderDevices(data.items || []);
    } catch {
      const box = q("[data-device-list]");
      if (box) box.innerHTML = H`<p class="setting-panel__hint">${t("设备列表读取失败")}</p>`;
    }
  }

  q("[data-device-list]")?.addEventListener("click", async (event) => {
    const btn = event.target.closest('[data-act="device-revoke"]');
    if (!btn) return;
    const sid = btn.dataset.sid;
    const ok = await Dialog.confirm({
      title: t("下线这台设备"),
      message: t("下线后那台设备上的登录立刻失效，需要重新输入密码才能进。"),
      confirmText: t("下线"),
      danger: true,
    });
    if (!ok) return;
    btn.disabled = true;
    try {
      await api.del(`/api/me/sessions/${encodeURIComponent(sid)}`);
      Toast.success(t("已下线"));
      await refreshDevices();
    } catch (err) {
      Toast.error(err?.message || t("下线失败"));
      btn.disabled = false;
    }
  });

  q('[data-act="revoke-others"]')?.addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    const ok = await Dialog.confirm({
      title: t("下线其他设备"),
      message: t("除当前这台以外，其它所有设备都会被下线。"),
      confirmText: t("下线其他设备"),
      danger: true,
    });
    if (!ok) return;
    btn.disabled = true;
    try {
      const res = await api.post("/api/me/sessions/revoke-others", {});
      Toast.success(res.revoked ? t("已下线 {n} 台设备", { n: res.revoked }) : t("没有其它登录设备"));
      await refreshDevices();
    } catch (err) {
      Toast.error(err?.message || t("下线失败"));
    } finally {
      btn.disabled = false;
    }
  });

  function renderPasskeys(list) {
    const box = q("[data-passkey-list]");
    if (!box) return;
    if (!list.length) {
      box.innerHTML = H`<p class="setting-panel__hint">${t("还没有添加通行密钥")}</p>`;
      return;
    }
    box.innerHTML = list
      .map(
        (item) => H`<div class="passkey-item">
          <div class="passkey-item__main">
            <div class="passkey-item__label">${escapeHtml(item.label || t("通行密钥"))}</div>
            <div class="passkey-item__meta">${
              item.lastUsedAt ? t("上次使用 {time}", { time: formatTime(item.lastUsedAt) }) : t("还没有用过")
            }</div>
          </div>
          <button class="btn btn--subtle" type="button" data-act="passkey-remove"
                  data-id="${item.id}">${t("移除")}</button>
        </div>`
      )
      .join("");
  }

  async function refreshSecurity() {
    try {
      security = await api.get("/api/me/security");
    } catch {
      return;
    }
    paintTwofa();
    renderPasskeys(security.passkeys || []);

    const note = q("[data-passkey-note]");
    const addBtn = q('[data-act="passkey-add"]');
    const usable = Boolean(security.passkeySupported) && webauthnSupported();
    if (addBtn) addBtn.disabled = !usable;
    if (note) {
      if (!security.passkeySupported) {
        note.hidden = false;
        note.textContent = t("服务端未安装通行密钥依赖（webauthn），暂时无法使用");
      } else if (!webauthnSupported()) {
        note.hidden = false;
        note.textContent = t("通行密钥需要 HTTPS 访问（localhost 除外），当前页面不可用");
      } else {
        note.hidden = true;
      }
    }
    const emailRow = q("[data-passkey-email-row]");
    if (emailRow) emailRow.hidden = !security.emailFactorAvailable;
  }

  q('[data-act="passkey-add"]')?.addEventListener("click", () => {
    if (!passkeyPanel) return;
    passkeyPanel.hidden = false;
    q("[data-passkey-password]").value = "";
    q("[data-passkey-code]").value = "";
    q("[data-passkey-label]").value = "";
    const hint = q("[data-passkey-hint]");
    if (hint) hint.hidden = true;
    q("[data-passkey-password]")?.focus();
  });

  q('[data-act="passkey-cancel"]')?.addEventListener("click", () => {
    if (passkeyPanel) passkeyPanel.hidden = true;
  });

  q('[data-act="passkey-send"]')?.addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    try {
      await sendCode({ url: "/api/me/email/code", scene: "password", button: btn });
      Toast.success("验证码已发送，请查收邮件");
      q("[data-passkey-code]")?.focus();
    } catch (err) {
      Toast.error(err?.message || "发送失败，请稍后再试");
    }
  });

  q('[data-act="passkey-create"]')?.addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    const password = q("[data-passkey-password]").value;
    const code = q("[data-passkey-code]").value.trim();
    const label = q("[data-passkey-label]").value.trim();
    if (!password && !code) {
      Toast.error("请先用密码或邮箱验证码验证身份");
      return;
    }
    if (!webauthnSupported()) {
      Toast.error("当前页面无法使用通行密钥（需要 HTTPS 访问）");
      return;
    }
    btn.disabled = true;
    try {
      const { options, nonce } = await api.post("/api/me/passkeys/options", {
        password,
        emailCode: code,
        label,
      });
      const credential = await createCredential(options);
      const res = await api.post("/api/me/passkeys", { nonce, credential, label });
      if (security) security.passkeys = res.passkeys;
      renderPasskeys(res.passkeys || []);
      passkeyPanel.hidden = true;
      Toast.success("通行密钥已添加。要让它在登录时生效，请打开「两步验证」开关");
    } catch (err) {
      const message = err instanceof ApiError ? err.message : describeWebauthnError(err);
      Toast.error(message);
    } finally {
      btn.disabled = false;
    }
  });

  q("[data-passkey-list]")?.addEventListener("click", async (event) => {
    const btn = event.target.closest('[data-act="passkey-remove"]');
    if (!btn) return;
    const id = Number(btn.dataset.id);
    const values = await Dialog.form({
      title: t("移除通行密钥"),
      message: t("移除后这个设备就不能再用来验证登录了，请输入账户密码确认。"),
      confirmText: t("移除"),
      danger: true,
      fields: [
        {
          name: "password",
          label: t("账户密码"),
          type: "password",
          validate: (v) => (String(v || "") ? null : t("请输入密码")),
        },
      ],
    });
    if (!values) return;
    btn.disabled = true;
    try {
      const res = await api.del(`/api/me/passkeys/${id}`, { password: values.password });
      if (security) security.passkeys = res.passkeys;
      renderPasskeys(res.passkeys || []);
      Toast.success("通行密钥已移除");
    } catch (err) {
      Toast.error(err?.message || "移除失败");
    } finally {
      btn.disabled = false;
    }
  });

  paintTwofa();
  refreshSecurity();
  refreshDevices();


  root.querySelector('[data-act="dav-help"]')?.addEventListener("click", () => {
    Dialog.alert({
      title: "连接 WebDAV",
      tone: "info",
      confirmText: "知道了",
      message: `
        <div class="dav-help">
          <div class="dav-help__row"><b>Windows</b>${t(
            "资源管理器右键「此电脑」→ 映射网络驱动器 → 文件夹填 {url}，或直接写 {unc}；勾选「使用其他凭据连接」，账号密码见上一行。",
            { url: `<code>${davUrl}</code>`, unc: `<code>${davUnc}</code>` }
          )}</div>
          <div class="dav-help__row"><b>macOS</b>${t(
            "Finder → 前往 → 连接服务器（⌘K）→ 输入 {url} → 以「注册用户」登录。",
            { url: `<code>${davUrl}</code>` }
          )}</div>
          <div class="dav-help__row"><b>${t("手机")}</b>${t(
            "Documents / RaiDrive / ES 文件浏览器里新增 WebDAV 连接，地址同上。"
          )}</div>
          <div class="dav-help__row dav-help__note">${t(
            "Windows 的 WebDAV 客户端默认不允许在 HTTP 上发送明文 Basic 密码（会报 {code}），本站已改用 Digest 认证，{noRegistry}。公网使用仍建议套 HTTPS；通过 WebDAV 删除的文件同样会进回收站。",
            { code: `<code>0x80070043</code>`, noRegistry: `<b>${t("不需要改注册表")}</b>` }
          )}</div>
          <div class="dav-help__row dav-help__note">${t(
            "如果资源管理器报「找不到网络名」或一直连不上，先确认 Windows 的 WebClient 服务在运行：管理员身份打开命令提示符，执行 {cmd} 后再试。",
            { cmd: `<code>net start webclient</code>` }
          )}</div>
        </div>`,
    });
  });
  }

  /* --- 界面语言：立即生效 + 记到账号 --- */
  root.querySelectorAll("[data-lang]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.lang;
      if (!id || id === getLang()) return;
      // 先改本地（重绘时读的是 store 里的设置），再派发 i18n:change 重画整个视图
      patchSettingsLocal({ language: id });
      setLang(id);
      try {
        await api.patch("/api/me/settings", { language: id });
      } catch {
        Toast.error(t("语言设置没同步到服务器，本次已切换"));
      }
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

  /* --- 主题色：单色 / 渐变（2–5 个颜色） --- */
  const solidPane = root.querySelector("[data-accent-solid]");
  const gradPane = root.querySelector("[data-accent-gradient]");
  const stopsBox = root.querySelector("[data-accent-stops]");
  const addStopBtn = root.querySelector("[data-accent-add]");
  const modeBtns = [...root.querySelectorAll("[data-accent-mode-btn]")];
  const pickInput = root.querySelector("[data-color-pick]");
  const pickSwatch = root.querySelector("[data-swatch-pick]");
  const pickFromBg = root.querySelector('[data-act="pick-bg"]');
  const aiPick = root.querySelector('[data-act="pick-ai"]');
  const aiPickLabel = root.querySelector("[data-ai-pick-label]");
  const hexInput = root.querySelector("[data-accent-hex]");

  let editColors = [...gradColors];

  const currentAccent = () => (store.get().user?.settings?.accentColor || accent).toLowerCase();

  /** 手输色值：容忍少写 #、大小写、以及 #abc 三位缩写；认不出来返回空串 */
  function normalizeHex(raw) {
    let hex = String(raw || "").trim().toLowerCase().replace(/\s+/g, "");
    if (!hex) return "";
    if (!hex.startsWith("#")) hex = `#${hex}`;
    if (/^#[0-9a-f]{3}$/.test(hex)) {
      hex = `#${hex
        .slice(1)
        .split("")
        .map((c) => c + c)
        .join("")}`;
    }
    return /^#[0-9a-f]{6}$/.test(hex) ? hex : "";
  }

  function cleanColors(list) {
    return [
      ...new Set(
        list.map((c) => String(c || "").toLowerCase()).filter((c) => /^#[0-9a-f]{6}$/.test(c))
      ),
    ].slice(0, 5);
  }

  /** 保存：主色始终跟着第一个颜色走，老的按主色渲染的地方不会失联 */
  function saveAccent(mode, colors) {
    const list = cleanColors(colors);
    const finalColors = list.length ? list : [currentAccent()];
    const realMode = mode === "gradient" && finalColors.length >= 2 ? "gradient" : "solid";
    persist({
      accentColor: finalColors[0],
      accentStyle: { mode: realMode, colors: finalColors },
    });
  }

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
    if (hexInput) hexInput.value = hex;
  }

  function paintStops() {
    if (!stopsBox) return;
    stopsBox.innerHTML = editColors
      .map(
        (color, index) => H`<span class="accent-stop" style="--stop:${color}">
          <span class="accent-stop__chip" title="${t("打开取色板挑颜色")}">
            <input type="color" value="${color}" data-accent-stop="${index}"
                   aria-label="${t("渐变颜色 {n}", { n: index + 1 })}">
          </span>
          <input class="accent-hex accent-hex--stop" value="${color}" data-accent-stop-hex="${index}"
                 maxlength="7" spellcheck="false" aria-label="${t("渐变颜色 {n}", { n: index + 1 })}">
          <button class="accent-stop__del" type="button" data-accent-stop-del="${index}"
                  title="${t("移除这个颜色")}" aria-label="${t("移除这个颜色")}"
                  ${editColors.length <= 2 ? "disabled" : ""}>
            <svg class="icon icon--s"><use href="#i-close"/></svg>
          </button>
        </span>`
      )
      .join("");
    if (addStopBtn) addStopBtn.disabled = editColors.length >= 5;
  }

  function paintMode(mode) {
    modeBtns.forEach((btn) =>
      btn.classList.toggle("is-active", btn.dataset.accentModeBtn === mode)
    );
    if (solidPane) solidPane.hidden = mode !== "solid";
    if (gradPane) gradPane.hidden = mode !== "gradient";
  }

  /** 切到渐变时补够两个颜色，单张背景图取色也能用 */
  function ensureGradientColors() {
    if (editColors.length >= 2) return;
    const base = editColors[0] || currentAccent();
    editColors = [base, accentCompanion(base)];
    paintStops();
  }

  function applyGradient(colors) {
    editColors = cleanColors(colors);
    ensureGradientColors();
    paintMode("gradient");
    paintStops();
    saveAccent("gradient", editColors);
  }

  // 看板娘对话里改了主题色（值已经落库）：把这块控件同步过去，别两处显示不一样
  window.addEventListener(
    "accent:change",
    () => {
      const live = accentStyleOf(store.get().user?.settings || {});
      const first = live.colors[0] || currentAccent();
      editColors = live.mode === "gradient" ? [...live.colors] : [first, accentCompanion(first)];
      paintMode(live.mode);
      paintStops();
      markAccent(first);
    },
    keep
  );

  modeBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.accentModeBtn;
      if (mode === "gradient") ensureGradientColors();
      paintMode(mode);
      const main = editColors[0] || currentAccent();
      // 切回单色时把色板高亮和色值框同步到当前主色，别留着上一个颜色
      if (mode === "solid") markAccent(main);
      saveAccent(mode, mode === "solid" ? [main] : editColors);
    });
  });

  function applyAccent(color) {
    const hex = String(color || "").toLowerCase();
    if (!/^#[0-9a-f]{6}$/.test(hex)) return;
    markAccent(hex);
    editColors = [hex];
    persist({ accentColor: hex, accentStyle: { mode: "solid", colors: [hex] } });
  }

  root.querySelectorAll("[data-color]").forEach((btn) => {
    btn.addEventListener("click", () => applyAccent(btn.dataset.color));
  });

  // 取色器拖动时只本地预览，松手（change）才落库，避免一路刷接口
  const previewSolid = (hex) =>
    patchSettingsLocal({ accentColor: hex, accentStyle: { mode: "solid", colors: [hex] } });

  pickInput?.addEventListener("input", () => {
    const hex = pickInput.value.toLowerCase();
    markAccent(hex);
    previewSolid(hex);
  });
  pickInput?.addEventListener("change", () => applyAccent(pickInput.value));

  /* 手输色值（单色）：输入时先预览，回车或失焦才落库 */
  let hexTimer = 0;
  hexInput?.addEventListener("input", () => {
    const hex = normalizeHex(hexInput.value);
    if (!hex) return;
    window.clearTimeout(hexTimer);
    hexTimer = window.setTimeout(() => {
      markAccent(hex);
      editColors = [hex];
      previewSolid(hex);
    }, 160);
  });
  hexInput?.addEventListener("change", () => {
    const hex = normalizeHex(hexInput.value);
    window.clearTimeout(hexTimer);
    if (!hex) {
      Toast.warn(t("色值要写成 #rrggbb 这样"));
      hexInput.value = currentAccent();
      return;
    }
    applyAccent(hex);
  });
  hexInput?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    hexInput.blur();
  });

  /* 渐变：颜色块用事件委托，重绘 DOM 也不会丢监听 */
  stopsBox?.addEventListener("input", (event) => {
    const input = event.target.closest("[data-accent-stop]");
    if (!input) return;
    const index = Number(input.dataset.accentStop);
    if (!Number.isInteger(index) || index < 0 || index >= editColors.length) return;
    editColors[index] = input.value.toLowerCase();
    const stop = input.closest(".accent-stop");
    stop?.style.setProperty("--stop", editColors[index]);
    const hexField = stop?.querySelector("[data-accent-stop-hex]");
    if (hexField) hexField.value = editColors[index];
    patchSettingsLocal({
      accentColor: editColors[0],
      accentStyle: { mode: "gradient", colors: [...editColors] },
    });
  });
  stopsBox?.addEventListener("change", (event) => {
    if (!event.target.closest("[data-accent-stop]")) return;
    saveAccent("gradient", editColors);
  });

  /* 手输色值（渐变里的每一档） */
  stopsBox?.addEventListener("input", (event) => {
    const field = event.target.closest("[data-accent-stop-hex]");
    if (!field) return;
    const index = Number(field.dataset.accentStopHex);
    const hex = normalizeHex(field.value);
    if (!Number.isInteger(index) || index < 0 || index >= editColors.length || !hex) return;
    editColors[index] = hex;
    const stop = field.closest(".accent-stop");
    stop?.style.setProperty("--stop", hex);
    const chip = stop?.querySelector("[data-accent-stop]");
    if (chip) chip.value = hex;
    patchSettingsLocal({
      accentColor: editColors[0],
      accentStyle: { mode: "gradient", colors: [...editColors] },
    });
  });
  stopsBox?.addEventListener("change", (event) => {
    const field = event.target.closest("[data-accent-stop-hex]");
    if (!field) return;
    const index = Number(field.dataset.accentStopHex);
    const hex = normalizeHex(field.value);
    if (!hex) {
      Toast.warn(t("色值要写成 #rrggbb 这样"));
      field.value = editColors[index] || "";
      return;
    }
    editColors[index] = hex;
    field.value = hex;
    paintStops();
    saveAccent("gradient", editColors);
  });
  stopsBox?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !event.target.closest("[data-accent-stop-hex]")) return;
    event.preventDefault();
    event.target.blur();
  });
  stopsBox?.addEventListener("click", (event) => {
    const del = event.target.closest("[data-accent-stop-del]");
    if (!del || editColors.length <= 2) return;
    const index = Number(del.dataset.accentStopDel);
    editColors.splice(index, 1);
    paintStops();
    saveAccent("gradient", editColors);
  });

  addStopBtn?.addEventListener("click", () => {
    if (editColors.length >= 5) return;
    editColors.push(accentCompanion(editColors[editColors.length - 1] || currentAccent()));
    paintStops();
    saveAccent("gradient", editColors);
  });

  root.querySelectorAll("[data-accent-preset]").forEach((btn) => {
    btn.addEventListener("click", () => applyGradient(btn.dataset.accentPreset.split(",")));
  });

  pickFromBg?.addEventListener("click", async () => {
    const url = store.get().user?.settings?.backgroundImage;
    if (!url) {
      Toast.error("先上传一张背景图");
      return;
    }
    pickFromBg.disabled = true;
    try {
      const img = await loadImageUrl(url);
      // 背景图取色默认走渐变：一张图里通常有两三个能用的色调
      const colors = dominantColors(img, 3);
      if (!colors.length) {
        Toast.error("这张图取不出颜色，换一张试试");
        return;
      }
      applyGradient(colors);
      Toast.success(t("已从背景取到 {n} 个颜色，也可以自己调整", { n: editColors.length }));
    } catch {
      Toast.error("背景图读取失败，请重新上传");
    } finally {
      pickFromBg.disabled = false;
    }
  });

  /* --- AI 取色：把背景图交给看板娘用的模型，让它挑单色 / 渐变 --- */
  const accentModeNow = () => (gradPane && !gradPane.hidden ? "gradient" : "solid");

  async function pickAccentByAi() {
    if (!aiPick || aiPick.disabled) return;
    const url = store.get().user?.settings?.backgroundImage;
    if (!url) {
      // 没图就没得看，先提示，别白跑一次接口
      Toast.warn(t("还没设置背景图，AI 取色用不了（去「设置 → 外观」换一张自定义背景图）"));
      return;
    }
    aiPick.disabled = true;
    if (aiPickLabel) aiPickLabel.textContent = t("AI 正在挑颜色…");
    try {
      let cfg = null;
      try {
        const res = await api.get("/api/ai/config");
        // 接口回的是 { config, isAdmin, systemPrompt }，配置在里层；别直接拿外层当配置
        cfg = res?.config ?? res;
      } catch {
        cfg = null;
      }
      if (cfg && cfg.aiEnabled === false) {
        Toast.warn(t("管理员已关闭 AI 功能"));
        return;
      }
      // 只有「明确读到配置且不可用」才拦；读配置失败就往下走，让服务端给出准确原因
      if (cfg && cfg.ready === false) {
        // 取色用的是看板娘对话那套接口，提示直接指向配置入口，别让人到处找
        Toast.warn(t("还没填 AI 接口，去「设置 → 看板娘 · 对话设置」填好就能取色啦~"));
        return;
      }
      const img = await loadImageUrl(url);
      const candidates = dominantColors(img, 8);
      const image = snapshotDataUrl(img, 768, 0.82);
      if (!image) {
        Toast.error(t("背景图读取失败，请重新上传"));
        return;
      }
      const result = await api.post("/api/ai/accent", {
        mode: accentModeNow(),
        image,
        candidates,
        lang: getLang(),
      });
      const colors = Array.isArray(result?.colors) ? result.colors : [];
      if (!colors.length) {
        Toast.error(t("AI 没挑出可用的颜色，换个模型再试试"));
        return;
      }
      if (result.mode === "gradient" && colors.length >= 2) applyGradient(colors);
      else applyAccent(colors[0]);
      if (result.source === "sampled") {
        // 模型这轮没吐颜色，服务端拿壁纸里采到的候选色兜的底 —— 如实说一声
        Toast.warn(t("模型没返回颜色，先用从背景图里提取的颜色；想更准可以换个模型再试"));
        return;
      }
      Toast.success(
        colors.length > 1
          ? t("AI 挑了 {n} 个颜色，不满意可以自己改", { n: colors.length })
          : t("AI 挑了 {color}，不满意可以自己改", { color: colors[0] })
      );
    } catch (err) {
      Toast.error(err?.message || t("AI 取色失败了，稍后再试"));
    } finally {
      aiPick.disabled = false;
      if (aiPickLabel) aiPickLabel.textContent = t("AI 取色");
    }
  }
  aiPick?.addEventListener("click", pickAccentByAi);

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
    if (bgThumb) bgThumb.style.backgroundImage = url ? H`url("${url}")` : "";
    const plus = root.querySelector("[data-bg-plus]");
    if (plus) plus.hidden = !!url;
    if (bgName) bgName.textContent = url ? t("自定义") : t("上传图片");
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

      // 主题色跟着背景图走：默认取渐变（一张图通常有两三个能用的色调）
      const colors = bitmap ? dominantColors(bitmap, 3) : [];
      if (colors.length) {
        applyGradient(colors);
        Toast.success(t("背景已更新，主题色改用图片渐变"));
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

  /* --- 滚轮缩放开关：和 live2d 的滚轮监听共用一份状态（按设备记） --- */
  const wheelSwitch = root.querySelector("[data-mascot-wheel]");
  wheelSwitch?.addEventListener("change", () => {
    setMascotWheelZoom(wheelSwitch.checked);
    Toast.info(
      wheelSwitch.checked
        ? t("滚轮缩放已开启，鼠标停在她身上滚一下试试")
        : t("滚轮缩放已关闭，滚轮恢复滚动页面")
    );
  });

  // 滚轮改了大小，这里滑块与百分比要跟着动
  const onMascotResize = (event) => {
    if (view.signal.aborted) return;
    const percent = Number(event.detail?.percent);
    if (!mascotSize || !Number.isFinite(percent)) return;
    mascotSize.value = String(percent);
    paintMascotSize();
  };
  window.addEventListener("mascot:resize", onMascotResize, keep);

  /* --- 重置看板娘位置：清掉本机记住的偏移，交给 live2d 模块套用 --- */
  root.querySelector("[data-mascot-reset]")?.addEventListener("click", () => {
    window.dispatchEvent(new CustomEvent("mascot:reset"));
    Toast.info(t("看板娘已回到默认位置"));
  });

  /* --- 看板娘锁定（鼠标穿透）：和悬停条上的「锁定」共用一份状态 --- */
  const lockSwitch = root.querySelector("[data-mascot-lock]");
  lockSwitch?.addEventListener("change", () => setMascotLocked(lockSwitch.checked));
  const onMascotLock = (event) => {
    if (view.signal.aborted) return;
    if (lockSwitch) lockSwitch.checked = Boolean(event.detail?.locked);
  };
  window.addEventListener("mascot:lock", onMascotLock, keep);

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
        ? t(" · 已发现 {n} 个：{names}", {
            n: names.length,
            names: names.join(langTag() === "zh-CN" ? "、" : ", "),
          })
        : t(" · 目录里还没有模型，先用云朵兜底");
    } catch {
      /* 拿不到就不提示，不影响设置项本身 */
    }
  })();

  /* --- 开关 --- */
  const notifyPermChip = root.querySelector("[data-notify-perm]");
  const notifyPermDesc = root.querySelector("[data-notify-desc]");
  const NOTIFY_DESC = notifyPermDesc?.textContent || "";

  /** 浏览器通知权限：未授权 / 已授权 / 被拒绝 / 不支持，四种状态都直说 */
  function paintNotifyPerm() {
    if (!notifyPermChip) return;
    const state = desktopPermission();
    notifyPermChip.hidden = false;
    notifyPermChip.className = "chip";
    if (state === "granted") {
      notifyPermChip.textContent = t("已授权");
      notifyPermChip.classList.add("chip--accent");
      if (notifyPermDesc) notifyPermDesc.textContent = NOTIFY_DESC;
    } else if (state === "denied") {
      notifyPermChip.textContent = t("已被浏览器拒绝");
      notifyPermChip.classList.add("chip--danger");
      if (notifyPermDesc) {
        notifyPermDesc.textContent = t(
          "浏览器里已经拒绝过通知：点地址栏左侧的图标 → 通知 → 允许，再回来打开开关。"
        );
      }
    } else if (state === "unsupported") {
      notifyPermChip.textContent = t("浏览器不支持");
      if (notifyPermDesc) notifyPermDesc.textContent = t("这个浏览器不支持系统通知，站内提醒仍然可用。");
    } else {
      notifyPermChip.textContent = t("未授权");
      if (notifyPermDesc) notifyPermDesc.textContent = NOTIFY_DESC;
    }
  }
  paintNotifyPerm();

  root.querySelectorAll("[data-toggle]").forEach((input) => {
    input.addEventListener("change", () => {
      const key = input.dataset.toggle;
      if (key === "trashEnabled") return; // 回收站开关有独立的二次确认流程
      persist({ [key]: input.checked });
      if (key === "notifyDesktop" && input.checked) {
        // 开关必须和浏览器授权一致：没授权就等于收不到，别让它停在「开着」的假象
        void (async () => {
          const state = await askDesktopPermission();
          paintNotifyPerm();
          if (state !== "granted") {
            Toast.error(t("浏览器没有授权通知，暂时收不到系统提醒"));
            input.checked = false;
            persist({ notifyDesktop: false });
          }
        })();
      }
      if (key === "notifyInapp" || key === "notifyDesktop") resetNotifierCursor();
      if (key === "showMascot") {
        document.getElementById("mascot")?.classList.toggle("is-hidden", !input.checked);
      }
      if (key === "webdavEnabled") {
        // 关掉后地址与凭据先收起来，避免"关了还能复制"的错觉
        root.querySelector("[data-dav-body]")?.toggleAttribute("hidden", !input.checked);
        if (input.checked) Toast.success(t("WebDAV 已开启，可以继续用原来的地址挂载"));
        else Toast.info(t("WebDAV 已关闭，你的文件不受影响"));
      }
    });
  });

  /* --- 快传：收到后存到哪（目录选择 + 恢复默认） --- */
  const quickPathEl = root.querySelector("[data-quick-folder-path]");
  const quickResetBtn = root.querySelector('[data-act="quick-folder-reset"]');

  function paintQuickPath(path) {
    // 默认位置的两段（我的文件 / 快传的文件）是系统给的目录名，要跟着界面语言走；
    // 用户自建目录翻译不到就原样返回（t 的兜底行为）
    const parts = (path?.path || []).map((part) => t(part.name));
    if (quickPathEl) {
      quickPathEl.textContent = parts.length ? parts.join(" / ") : t("我的文件 / 快传的文件");
    }
    if (quickResetBtn) quickResetBtn.toggleAttribute("hidden", path ? path.isDefault !== false : true);
  }

  async function saveQuickFolder(folderId) {
    try {
      // 选根目录等于「恢复默认」：默认位置就是 我的文件/快传的文件
      const data = await api.put("/api/quick/inbox-folder", { folderId });
      paintQuickPath(data?.path);
      Toast.success(
        folderId
          ? t("收到后存到「{path}」", { path: quickPathEl?.textContent || "" })
          : t("已恢复默认位置：{path}", { path: quickPathEl?.textContent || "" })
      );
    } catch (err) {
      Toast.error(err.message);
    }
  }

  root.querySelector('[data-act="quick-folder"]')?.addEventListener("click", async () => {
    const picked = await pickFolder({ title: "选择收件目录", confirmText: "存到这里" });
    if (picked === undefined) return;
    await saveQuickFolder(picked);
  });
  quickResetBtn?.addEventListener("click", () => saveQuickFolder(null));
  api
    .get("/api/quick/receive")
    .then((data) => paintQuickPath(data?.receive?.path))
    .catch(() => {
      /* 拿不到就保持默认文案 */
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
        message: `${t("关闭后删除的文件<b>不会</b>进入回收站，而是直接彻底删除，无法恢复。")}<br>
          ${t("请把下面这串验证码原样输入到输入框，完成确认：")}
          <div class="code-display" data-code>${challenge.code}</div>
          <div class="dialog__hint">${t("由服务器生成，{n} 分钟内有效，验证一次即作废", { n: minutes })}</div>`,
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

  root.querySelector('[data-act="open-profile"]')?.addEventListener("click", () => {
    location.hash = `#/profile/${user?.id || ""}`;
  });

  /* --- 注销账号：先说清后果，再在面板里验密码 + 邮箱验证码 --- */
  const deletePanel = q("[data-delete-panel]");
  const deleteCodeRow = q("[data-delete-code-row]");
  const deleteEmailOn = () => deleteNeedsCode;
  const deleteHint = (msg) => {
    const hint = q("[data-delete-hint]");
    if (!hint) return;
    hint.hidden = !msg;
    hint.textContent = msg || "";
  };

  function closeDeletePanel() {
    if (!deletePanel) return;
    deletePanel.hidden = true;
    const pwd = q("[data-delete-password]");
    const code = q("[data-delete-code]");
    if (pwd) pwd.value = "";
    if (code) code.value = "";
    deleteHint("");
  }

  root.querySelector('[data-act="delete-account"]')?.addEventListener("click", async () => {
    const res = await Dialog.confirm({
      title: t("注销账号"),
      message: H`
        <p>${t("这一步会把 {name} 这个账号永久删除，无法撤销：", {
          name: `<b>${escapeHtml(currentUser().username || "")}</b>`,
        })}</p>
        <ul class="danger-list">
          <li>${t("登录名会释放，之后任何人都可能注册它，你的旧链接与分享也会失效")}</li>
          <li>${t("网盘里的全部文件、相册、分享链接、快传送出的文件与取件码一起删除")}</li>
          <li>${t("看板娘的历史对话与长期记忆、意见反馈、封禁申诉一并清除")}</li>
          <li>${t("如果同一份文件别人也在用，别人那份不受影响（磁盘上只存一份）")}</li>
        </ul>
        <p>${t("确认要继续的话，下一步需要再验证一次身份。")}</p>`,
      confirmText: t("我了解，继续"),
      cancelText: t("再想想"),
      danger: true,
    });
    if (!res || !deletePanel) return;
    deletePanel.hidden = false;
    if (deleteCodeRow) deleteCodeRow.hidden = !deleteEmailOn();
    deleteHint(
      deleteEmailOn()
        ? ""
        : t("这个账号没有已验证的邮箱，或站点未配置邮件服务，输入密码即可确认是本人操作。")
    );
    deletePanel.scrollIntoView({ block: "nearest", behavior: "smooth" });
    q("[data-delete-password]")?.focus();
  });

  q('[data-act="delete-cancel"]')?.addEventListener("click", closeDeletePanel);

  q('[data-act="delete-send"]')?.addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    const password = q("[data-delete-password]")?.value || "";
    if (!password) {
      deleteHint(t("请先填写登录密码，再发送验证码"));
      q("[data-delete-password]")?.focus();
      return;
    }
    try {
      await sendCode({
        url: "/api/me/email/code",
        scene: "delete",
        body: { password },
        button: btn,
      });
      Toast.success("验证码已发送，请查收邮件");
      deleteHint("");
      q("[data-delete-code]")?.focus();
    } catch (err) {
      Toast.error(err?.message || t("发送失败，请稍后再试"));
    }
  });

  q('[data-act="delete-confirm"]')?.addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    const password = q("[data-delete-password]")?.value || "";
    const code = (q("[data-delete-code]")?.value || "").trim();

    if (!password) {
      deleteHint(t("请输入登录密码"));
      q("[data-delete-password]")?.focus();
      return;
    }
    if (deleteEmailOn() && !/^\d{6}$/.test(code)) {
      deleteHint(t("请输入 6 位邮箱验证码"));
      q("[data-delete-code]")?.focus();
      return;
    }

    btn.disabled = true;
    deleteHint("");
    try {
      await api.post("/api/me/delete", { password, emailCode: code });
      Toast.success(t("账号已注销，正在退出…"));
      // 服务端已经清了 cookie，这里整页重载回到登录页，顺便丢掉内存里的用户态
      window.setTimeout(() => window.location.assign("/"), 700);
    } catch (err) {
      btn.disabled = false;
      deleteHint(err.message || t("注销失败，请稍后再试"));
    }
  });

  /* --- 标签页：只切 class / hidden，不重建 DOM ---
     放在最后：showPane 里要补排主题模式的滑块，得等 layoutThumb 那套 const 初始化完 */
  const tabBtns = [...root.querySelectorAll("[data-settings-tab]")];
  const tabSeg = root.querySelector("[data-settings-tabs]");
  const tabThumb = root.querySelector("[data-tab-thumb]");
  const panes = [...root.querySelectorAll("[data-settings-pane]")];

  function layoutTabs() {
    const active = tabBtns.find((b) => b.classList.contains("is-active"));
    if (!active || !tabThumb || !tabSeg) return;
    // 滑块的 left/top:0 是 .segmented 的 padding box 原点，所以要扣掉边框；
    // 宽高按按钮实测写，页签高度/内边距怎么改都不会错位
    const sr = tabSeg.getBoundingClientRect();
    const ar = active.getBoundingClientRect();
    tabThumb.style.width = `${ar.width}px`;
    tabThumb.style.height = `${ar.height}px`;
    tabThumb.style.transform = `translate(${ar.left - sr.left - tabSeg.clientLeft}px, ${
      ar.top - sr.top - tabSeg.clientTop
    }px)`;
  }

  function showPane(id, { remember = true } = {}) {
    if (!tabBtns.some((b) => b.dataset.settingsTab === id)) id = "appearance";
    tabBtns.forEach((b) => b.classList.toggle("is-active", b.dataset.settingsTab === id));
    panes.forEach((p) => {
      p.hidden = p.dataset.settingsPane !== id;
    });
    layoutTabs();
    // 主题模式分段控件的滑块靠 offsetWidth 定位：外观页藏着时量出来是 0，
    // 切回来必须补排一次，否则滑块缩成一团
    if (id === "appearance") layoutThumb();
    if (remember) {
      try {
        localStorage.setItem("settings-tab", id);
      } catch {
        /* 隐私模式写不进去就算了 */
      }
    }
  }

  tabBtns.forEach((btn) => {
    btn.addEventListener("click", () => showPane(btn.dataset.settingsTab));
  });

  /* --- 设置项搜索：跨所有页签模糊匹配，点结果直接跳过去 ---
     索引不预先建：AI 配置那块是异步挂进来的，每次输入时现扫 DOM 才扫得到 */
  const searchWrap = root.querySelector("[data-settings-search-wrap]");
  const searchInput = root.querySelector("[data-settings-search]");
  const searchClear = root.querySelector("[data-settings-clear]");
  const hitsEl = root.querySelector("[data-settings-hits]");
  let hitEntries = [];
  let hitActive = -1;

  function collectSettings(boxes) {
    const out = [];
    boxes.forEach((pane) => {
      const key = pane.dataset.settingsPane;
      const tabName = (tabBtns.find((b) => b.dataset.settingsTab === key)?.textContent || key).trim();
      pane.querySelectorAll(".setting-row").forEach((row) => {
        const card = (row.closest(".settings-card")?.querySelector(".settings-card__head")?.textContent || "").trim();
        const label = (row.querySelector(".setting-row__label")?.textContent || "").trim().replace(/\s+/g, " ");
        const desc = (row.querySelector(".setting-row__desc")?.textContent || "").trim().replace(/\s+/g, " ");
        // 整行就是控件、没有 label 的，用说明开头当标题
        const title = label || desc.split(/[，。；;]/)[0].slice(0, 26);
        if (!title) return;
        out.push({ key, tabName, card, title, desc, hay: `${card} ${title} ${desc}`, row });
      });
    });
    return out;
  }

  function hideHits() {
    if (hitsEl) {
      hitsEl.hidden = true;
      hitsEl.innerHTML = "";
    }
    hitActive = -1;
  }

  function markActive(next) {
    if (!hitsEl || !hitEntries.length) return;
    hitActive = (next + hitEntries.length) % hitEntries.length;
    [...hitsEl.querySelectorAll("[data-hit]")].forEach((el, index) => {
      el.classList.toggle("is-active", index === hitActive);
    });
    hitsEl.querySelector(`[data-hit="${hitActive}"]`)?.scrollIntoView({ block: "nearest" });
  }

  function gotoSetting(entry) {
    if (!entry) return;
    if (searchInput) searchInput.value = "";
    if (searchClear) searchClear.hidden = true;
    hideHits();
    showPane(entry.key);
    // 页签刚从 hidden 变可见，滚动要等一帧，不然量到的位置还是 0
    window.requestAnimationFrame(() => {
      entry.row.scrollIntoView({ block: "center", behavior: "smooth" });
      entry.row.classList.add("is-hit");
      window.setTimeout(() => entry.row.classList.remove("is-hit"), 2400);
    });
  }

  function renderHits() {
    if (!hitsEl || !searchInput) return;
    const keyword = searchInput.value.trim();
    if (searchClear) searchClear.hidden = !keyword;
    if (!keyword) {
      hideHits();
      return;
    }
    const tokens = splitTokens(keyword);
    hitEntries = collectSettings(panes)
      .map((entry, index) => ({ ...entry, got: fuzzyScore(entry.hay, keyword, tokens), index }))
      .filter((entry) => entry.got > 0)
      .sort((a, b) => b.got - a.got || a.index - b.index)
      .slice(0, 20);

    if (!hitEntries.length) {
      hitsEl.innerHTML = H`<div class="settings-hits__empty">${t("没有匹配的设置项，换个词试试")}</div>`;
      hitsEl.hidden = false;
      hitActive = -1;
      return;
    }
    hitsEl.innerHTML = hitEntries
      .map(
        (entry, index) => H`
        <button class="settings-hit ${index === 0 ? "is-active" : ""}" type="button" data-hit="${index}">
          <span class="settings-hit__label">${highlight(entry.title, keyword)}</span>
          <span class="settings-hit__where">${escapeHtml(entry.tabName)}${entry.card ? ` › ${escapeHtml(entry.card)}` : ""}</span>
        </button>`
      )
      .join("");
    hitsEl.hidden = false;
    hitActive = 0;
  }

  searchInput?.addEventListener("input", () => {
    window.requestAnimationFrame(renderHits);
  });
  searchInput?.addEventListener("focus", () => {
    if (searchInput.value.trim()) renderHits();
  });
  searchInput?.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (hitsEl?.hidden) return;
      event.preventDefault();
      markActive(hitActive + (event.key === "ArrowDown" ? 1 : -1));
    } else if (event.key === "Enter") {
      if (hitsEl?.hidden) return;
      event.preventDefault();
      gotoSetting(hitEntries[hitActive]);
    } else if (event.key === "Escape") {
      searchInput.value = "";
      if (searchClear) searchClear.hidden = true;
      hideHits();
      searchInput.blur();
    }
  });
  searchClear?.addEventListener("click", () => {
    if (searchInput) {
      searchInput.value = "";
      searchInput.focus();
    }
    searchClear.hidden = true;
    hideHits();
  });
  hitsEl?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-hit]");
    if (!btn) return;
    gotoSetting(hitEntries[Number(btn.dataset.hit)]);
  });
  // 点别处收起结果；点搜索框本身不收起（用 composedPath，弹层里点也能正确判断）
  document.addEventListener(
    "click",
    (event) => {
      if (view.signal.aborted) return;
      if (!searchWrap || event.composedPath().includes(searchWrap)) return;
      if (searchInput?.value.trim()) hideHits();
    },
    keep
  );

  // 首帧别让滑块从「0 宽、贴在左上角」滑出来：先关掉过渡排一次，下一帧恢复
  if (tabThumb) tabThumb.style.transition = "none";

  // 记住上次的页签（localStorage，按设备记）；i18n 切语言整页重绘后也回到同一页。
  // 带子段的地址（#/settings/mascot，CDP娘页的「看板娘设置」）优先。
  let initialTab = tab || "appearance";
  try {
    initialTab = tab || localStorage.getItem("settings-tab") || "appearance";
  } catch {
    /* 读不到就停在第一页 */
  }
  showPane(initialTab, { remember: false });

  // 窗口变化时 app.js 只会重排页面里第一个分段控件（现在是页签条），
  // 主题模式的滑块得自己补
  window.addEventListener(
    "resize",
    () => {
      if (view.signal.aborted) return;
      layoutTabs();
      const appearancePane = panes.find((p) => p.dataset.settingsPane === "appearance");
      if (appearancePane && !appearancePane.hidden) layoutThumb();
    },
    keep
  );

  window.requestAnimationFrame(() => {
    layoutTabs();
    layoutThumb();
    // 首帧排完再放开过渡，之后切页签才有滑动
    if (tabThumb) tabThumb.style.transition = "";
  });
}
