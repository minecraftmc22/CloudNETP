/**
 * 主题：主题模式 / 主题色 / 壁纸 / 花瓣开关
 */
import { store } from "./store.js";

export const ACCENT_PRESETS = [
  { name: "樱紫", value: "#c084fc" },
  { name: "桃粉", value: "#f472b6" },
  { name: "天青", value: "#60a5fa" },
  { name: "薄荷", value: "#34d399" },
  { name: "琥珀", value: "#fbbf24" },
  { name: "珊瑚", value: "#fb7185" },
];

export const WALLPAPERS = [
  { id: "sakura", name: "樱雾" },
  { id: "night", name: "夜樱" },
  { id: "cream", name: "奶油" },
  { id: "plain", name: "素色" },
];

const MEDIA = window.matchMedia("(prefers-color-scheme: dark)");
const CACHE_KEY = "cn:appearance";
const LUMA_KEY = "cn:bgLuma";

/** 可单独调不透明度的模块（键名与 user_settings.surface_opacity 一致） */
export const SURFACE_KEYS = ["nav", "titlebar", "panel", "dialog", "card"];

/** 把 { nav: 0-100, ... } 写进 --op-*；缺省则交回 CSS 里的主题默认值 */
function applySurfaceOpacity(map) {
  const root = document.documentElement;
  for (const key of SURFACE_KEYS) {
    const raw = map?.[key];
    if (typeof raw === "number" && Number.isFinite(raw)) {
      root.style.setProperty(`--op-${key}`, (clamp(raw, 0, 100) / 100).toFixed(3));
    } else {
      root.style.removeProperty(`--op-${key}`);
    }
  }
}

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/* ------------------------------------------------------------------ */
/* 背景图亮度 → 蒙层强度                                                */
/* 亮照片要压得更狠才看得清字，暗照片压狠了又什么都看不见，              */
/* 所以按图片平均亮度算一个蒙层值，存 localStorage 免得每次重新采样。    */
/* ------------------------------------------------------------------ */
function applyScrim(luma) {
  const root = document.documentElement;
  const dark = clamp(0.26 + luma * 0.34, 0.24, 0.6); // 暗色主题：压暗
  const light = clamp(0.26 + (1 - luma) * 0.34, 0.24, 0.6); // 亮色主题：提亮
  root.style.setProperty("--bg-scrim", dark.toFixed(3));
  root.style.setProperty("--bg-veil", light.toFixed(3));
}

function averageLuma(source) {
  const S = 32;
  const canvas = document.createElement("canvas");
  canvas.width = S;
  canvas.height = S;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) return null;
  try {
    ctx.drawImage(source, 0, 0, S, S);
    const px = ctx.getImageData(0, 0, S, S).data;
    let sum = 0;
    let n = 0;
    for (let i = 0; i < px.length; i += 4) {
      if (px[i + 3] < 128) continue;
      sum += (0.2126 * px[i] + 0.7152 * px[i + 1] + 0.0722 * px[i + 2]) / 255;
      n += 1;
    }
    return n ? sum / n : null;
  } catch {
    return null;
  }
}

/** 读缓存里的亮度；没有就取一张图算一次（HTTP 有缓存，不会重复下载） */
export function tuneBackgroundScrim(url) {
  let cache = {};
  try {
    cache = JSON.parse(localStorage.getItem(LUMA_KEY) || "{}");
  } catch {
    cache = {};
  }
  if (typeof cache[url] === "number") {
    applyScrim(cache[url]);
    return;
  }
  const img = new Image();
  img.onload = () => {
    const luma = averageLuma(img);
    if (luma == null) return;
    try {
      localStorage.setItem(LUMA_KEY, JSON.stringify({ ...cache, [url]: luma }));
    } catch {
      /* 存不下就每次重算，不影响显示 */
    }
    applyScrim(luma);
  };
  img.src = url;
}

