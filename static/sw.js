/**
 * CloudNETP Service Worker
 *
 * 设计原则（配合本项目「手动上传、零构建、频繁改文件」的更新方式）：
 *  1. 页面导航与 /static 下的 JS / CSS 一律 **网络优先**：只要在线就回源，
 *     避免改了样式却因为缓存看到旧界面（这个坑踩过一次）。
 *  2. 只有 /static/vendor/ 与图标这类几乎不变的大文件才缓存优先。
 *  3. /api/、/dav、非 GET、带 Range 的请求（音视频拖动、分片上传）完全不拦。
 *  4. 缓存名带版本号，activate 时清掉旧版本，不会无限堆积。
 *
 * 改版本号时记得同步改这里的 VERSION 与 app/config.py 的 VERSION。
 */
const VERSION = "0.3.72";
const SHELL = `cloudnetp-shell-${VERSION}`;
const RUNTIME = `cloudnetp-runtime-${VERSION}`;
const CACHE_PREFIX = "cloudnetp-";

/** 安装时预缓存的应用外壳（够撑起「离线也能打开界面」） */
const PRECACHE = [
  "/",
  "/static/index.html",
  "/static/share.html",
  "/static/quick.html",
  "/static/offline.html",
  "/terms",
  "/privacy",
  "/manifest.webmanifest",
  "/assets/favicon.ico",
  "/assets/favicon-32.png",
  "/assets/brand-avatar.png",
  "/assets/brand-avatar-256.png",
  "/assets/icon-192.png",
  "/assets/icon-512.png",
  "/assets/icon-maskable-512.png",
  "/assets/apple-touch-icon.png",
  "/static/css/theme.css",
  "/static/css/layout.css",
  "/static/css/components.css",
  "/static/css/views.css",
  "/static/css/files.css",
  "/static/css/viewer.css",
  "/static/css/preview.css",
  "/static/css/admin.css",
  "/static/css/share.css",
  "/static/css/quick.css",
  "/static/css/legal.css",
  "/static/css/mobile.css",
  "/static/js/api.js",
  "/static/js/app.js",
  "/static/js/password.js",
  "/static/js/pwa.js",
  "/static/js/store.js",
  "/static/js/theme.js",
  "/static/js/petals.js",
  "/static/js/sha256.js",
  "/static/js/share.js",
  "/static/js/legal.js",
  "/static/js/uploader.js",
  "/static/js/views/auth.js",
  "/static/js/views/files.js",
  "/static/js/views/shares.js",
  "/static/js/views/trash.js",
  "/static/js/views/settings.js",
  "/static/js/views/admin.js",
  "/static/js/views/about.js",
  "/static/js/ui/avatar.js",
  "/static/js/ui/dialog.js",
  "/static/js/ui/live2d.js",
  "/static/js/ui/menu.js",
  "/static/js/ui/picker.js",
  "/static/js/ui/share.js",
  "/static/js/ui/tasks.js",
  "/static/js/ui/upload-queue.js",
  "/static/js/ui/toast.js",
  "/static/js/ui/markdown.js",
  "/static/js/ui/office.js",
  "/static/js/ui/odf.js",
  "/static/js/ui/docparse.js",
  "/static/js/ui/viewer.js",
  "/static/js/ui/artplayer-plugins.js",
];

const NAV_TIMEOUT = 4500;
const ASSET_TIMEOUT = 6000;

/* ------------------------------------------------------------------ */
/* 安装 / 激活                                                        */
/* ------------------------------------------------------------------ */
self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL);
      // 单个文件失败不能拖垮整个安装（比如某个可选资源还没上传）
      await Promise.all(
        PRECACHE.map(async (url) => {
          try {
            await cache.add(new Request(url, { cache: "reload" }));
          } catch (err) {
            console.warn("[sw] 预缓存跳过", url, err && err.message);
          }
        })
      );
      await self.skipWaiting();
    })()
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(
        names
          .filter((name) => name.startsWith(CACHE_PREFIX) && name !== SHELL && name !== RUNTIME)
          .map((name) => caches.delete(name))
      );
      await self.clients.claim();
    })()
  );
});

self.addEventListener("message", (event) => {
  if (event.data === "skip-waiting") self.skipWaiting();
});

/* ------------------------------------------------------------------ */
/* 工具                                                              */
/* ------------------------------------------------------------------ */
function timeout(ms) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  return { signal: controller.signal, done: () => clearTimeout(timer) };
}

