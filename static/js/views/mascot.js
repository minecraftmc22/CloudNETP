/**
 * CDP娘（#/mascot，底部导航的第三项）
 *
 * 这一页就是「她站在你面前」：Live2D 整只搬进页面中间，下面直接是对话框。
 * 聊天复用右下角那个面板（历史记录、长期记忆、工具胶囊、token 圆环都在），
 * 只是换成整页内联布局，所以不需要第二套聊天逻辑。
 *
 * 注意：Live2D 不复制实例 —— 把 #mascot 本体搬进页面，离开时再搬回 body。
 * 两份 canvas 同时跑动画在手机上又热又费电。
 */
import { api } from "../api.js";
import { H, t } from "../i18n.js";
import { getMascotController } from "../ui/live2d.js";
import { mountMascotChatInline, startNewMascotChat, unmountMascotChatInline } from "../ui/mascot-chat.js";

/** 记住这一页有没有挂上去过，route() 里据此收尾 */
let mounted = false;
/** 看板娘是否已经搬进页面（深链进来时她可能还没初始化） */
let embedded = false;

export function renderMascotPage(root) {
  root.innerHTML = H`
    <div class="cdp-page">
      <div class="view-head cdp-head">
        <div class="view-title"><svg class="icon"><use href="#i-spark"/></svg>CDP娘</div>
        <span class="cdp-head__status" data-status>${t("正在检查 AI 接口…")}</span>
        <button class="icon-btn cdp-head__gear" type="button" data-act="settings"
                title="${t("看板娘设置")}" aria-label="${t("看板娘设置")}">
          <svg class="icon icon--s"><use href="#i-settings"/></svg>
        </button>
      </div>
      <div class="cdp-hero" data-cdp-hero>
        <div class="cdp-hero__stage" data-cdp-stage></div>
        <button class="cdp-new" type="button" data-act="new">
          <svg class="icon icon--s"><use href="#i-plus"/></svg><span>${t("开启新对话")}</span>
        </button>
      </div>
    </div>`;

  const stage = root.querySelector("[data-cdp-stage]");
  const hero = root.querySelector("[data-cdp-hero]");

  // 深链直接进 #/mascot 时，route() 可能早于看板娘初始化：等她准备好再搬
  function embed() {
    if (embedded || !root.isConnected) return;
    const controller = getMascotController();
    if (!controller) return;
    controller.embed(stage);
    embedded = true;
  }
  embed();
  if (!embedded) window.addEventListener("mascot:ready", embed, { once: true });

  mountMascotChatInline(hero);
  mounted = true;
  trackCompose(hero);

  root.querySelector('[data-act="new"]').addEventListener("click", () => startNewMascotChat());
  root.querySelector('[data-act="settings"]').addEventListener("click", () => {
    location.hash = "#/settings/mascot";
  });

  paintStatus(root.querySelector("[data-status]"));
}

/** 离开这一页（route() 清空视图之前调用）：面板搬回 body，她也搬回右下角 */
export function unmountMascotPage() {
  if (!mounted && !embedded) return;
  mounted = false;
  embedded = false;
  footObs?.disconnect();
  footObs = null;
  unmountMascotChatInline();
  getMascotController()?.unembed();
}

/** 输入框会长高（多行 / 贴图预览），舞台要跟着让位，否则她的脚会伸到卡片下面 */
let footObs = null;

function trackCompose(hero) {
  const compose = hero.querySelector(".mchat__compose");
  if (!compose) return;
  const apply = () => {
    // 卡片高度 + 底部 10px 外边距 + 一点呼吸空间
    const height = Math.round(compose.getBoundingClientRect().height) + 18;
    hero.style.setProperty("--cdp-foot", `${height}px`);
  };
  apply();
  if (!("ResizeObserver" in window)) return;
  footObs?.disconnect();
  footObs = new ResizeObserver(apply);
  footObs.observe(compose);
}

function paintStatus(statusEl) {
  if (!statusEl) return;
  api
    .get("/api/ai/config")
    .then((data) => {
      const cfg = data?.config || {};
      const aiOn = cfg.aiEnabled !== false;
      const model = cfg.model || cfg.defaultModel || "";
      if (!aiOn) {
        statusEl.textContent = t("管理员关闭了 AI 对话");
        statusEl.classList.add("is-off");
      } else if (cfg.ready) {
        statusEl.textContent = model ? t("已连接 · {model}", { model }) : t("已连接");
        statusEl.classList.remove("is-off");
      } else {
        statusEl.textContent = t("还没配置模型，去设置里填一下");
        statusEl.classList.add("is-off");
      }
    })
    .catch(() => {
      statusEl.textContent = t("AI 接口状态未知");
      statusEl.classList.add("is-off");
    });
}