/** 上传后已经拿到图了，直接算亮度写进缓存，省一次加载 */
export function rememberBackgroundLuma(url, source) {
  const luma = averageLuma(source);
  if (luma == null) return null;
  let cache = {};
  try {
    cache = JSON.parse(localStorage.getItem(LUMA_KEY) || "{}");
  } catch {
    cache = {};
  }
  try {
    localStorage.setItem(LUMA_KEY, JSON.stringify({ ...cache, [url]: luma }));
  } catch {
    /* 忽略 */
  }
  applyScrim(luma);
  return luma;
}

export function resolveTheme(mode) {
  if (mode === 0) return "light";
  if (mode === 1) return "dark";
  return MEDIA.matches ? "dark" : "light";
}

export function applyAppearance(settings = {}) {
  const root = document.documentElement;
  const mode = settings.themeMode ?? 2;
  const theme = resolveTheme(mode);
  // custom 只是“用自定义图片”的标记，图片没了就回落到默认壁纸
  const wantsCustom = settings.wallpaper === "custom";
  const custom = wantsCustom && !!settings.backgroundImage;
  const wallpaper = wantsCustom ? (custom ? "custom" : "sakura") : settings.wallpaper || "sakura";

  root.dataset.theme = theme;
  root.dataset.wallpaper = wallpaper;
  root.dataset.petals = settings.showPetals === false ? "off" : "on";
  root.style.setProperty("--accent-user", settings.accentColor || "#c084fc");
  applySurfaceOpacity(settings.surfaceOpacity);

  if (custom) {
    root.style.setProperty("--bg-image", `url("${settings.backgroundImage}")`);
    tuneBackgroundScrim(settings.backgroundImage);
  } else {
    root.style.removeProperty("--bg-image");
  }

  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "dark" ? "#150f21" : "#fdf4fa");

  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify({ ...settings, wallpaper }));
  } catch {
    /* 隐私模式下忽略 */
  }

  document.dispatchEvent(new CustomEvent("appearance:change", { detail: { theme, settings } }));
}

/** 启动时先用本地缓存 / 系统偏好上色，避免登录页闪白 */
export function applyCachedAppearance() {
  let cached = null;
  try {
    cached = JSON.parse(localStorage.getItem(CACHE_KEY) || "null");
  } catch {
    cached = null;
  }
  if (cached && typeof cached === "object") {
    applyAppearance(cached);
  } else {
    document.documentElement.dataset.theme = MEDIA.matches ? "dark" : "light";
    document.documentElement.dataset.petals = "on";
  }
}

/** 退出登录：自定义背景图要鉴权才能读，登录页挂着它只会 401，顺手摘掉 */
export function dropCachedBackground() {
  let cached = null;
  try {
    cached = JSON.parse(localStorage.getItem(CACHE_KEY) || "null");
  } catch {
    return;
  }
  if (!cached || typeof cached !== "object") return;
  if (!cached.backgroundImage && cached.wallpaper !== "custom") return;
  const next = { ...cached, backgroundImage: null };
  if (next.wallpaper === "custom") next.wallpaper = "sakura";
  applyAppearance(next);
}

export function watchSystemTheme() {
  const handler = () => {
    const user = store.get().user;
    if (!user) return;
    const s = user.settings || {};
    if ((s.themeMode ?? 2) === 2) applyAppearance(s);
  };
  if (MEDIA.addEventListener) MEDIA.addEventListener("change", handler);
  else MEDIA.addListener(handler); // 兼容旧内核
}

export function patchSettingsLocal(patch) {
  const { user } = store.get();
  if (!user) return null;
  const next = { ...user, settings: { ...(user.settings || {}), ...patch } };
  store.set({ user: next });
  applyAppearance(next.settings);
  return next.settings;
}

/* ------------------------------------------------------------------ */
/* 从图片里取主色                                                      */
/* ------------------------------------------------------------------ */

