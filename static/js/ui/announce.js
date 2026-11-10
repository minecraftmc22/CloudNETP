/**
 * 站内公告
 *
 * 后台「系统设置 → 站点 → 公告」填的内容会显示在两个地方：
 *  - 登录页：卡片里、标题下面（`card: true`）
 *  - 主界面：标题栏下面一整条，切到哪个视图都在
 *
 * 关掉是按「公告内容」记在本机的：管理员改了文字，等于换了一条公告，
 * 会重新出现一次（否则改完也没人看得到）。
 */
import { escapeHtml } from "../api.js";
import { H, t } from "../i18n.js";

const DISMISS_KEY = "cloudnetp.announce-dismissed";

/** 内容指纹：用来判断"这条"是否被关过 */
function fingerprint(text) {
  let hash = 0;
  for (let i = 0; i < text.length; i += 1) {
    hash = (hash * 31 + text.charCodeAt(i)) | 0;
  }
  return hash.toString(36);
}

function readDismissed() {
  try {
    return localStorage.getItem(DISMISS_KEY) || "";
  } catch {
    return "";
  }
}

function remember(text) {
  try {
    localStorage.setItem(DISMISS_KEY, fingerprint(text));
  } catch {
    /* 隐私模式忽略 */
  }
}

/**
 * 把公告渲染进 host 元素。
 * @returns {boolean} 是否有内容被显示
 */
export function renderAnnounce(host, text, { card = false } = {}) {
  if (!host) return false;
  const body = String(text || "").trim();

  if (!body || readDismissed() === fingerprint(body)) {
    host.hidden = true;
    host.innerHTML = "";
    return false;
  }

  host.className = card ? "announce announce--card" : "announce";
  host.innerHTML = H`
    <span class="announce__icon"><svg class="icon icon--s"><use href="#i-info"/></svg></span>
    <span class="announce__text" data-announce-text>${escapeHtml(body)}</span>
    <button class="announce__more" type="button" data-announce-more hidden>${t("展开")}</button>
    <button class="announce__close" type="button" data-announce-close
            title="${t("关闭这条公告")}" aria-label="${t("关闭这条公告")}">
      <svg class="icon icon--s"><use href="#i-close"/></svg>
    </button>`;
  host.hidden = false;

  const textEl = host.querySelector("[data-announce-text]");
  const more = host.querySelector("[data-announce-more]");
  const close = host.querySelector("[data-announce-close]");

  // 超过三行才给「展开」，短公告不该多一个没用的按钮
  const measure = () => {
    if (!textEl || !more) return;
    const clipped = textEl.scrollHeight > textEl.clientHeight + 2;
    more.hidden = !host.classList.contains("is-open") && !clipped;
  };
  window.requestAnimationFrame(measure);

  more?.addEventListener("click", () => {
    const open = host.classList.toggle("is-open");
    more.textContent = open ? t("收起") : t("展开");
    more.hidden = false;
    if (!open) window.requestAnimationFrame(measure);
  });

  close?.addEventListener("click", () => {
    remember(body);
    host.hidden = true;
    host.innerHTML = "";
  });

  return true;
}
