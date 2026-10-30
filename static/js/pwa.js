/**
 * PWA 支持：注册 Service Worker、接管安装入口
 *
 * - 注册放在 window load 之后，不抢首屏带宽
 * - 记录 beforeinstallprompt，让「安装到桌面」菜单项能按需出现
 * - iOS 没有 beforeinstallprompt，退回一段手动添加的说明
 */

let deferredPrompt = null;
const listeners = new Set();

export function isStandalone() {
  return (
    window.matchMedia?.("(display-mode: standalone)")?.matches ||
    window.matchMedia?.("(display-mode: minimal-ui)")?.matches ||
    window.navigator.standalone === true
  );
}

function isIos() {
  const ua = navigator.userAgent || "";
  const ios = /iPad|iPhone|iPod/.test(ua) || (ua.includes("Macintosh") && "ontouchend" in document);
  // 只有 Safari 能「添加到主屏幕」，Chrome for iOS 不行
  return ios && !/CriOS|FxiOS|EdgiOS/.test(ua);
}

/** 当前安装能力快照 */
export function installState() {
  const installed = isStandalone();
  return {
    installed,
    canInstall: !installed && Boolean(deferredPrompt),
    iosManual: !installed && !deferredPrompt && isIos(),
  };
}

function emit() {
  const state = installState();
  listeners.forEach((fn) => {
    try {
      fn(state);
    } catch {
      /* 单个订阅者出错不影响其它 */
    }
  });
}

export function onInstallChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/**
 * 触发安装。
 * @returns {"accepted"|"dismissed"|"unavailable"}
 */
export async function promptInstall() {
  if (!deferredPrompt) return "unavailable";
  const event = deferredPrompt;
  deferredPrompt = null;
  try {
    event.prompt();
    const { outcome } = await event.userChoice;
    return outcome;
  } catch {
    return "dismissed";
  } finally {
    emit();
  }
}

/** 注册 Service Worker（幂等，可重复调用） */
export function registerPwa() {
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredPrompt = event;
    emit();
  });

  window.addEventListener("appinstalled", () => {
    deferredPrompt = null;
    emit();
  });

  if (!("serviceWorker" in navigator)) return;

  const register = async () => {
    try {
      const reg = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
      // 回到页面时顺手检查更新，避免长时间开着标签页吃旧版本
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") reg.update().catch(() => {});
      });
    } catch (err) {
      console.warn("[pwa] Service Worker 注册失败", err);
    }
  };

  if (document.readyState === "complete") register();
  else window.addEventListener("load", register, { once: true });
}