function rgbToHsl(r, g, b) {
  const rr = r / 255;
  const gg = g / 255;
  const bb = b / 255;
  const max = Math.max(rr, gg, bb);
  const min = Math.min(rr, gg, bb);
  const l = (max + min) / 2;
  const d = max - min;
  if (!d) return [0, 0, l];
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h;
  if (max === rr) h = ((gg - bb) / d) % 6;
  else if (max === gg) h = (bb - rr) / d + 2;
  else h = (rr - gg) / d + 4;
  h *= 60;
  return [h < 0 ? h + 360 : h, s, l];
}

function hslToHex(h, s, l) {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const hp = (((h % 360) + 360) % 360) / 60;
  const x = c * (1 - Math.abs((hp % 2) - 1));
  const [r1, g1, b1] =
    hp < 1 ? [c, x, 0] : hp < 2 ? [x, c, 0] : hp < 3 ? [0, c, x]
    : hp < 4 ? [0, x, c] : hp < 5 ? [x, 0, c] : [c, 0, x];
  const m = l - c / 2;
  const to = (v) => Math.round((v + m) * 255).toString(16).padStart(2, "0");
  return `#${to(r1)}${to(g1)}${to(b1)}`;
}

/**
 * 取图中出现最多的颜色（按色相聚类，灰/近白/近黑不参与，避免照片大面积
 * 天空、阴影把主色拉成灰色），再把饱和度和明度收进「适合当主题色」的区间。
 * source: ImageBitmap / HTMLImageElement / HTMLCanvasElement
 */
export function dominantColor(source) {
  const SIZE = 72;
  const canvas = document.createElement("canvas");
  canvas.width = SIZE;
  canvas.height = SIZE;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) return null;
  try {
    ctx.drawImage(source, 0, 0, SIZE, SIZE);
  } catch {
    return null; // 跨域图不能取像素
  }
  let px;
  try {
    px = ctx.getImageData(0, 0, SIZE, SIZE).data;
  } catch {
    return null;
  }

  const buckets = new Map(); // 24 个色相桶
  const plain = new Map(); // 兜底：直接数颜色
  for (let i = 0; i < px.length; i += 4) {
    const a = px[i + 3] / 255;
    if (a < 0.5) continue;
    const r = px[i];
    const g = px[i + 1];
    const b = px[i + 2];
    const key = ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4);
    plain.set(key, (plain.get(key) || 0) + 1);

    const [h, s, l] = rgbToHsl(r, g, b);
    if (l < 0.1 || l > 0.94 || s < 0.14) continue; // 灰白黑跳过
    const idx = Math.min(23, Math.floor((h / 360) * 24));
    const weight = s * (1 - Math.abs(l - 0.56) * 1.1) * a;
    if (weight <= 0) continue;
    const b2 = buckets.get(idx) || { w: 0, r: 0, g: 0, b: 0 };
    b2.w += weight;
    b2.r += r * weight;
    b2.g += g * weight;
    b2.b += b * weight;
    buckets.set(idx, b2);
  }

  let best = null;
  for (const b of buckets.values()) if (!best || b.w > best.w) best = b;

  if (best && best.w > 0) {
    const [h, s, l] = rgbToHsl(best.r / best.w, best.g / best.w, best.b / best.w);
    return hslToHex(h, clamp(s, 0.62, 0.9), clamp(l, 0.62, 0.74));
  }

  // 整张图都偏低饱和：退回最频繁的那一桶，强行提一点饱和度
  let topKey = null;
  let topN = 0;
  for (const [k, n] of plain) if (n > topN) [topKey, topN] = [k, n];
  if (topKey === null) return null;
  const r = ((topKey >> 8) & 15) * 17;
  const g = ((topKey >> 4) & 15) * 17;
  const b = (topKey & 15) * 17;
  const [h, s, l] = rgbToHsl(r, g, b);
  return hslToHex(h, clamp(s, 0.35, 0.8), clamp(l, 0.58, 0.72));
}
