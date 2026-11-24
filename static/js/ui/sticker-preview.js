/**
 * 我的表情：悬停放大预览
 *
 * 表情格子只有几十像素见方，动图和小细节根本看不清。鼠标停上去 300ms 就在旁边弹一张大图，
 * 顺手把「发送 / 删除」也放进去 —— 删除不必再去够右上角那个小叉，也不用记右键。
 *
 * 只对 hover 能力为 hover 的设备生效：手机上一碰就是发送，弹预览反而挡事。
 */
import { escapeHtml } from "../api.js";
import { H, t } from "../i18n.js";

let node = null;
let anchorEl = null;
let currentId = 0;
let showTimer = 0;
let hideTimer = 0;

const GAP = 10;

function hoverable() {
  return !window.matchMedia || window.matchMedia("(hover: hover)").matches;
}

function place(anchor) {
  if (!node) return;
  const rect = anchor.getBoundingClientRect();
  const box = node.getBoundingClientRect();
  const left = Math.min(
    Math.max(8, rect.left + rect.width / 2 - box.width / 2),
    window.innerWidth - box.width - 8
  );
  let top = rect.top - box.height - GAP;
  if (top < 8) top = Math.min(rect.bottom + GAP, window.innerHeight - box.height - 8);
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
  currentId = 0;
  node?.remove();
  node = null;
}

/** 鼠标离开格子：给 180ms 缓冲，够移到浮层上继续看 */
export function scheduleHideStickerPreview() {  window.clearTimeout(showTimer);
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
 * 悬停到某个表情上
 * @param {{id:number,name:string,url:string}} sticker
 * @param {HTMLElement} anchor 表情格子
 * @param {{onSend:Function,onDelete:Function,delay?:number}} opts
 */
export function hoverStickerPreview(sticker, anchor, { onSend, onDelete, delay = 300 } = {}) {
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
    currentId = sticker.id;
    node = document.createElement("div");
    node.className = "sticker-preview";
    node.setAttribute("role", "tooltip");
    node.innerHTML = H`
      <div class="sticker-preview__pic">
        <img src="${escapeHtml(sticker.url)}" alt="${escapeHtml(sticker.name || t("表情"))}">
      </div>
      <div class="sticker-preview__bar">
        <span class="sticker-preview__name" title="${escapeHtml(sticker.name || "")}">${escapeHtml(
          sticker.name || t("表情")
        )}</span>
        <span class="sticker-preview__ops">
          <button class="sticker-preview__btn is-danger" type="button" data-sp="del">
            <svg class="icon icon--s"><use href="#i-trash"/></svg>${t("删除")}
          </button>
          <button class="sticker-preview__btn" type="button" data-sp="send">
            <svg class="icon icon--s"><use href="#i-send"/></svg>${t("发送")}
          </button>
        </span>
      </div>`;
    node.addEventListener("mouseenter", () => {
      window.clearTimeout(hideTimer);
      hideTimer = 0;
    });
    node.addEventListener("mouseleave", scheduleHideStickerPreview);
    node.addEventListener("click", (event) => {
      const btn = event.target.closest("[data-sp]");
      if (!btn) return;
      event.preventDefault();
      event.stopPropagation();
      const id = currentId;
      const name = sticker.name || "";
      hideStickerPreview();
      if (btn.dataset.sp === "send") onSend?.(id);
      else onDelete?.(id, name);
    });
    document.body.appendChild(node);
    anchor.classList.add("is-previewing");
    place(anchor);
  }, delay);
}
