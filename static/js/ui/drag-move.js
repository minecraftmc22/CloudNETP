/**
 * 拖动移动：把文件/文件夹拖到另一个文件夹上松手就移过去。
 *
 * 鼠标和触屏共用一套指针逻辑 —— HTML5 原生 DnD 在触屏上根本不触发，
 * 两边各写一套又容易走偏，所以统一用 pointer 事件自己跟手：
 *   鼠标：按住后移动超过 5px 开始拖（立刻响应，不迟钝）
 *   触屏：按住 280ms 开始拖；这期间手指移动超过 8px 就当成在滚列表，直接放弃
 *
 * 拖起来之后：原件淡出、跟手的是一份克隆、目标文件夹画一圈强调色，
 * 拖到列表上下边缘会自动滚（长列表里不用先滚再拖）。
 *
 * 这个模块只管「手势」，真正移动由 onDrop 回调决定（files.js 里走 /move 接口）。
 *
 * 用法：
 *   enableDragMove({
 *     root,        // 视图根节点（在它内部找源和目标）
 *     scroller,    // 列表滚动容器，拖到边缘时自动滚
 *     signal,      // AbortSignal，切页/登出自动解绑
 *     getIds,      // (sourceEl) => number[]  拖这一项要带走哪些 id
 *     isNoop,      // (targetId) => boolean  目标是当前目录 → 不画提示也不移动
 *     onDrop,      // ({ ids, targetId, targetName }) => void
 *   });
 *
 * DOM 约定：
 *   可拖的项：   [data-drag-id="<nodeId>"]
 *   可放的目标： [data-drop-folder="<folderId>"|""]（""=根目录），名字放 data-drop-name
 */
import { t } from "../i18n.js";

const MOUSE_SLOP = 5;
const TOUCH_HOLD_MS = 280;
const TOUCH_SLOP = 8;
const EDGE_ZONE = 56;
const EDGE_STEP = 14;
const CLICK_GUARD_MS = 500;

const readFolderId = (el) => (el.dataset.dropFolder === "" ? null : Number(el.dataset.dropFolder));

