/**
 * 我的表情：悬停放大预览
 *
 * 表情格子只有几十像素见方，动图和小细节根本看不清。鼠标停上去 300ms
 * 就在表情正上方浮一张大图 —— 纯预览，不带任何按钮：想发就点表情本身，
 * 想删用右上角的小叉。
 *
 * 只对 hover 能力为 hover 的设备生效：手机上一碰就是发送，弹预览反而挡事。
 */
import { escapeHtml } from "../api.js";
import { H, t } from "../i18n.js";

let node = null;
let anchorEl = null;
let showTimer = 0;
let hideTimer = 0;

const GAP = 8;

function hoverable() {
  return !window.matchMedia || window.matchMedia("(hover: hover)").matches;
}

/** 贴着表情正上方放；真的顶到屏幕边（top < 8）就贴顶，绝不落到表情下面 */
function place(anchor) {
  if (!node) return;
  const rect = anchor.getBoundingClientRect();
  const box = node.getBoundingClientRect();
  const left = Math.min(
    Math.max(8, rect.left + rect.width / 2 - box.width / 2),
    window.innerWidth - box.width - 8
  );
  const top = Math.max(8, rect.top - box.height - GAP);
  node.style.left = `${Math.round(left)}px`;
  node.style.top = `${Math.round(top)}px`;
}

/** 立刻收起（删完表情 / 面板关掉 / 换会话时用） */
export function hideStickerPreview() {
  window.clearTimeout(showTimer);
  window.clearTimeout(hideTimer);
  showTimer = 0;
  hideTimer = 0;
  anchorEl?.classList.remove("is-previewing");
  anchorEl = null;
  node?.remove();
  node = null;
}

/** 鼠标离开格子：给 180ms 缓冲，够移到浮层上继续看 */
export function scheduleHideStickerPreview() {
  window.clearTimeout(showTimer);
  showTimer = 0;
  window.clearTimeout(hideTimer);
  hideTimer = window.setTimeout(() => {
    hideTimer = 0;
    if (node?.matches(":hover")) return;
    hideStickerPreview();
  }, 180);
}

/**
 * 表情格子刚被重画过：旧锚点已经脱离文档，浮层留着就是幽灵。
 * 只在这种「确实悬空了」的情况下收，别把还在排队等 300ms 的悬停也一起打断。
 */
export function hideStickerPreviewIfStale() {
  if (anchorEl && !anchorEl.isConnected) hideStickerPreview();
}

/**
 * 悬停到某个表情上：在它正上方弹一张纯预览大图。
 * @param {{id:number,name:string,url:string}} sticker
 * @param {HTMLElement} anchor 表情格子
 * @param {{delay?:number}} opts
 */
export function hoverStickerPreview(sticker, anchor, { delay = 300 } = {}) {
  if (!hoverable() || !sticker || !anchor) return;
  // 还停在同一个格子里（含从图移到浮层）：别重置计时、也别重画
  if (anchorEl === anchor) {
    window.clearTimeout(hideTimer);
    hideTimer = 0;
    return;
  }
  hideStickerPreview();
  anchorEl = anchor;
  showTimer = window.setTimeout(() => {
    showTimer = 0;
    // 等这 300ms 里面板被重画了：锚点已经不在文档里，别在空气上弹一张图
    if (!anchor.isConnected) {
      anchorEl = null;
      return;
    }
    node = document.createElement("div");
    node.className = "sticker-preview";
    node.setAttribute("role", "tooltip");
    const name = sticker.name || t("表情");
    node.innerHTML = H`
      <img class="sticker-preview__img" src="${escapeHtml(sticker.url)}"
           alt="${escapeHtml(name)}" title="${escapeHtml(name)}">`;
    // 图片是异步加载的：加载完浮层会变高，必须重新定位，
    // 否则浮层朝下长会压住表情格子，点击就落在预览上了
    node.querySelector("img").addEventListener("load", () => place(anchor));
    node.addEventListener("mouseenter", () => {
      window.clearTimeout(hideTimer);
      hideTimer = 0;
    });
    node.addEventListener("mouseleave", scheduleHideStickerPreview);
    document.body.appendChild(node);
    anchor.classList.add("is-previewing");
    place(anchor);
  }, delay);
}
