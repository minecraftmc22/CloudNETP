/**
 * CDP娘（#/mascot，手机底部导航的第三项）
 *
 * 这一页相当于她的「名片 + 快捷入口」：
 * - 显示当前的 AI 接口状态（用默认接口 / 自己的模型 / 未配置）
 * - 一键打开对话（复用右下角那个浮层，历史记录、长期记忆都在）
 * - 直达设置里的「看板娘」页签，换模型、调大小、开关闭
 *
 * 看板娘本体还是右下角那个固定层，这里不复制一份 Live2D 实例 ——
 * 两份 canvas 会同时跑动画，手机发热还费电。
 */
import { api } from "../api.js";
import { H, t } from "../i18n.js";
import { openMascotChat } from "../ui/mascot-chat.js";
import { Toast } from "../ui/toast.js";

const TIPS = [
  {
    icon: "i-search",
    title: "找文件",
    desc: "「上个月那份报表放哪了？」——按名字、类型或时间帮你翻出来",
  },
  {
    icon: "i-chart",
    title: "看容量",
    desc: "剩余空间、哪个文件夹最占地方，问一句就有结论",
  },
  {
    icon: "i-palette",
    title: "换主题色",
    desc: "发一张图或者说个颜色，她能把界面主题一起换掉",
  },
  {
    icon: "i-bolt",
    title: "整理与分享",
    desc: "新建文件夹、移动重命名、生成取件码，都能让她代劳",
  },
];

export function renderMascotPage(root) {
  root.innerHTML = H`
    <div class="view-head">
      <div class="view-title"><svg class="icon"><use href="#i-spark"/></svg>CDP娘</div>
      <span class="view-sub">你的专属数据管家</span>
    </div>
    <div class="view-body">
      <div class="friend-body">
        <section class="mascot-hero">
          <div class="mascot-hero__avatar">
            <img src="/assets/brand-avatar.png" alt="CDP娘" width="108" height="108">
          </div>
          <div class="mascot-hero__name">CDP娘</div>
          <div class="mascot-hero__badge" data-status>${t("正在检查 AI 接口…")}</div>
          <p class="mascot-hero__desc">
            ${t("住在云盘右下角的小管家。可以和她聊文件、要统计、换主题色，也可以只是打个招呼。")}
          </p>
          <div class="mascot-hero__ops">
            <button class="btn btn--accent" type="button" data-act="chat">
              <svg class="icon icon--s"><use href="#i-chat"/></svg>${t("和她聊天")}
            </button>
            <button class="btn btn--subtle" type="button" data-act="settings">
              <svg class="icon icon--s"><use href="#i-settings"/></svg>${t("看板娘设置")}
            </button>
          </div>
        </section>

        <section class="mascot-tips">
          ${TIPS.map(
            (tip) => H`<div class="mascot-tip">
              <span class="mascot-tip__icon"><svg class="icon icon--s"><use href="#${tip.icon}"/></svg></span>
              <div>
                <div class="mascot-tip__title">${t(tip.title)}</div>
                <div class="mascot-tip__desc">${t(tip.desc)}</div>
              </div>
            </div>`
          ).join("")}
        </section>

        <p class="friend-hint">
          ${t("她的记性可以关：设置 → 看板娘里能关掉长期记忆、历史对话和工具权限。")}
        </p>
      </div>
    </div>`;

  const statusEl = root.querySelector("[data-status]");

  function paintStatus(cfg) {
    if (!statusEl) return;
    const aiOn = cfg?.aiEnabled !== false;
    const ready = Boolean(cfg?.ready);
    const model = cfg?.model || cfg?.defaultModel || "";
    if (!aiOn) {
      statusEl.textContent = t("管理员关闭了 AI 对话");
      statusEl.classList.add("is-off");
    } else if (ready) {
      statusEl.textContent = model ? t("已连接 · {model}", { model }) : t("已连接");
      statusEl.classList.remove("is-off");
    } else {
      statusEl.textContent = t("还没配置模型，去设置里填一下");
      statusEl.classList.add("is-off");
    }
  }

  api
    .get("/api/ai/config")
    .then((data) => paintStatus(data?.config || {}))
    .catch(() => paintStatus(null));

  root.querySelector('[data-act="chat"]').addEventListener("click", () => {
    openMascotChat();
    // 手机上对话是整屏抽屉，给个提示免得以为页面没反应
    if (window.innerWidth <= 768) Toast.info(t("CDP娘 已弹出，直接说话就行"));
  });

  root.querySelector('[data-act="settings"]').addEventListener("click", () => {
    location.hash = "#/settings/mascot";
  });
}
