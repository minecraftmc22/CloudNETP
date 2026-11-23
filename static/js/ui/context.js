/**
 * 全局右键收敛。
 *
 * 浏览器自带的右键菜单在应用里没有意义（「查看源代码」「检查」之类），
 * 但项目里只有少数几个地方真的做了右键菜单：我的文件、回收站、聊天会话与表情。
 *
 * 规则：
 * - 元素自己处理过（按需 `preventDefault()`）→ 不动它
 * - 在 `[data-ctx]` 容器内 → 交给页面自己（本项目里基本都用前者）
 * - 输入框 / 文本域 / 可编辑区域 → 保留原生菜单（右键粘贴是刚需）
 * - 其余一律吃掉，什么都不弹
 *
 * 监听挂在冒泡阶段而不是捕获阶段：页面自己的处理器绑在元素上，先跑，
 * 它调过 `preventDefault()` 之后 `event.defaultPrevented` 才是 true。
 */
import { closeMenu } from "./menu.js";

const NATIVE_TARGETS = "input, textarea, [contenteditable='true'], [contenteditable='']";

export function initContextGuard() {
  if (window.__ctxGuard) return;
  window.__ctxGuard = true;
  document.addEventListener(
    "contextmenu",
    (event) => {
      if (event.defaultPrevented) return;
      const target = event.target;
      if (!(target instanceof Element)) {
        event.preventDefault();
        return;
      }
      if (target.closest(NATIVE_TARGETS) || target.closest("[data-ctx]")) return;
      event.preventDefault();
      // 顺手把可能还没关的浮层关掉：右键往往是「想换个操作」的开始
      closeMenu();
    },
    false
  );
}
