/**
 * 极简状态容器：发布-订阅，不引框架
 */
export function createStore(initial) {
  let state = { ...initial };
  const listeners = new Set();

  return {
    get() {
      return state;
    },
    set(patch) {
      const next = typeof patch === "function" ? patch(state) : patch;
      state = { ...state, ...next };
      listeners.forEach((fn) => {
        try {
          fn(state);
        } catch (err) {
          console.error("[store] listener error", err);
        }
      });
      return state;
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}

export const store = createStore({
  user: null,
  meta: null,
  route: "files",
  navCollapsed: false,
});

/** 对外站点根地址：优先用 .env 的 SITE_URL，未配置时退回当前浏览器地址。 */
export function siteOrigin() {
  const configured = store.get().meta?.siteUrl;
  const base = configured && /^https?:\/\//i.test(configured) ? configured : location.origin;
  return base.replace(/\/+$/, "");
}
