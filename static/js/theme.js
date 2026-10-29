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

export function resolveTheme(mode) {
  if (mode === 0) return "light";
  if (mode === 1) return "dark";
  return MEDIA.matches ? "dark" : "light";
}

export function applyAppearance(settings = {}) {
  const root = document.documentElement;
  const mode = settings.themeMode ?? 2;
  const theme = resolveTheme(mode);

  root.dataset.theme = theme;
  root.dataset.wallpaper = settings.wallpaper || "sakura";
  root.dataset.petals = settings.showPetals === false ? "off" : "on";
  root.style.setProperty("--accent-user", settings.accentColor || "#c084fc");

  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "dark" ? "#150f21" : "#fdf4fa");

  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(settings));
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