export function enableDragMove({ root, scroller = null, signal, getIds, isNoop, onDrop }) {
  let drag = null;
  /** 拖完松手时浏览器还会补一个 click，别让它顺手把文件夹打开、把选中项清掉 */
  let clickGuardUntil = 0;

  const sourceOf = (node) => {
    const el = node?.closest?.("[data-drag-id]");
    return el && root.contains(el) ? el : null;
  };

  /** 手指/光标下方的合法投放目标：文件夹磁贴、列表行、面包屑 */
  function targetOf(x, y) {
    const under = document.elementFromPoint(x, y);
    const el = under?.closest?.("[data-drop-folder]");
    if (!el || !root.contains(el) || !drag) return null;
    const id = readFolderId(el);
    // 拖着自己（或被选中一起拖的文件夹）往上放没有意义
    if (drag.ids.includes(id)) return null;
    if (isNoop?.(id)) return null;
    return el;
  }

  function setTarget(el) {
    if (!drag || drag.target === el) return;
    drag.target?.classList.remove("is-drop-target");
    drag.target = el || null;
    drag.target?.classList.add("is-drop-target");
    if (drag.hint) {
      const name = drag.target?.dataset.dropName || "";
      drag.hint.textContent = drag.target ? t("移到「{name}」", { name }) : "";
      drag.hint.hidden = !drag.target;
    }
  }

  function autoScroll(y) {
    if (!scroller) return;
    const rect = scroller.getBoundingClientRect();
    if (y < rect.top + EDGE_ZONE) scroller.scrollTop -= EDGE_STEP;
    else if (y > rect.bottom - EDGE_ZONE) scroller.scrollTop += EDGE_STEP;
  }

  function paint(x, y) {
    if (!drag?.ghost) return;
    // 触屏时把幽灵抬高一点，不然整只手指正好盖住它
    const lift = drag.lift;
    drag.ghost.style.transform = `translate3d(${x - drag.offX}px, ${y - drag.offY - lift}px, 0)`;
    if (drag.hint) {
      const width = drag.ghost.offsetWidth;
      drag.hint.style.transform = `translate3d(${x - drag.offX + width / 2}px, ${
        y - drag.offY - lift - 34
      }px, 0) translateX(-50%)`;
    }
    setTarget(targetOf(x, y));
    autoScroll(y);
  }

  function activate(x, y) {
    if (!drag || drag.active) return;
    window.clearTimeout(drag.timer);
    drag.active = true;

    const rect = drag.source.getBoundingClientRect();
    // 抓住的位置就在克隆上的同一处，拖起来不会突然「跳一下」
    drag.offX = Math.min(Math.max(drag.startX - rect.left, 6), Math.max(rect.width - 6, 6));
    drag.offY = Math.min(Math.max(drag.startY - rect.top, 6), Math.max(rect.height - 6, 6));
    drag.lift = drag.type === "touch" ? 30 : 0;

    drag.source.classList.add("is-dragging");

    const ghost = drag.source.cloneNode(true);
    ghost.classList.add("drag-ghost");
    ghost.classList.remove("is-dragging", "is-selected", "is-cursor");
    ghost.removeAttribute("data-drag-id");
    ghost.style.width = `${rect.width}px`;
    ghost.style.height = `${rect.height}px`;
    if (drag.ids.length > 1) {
      const badge = document.createElement("span");
      badge.className = "drag-ghost__count";
      badge.textContent = String(drag.ids.length);
      ghost.appendChild(badge);
    }
    document.body.appendChild(ghost);
    drag.ghost = ghost;

    const hint = document.createElement("div");
    hint.className = "drag-hint";
    hint.hidden = true;
    document.body.appendChild(hint);
    drag.hint = hint;

    document.body.classList.add("is-drag-moving");
    if (drag.type === "touch") lockScroll();
    paint(x, y);
  }

  /**
   * 触屏拖起来后别让列表跟着滚。
   * 只挂一个非 passive 的 touchmove 不够稳（手势一开始就被当成滚动时，后续
   * preventDefault 会被浏览器忽略），所以同时把滚动容器冻住。
   */
  let scrollLock = null;
  function lockScroll() {
    if (!scroller || scrollLock) return;
    scrollLock = { overflowY: scroller.style.overflowY };
    scroller.style.overflowY = "hidden";
    window.addEventListener("touchmove", onTouchMove, { passive: false });
  }
  function unlockScroll() {
    if (!scrollLock) return;
    window.removeEventListener("touchmove", onTouchMove);
    if (scroller) scroller.style.overflowY = scrollLock.overflowY;
    scrollLock = null;
  }
  function onTouchMove(event) {
    if (drag?.active && event.cancelable) event.preventDefault();
  }

  function cleanup() {
    if (!drag) return;
    window.clearTimeout(drag.timer);
    drag.target?.classList.remove("is-drop-target");
    drag.source.classList.remove("is-dragging");
    drag.ghost?.remove();
    drag.hint?.remove();
    document.body.classList.remove("is-drag-moving");
    if (drag.captured) {
      try {
        drag.source.releasePointerCapture(drag.pointerId);
      } catch {
        /* 指针已经结束，忽略 */
      }
    }
    unlockScroll();
    drag = null;
  }

  root.addEventListener(
    "pointerdown",
    (event) => {
      if (drag) return;
      if (event.pointerType === "mouse" && event.button !== 0) return;
      // 「更多」按钮、勾选框、链接上的按下不算拖
      if (event.target.closest?.("button, a, input, label, textarea, select")) return;
      const source = sourceOf(event.target);
      if (!source) return;
      const ids = getIds?.(source);
      if (!ids?.length) return;

      drag = {
        source,
        ids,
        pointerId: event.pointerId,
        type: event.pointerType,
        startX: event.clientX,
        startY: event.clientY,
        active: false,
        target: null,
        ghost: null,
        hint: null,
        timer: 0,
        captured: false,
        offX: 0,
        offY: 0,
        lift: 0,
      };
      try {
        source.setPointerCapture(event.pointerId);
        drag.captured = true;
      } catch {
        /* 个别浏览器不支持，靠 window 上的监听兜底 */
      }
      if (event.pointerType !== "mouse") {
        drag.scrollTop = scroller ? scroller.scrollTop : 0;
        drag.timer = window.setTimeout(() => {
          if (!drag || drag.active) return;
          // 长按这几百毫秒里列表已经滚起来了 → 用户在滑列表，不是在拖
          // （pointermove 会被浏览器按帧合并，光靠 8px 阈值偶尔会漏判）
          if (scroller && Math.abs(scroller.scrollTop - drag.scrollTop) > 2) {
            cleanup();
            return;
          }
          navigator.vibrate?.(12);
          activate(drag.startX, drag.startY);
        }, TOUCH_HOLD_MS);
      }
    },
    { signal }
  );

  const onMove = (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;

    if (!drag.active) {
      const dist = Math.hypot(dx, dy);
      if (drag.type === "mouse") {
        if (dist < MOUSE_SLOP) return;
        activate(event.clientX, event.clientY);
      } else {
        // 长按判定期间手指动了 → 用户其实在滚列表，放手
        if (dist > TOUCH_SLOP) cleanup();
        return;
      }
    }
    if (event.cancelable) event.preventDefault();
    paint(event.clientX, event.clientY);
  };

  const onUp = (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const active = drag.active;
    const target = drag.target;
    const ids = drag.ids.slice();
    const targetId = target ? readFolderId(target) : null;
    const targetName = target?.dataset.dropName || "";
    cleanup();
    // 只吞掉「拖过」之后那一下 click；普通点击照常进选择的逻辑
    if (active) clickGuardUntil = performance.now() + CLICK_GUARD_MS;
    if (active && target) onDrop?.({ ids, targetId, targetName });
  };

  const onCancel = (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    cleanup();
  };

  window.addEventListener("pointermove", onMove, { signal, passive: false });
  window.addEventListener("pointerup", onUp, { signal });
  window.addEventListener("pointercancel", onCancel, { signal });
  window.addEventListener("blur", () => cleanup(), { signal });
  // 拖到一半切页 / 登出：监听会被 abort 撤掉，克隆和 body 上的类得自己收
  signal?.addEventListener("abort", () => cleanup(), { once: true });

  // 拖起来时手机长按会弹系统菜单（Android 尤其明显），这里按住不放
  root.addEventListener(
    "contextmenu",
    (event) => {
      if (!drag?.active) return;
      event.preventDefault();
      event.stopPropagation();
    },
    { signal, capture: true }
  );

  root.addEventListener(
    "click",
    (event) => {
      if (performance.now() >= clickGuardUntil) return;
      event.stopPropagation();
      event.preventDefault();
    },
    { signal, capture: true }
  );
}
