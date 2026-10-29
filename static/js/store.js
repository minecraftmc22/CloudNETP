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