async function fetchWithTimeout(request, ms) {
  const t = timeout(ms);
  try {
    return await fetch(request, { signal: t.signal });
  } finally {
    t.done();
  }
}

async function put(cacheName, request, response) {
  if (!response || !response.ok || response.status === 206) return;
  try {
    const cache = await caches.open(cacheName);
    await cache.put(request, response);
  } catch (err) {
    console.warn("[sw] 写入缓存失败", err && err.message);
  }
}

/** 网络优先：在线永远最新，离线回落缓存 */
async function networkFirst(request, cacheName, ms) {
  try {
    const response = await fetchWithTimeout(request, ms);
    if (response && response.status === 200) put(cacheName, request, response.clone());
    return response;
  } catch {
    const cached = await caches.match(request, { ignoreSearch: true });
    if (cached) return cached;
    throw new Error("offline");
  }
}

/** 缓存优先：适合几乎不变的大文件（播放器、图标） */
async function cacheFirst(request, cacheName) {
  const cached = await caches.match(request, { ignoreSearch: true });
  if (cached) return cached;
  const response = await fetch(request);
  if (response && response.status === 200) put(cacheName, request, response.clone());
  return response;
}

/* ------------------------------------------------------------------ */
/* 导航                                                              */
/* ------------------------------------------------------------------ */
async function handleNavigate(request) {
  const url = new URL(request.url);
  try {
    const response = await fetchWithTimeout(request, NAV_TIMEOUT);
    if (response && response.ok) {
      // 只缓存主站外壳；分享页内容依赖接口，缓存壳子意义不大
      if (url.pathname === "/" || url.pathname === "/index.html") {
        put(SHELL, "/", response.clone());
      }
      return response;
    }
    throw new Error(`status ${response && response.status}`);
  } catch {
    if (url.pathname.startsWith("/s/")) {
      return (await caches.match("/static/offline.html")) || offlineFallback();
    }
    return (
      (await caches.match("/static/index.html")) ||
      (await caches.match("/")) ||
      (await caches.match("/static/offline.html")) ||
      offlineFallback()
    );
  }
}

/** 连离线页都没缓存时，用内存里的极简兜底 */
function offlineFallback() {
  return new Response(
    `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
     <title>离线 · CloudNETP</title>
     <style>html,body{height:100%;margin:0;display:grid;place-items:center;background:#150f21;color:#e9e2f5;
     font:14px/1.7 system-ui,"Microsoft YaHei",sans-serif;text-align:center;padding:24px}
     b{display:block;font-size:17px;margin-bottom:6px}</style>
     <div><b>当前离线</b>网络恢复后刷新页面就能继续使用。</div>`,
    { status: 200, headers: { "Content-Type": "text/html; charset=utf-8" } }
  );
}

/* ------------------------------------------------------------------ */
/* 分发                                                              */
/* ------------------------------------------------------------------ */
self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  let url;
  try {
    url = new URL(request.url);
  } catch {
    return;
  }
  if (url.origin !== self.location.origin) return;

  const path = url.pathname;
  // 接口、WebDAV、上传下载：一律不参与缓存
  if (path.startsWith("/api/") || path === "/dav" || path.startsWith("/dav/")) return;
  // Live2D 模型随时可能被换掉，必须回源（缓存优先会让人以为「换模型没生效」）
  if (path.startsWith("/live2d/")) return;
  // Range 请求（音视频拖动、断点续传）必须回源，缓存会破坏 206 语义
  if (request.headers.has("range")) return;

  if (request.mode === "navigate") {
    event.respondWith(handleNavigate(request));
    return;
  }

  if (path.startsWith("/static/vendor/")) {
    event.respondWith(cacheFirst(request, RUNTIME));
    return;
  }

  const dynamicAsset =
    path.startsWith("/static/js/") ||
    path.startsWith("/static/css/") ||
    path === "/static/manifest.webmanifest" ||
    path.startsWith("/assets/");

  if (dynamicAsset) {
    event.respondWith(
      networkFirst(request, SHELL, ASSET_TIMEOUT).catch(
        () => caches.match("/static/offline.html").then((r) => r || offlineFallback())
      )
    );
    return;
  }

  // 其余同源 GET（如 /s/{token} 的图标之类）走缓存优先，失败就交给网络
  event.respondWith(cacheFirst(request, RUNTIME).catch(() => fetch(request)));
});
