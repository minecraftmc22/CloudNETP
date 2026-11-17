/**
 * 封禁提示层：账号被封后，在线用户会立刻看到这个遮罩。
 *
 * 单独实现而不用 Dialog：这里必须一直挡着（不许点背景 / ESC 关掉），
 * 按钮也是「退出登录」「去申诉」这两个固定的出口。
 */
import { H, t } from "../i18n.js";

const HOST_ID = "ban-host";
let mounted = false;

const pad = (n) => String(n).padStart(2, "0");

/** 绝对时间：封禁时间、解封时间要能一眼看懂，不用「4 天前」 */
export function absTime(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(
    d.getMinutes()
  )}`;
}

/** 剩余时长：给临时封禁显示「还剩 x 天 x 小时」 */
export function remainText(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const s = Math.max(0, Number(seconds) || 0);
  if (s <= 0) return t("即将解封");
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  if (days) return t("还剩 {d} 天 {h} 小时", { d: days, h: hours });
  if (hours) return t("还剩 {h} 小时 {m} 分", { h: hours, m: minutes });
  return t("还剩 {m} 分钟", { m: Math.max(1, minutes) });
}

/** 封禁信息三行：原因 / 封禁时间 / 解封时间 */
export function banInfoRows(ban) {
  const rows = [
    [t("封禁原因"), ban?.reason ? escapeText(ban.reason) : t("管理员未填写原因")],
    [t("封禁时间"), absTime(ban?.bannedAt)],
    [
      t("解封时间"),
      ban?.permanent ? t("永久封禁") : `${absTime(ban?.banUntil)}（${remainText(ban?.remainingSeconds)}）`,
    ],
  ];
  return rows
    .map(
      ([label, value]) => H`
      <div class="ban-info__row">
        <span class="ban-info__label">${label}</span>
        <span class="ban-info__value">${value}</span>
      </div>`
    )
    .join("");
}

function escapeText(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[c]);
}

function host() {
  let el = document.getElementById(HOST_ID);
  if (!el) {
    el = document.createElement("div");
    el.id = HOST_ID;
    el.className = "ban-host";
    document.body.appendChild(el);
  }
  return el;
}

/** 显示封禁遮罩；重复调用只更新内容，不会叠出两层 */
export function showBanNotice(detail, { onAppeal, onLogout } = {}) {
  const ban = detail?.ban || {};
  const el = host();
  el.innerHTML = H`
    <div class="ban-backdrop"></div>
    <div class="ban-card" role="alertdialog" aria-modal="true" aria-label="${t("你已被管理员封禁")}">
      <div class="ban-card__icon"><svg class="icon"><use href="#i-shield"/></svg></div>
      <h2 class="ban-card__title">${t("你已被管理员封禁")}</h2>
      <p class="ban-card__sub">${
        detail?.username ? `@${escapeText(detail.username)}` : t("当前账号暂时无法继续使用")
      }</p>
      <div class="ban-info">${banInfoRows(ban)}</div>
      <p class="ban-card__hint">${t("如果你认为这是误判，可以提交申诉，管理员会重新审核。")}</p>
      <div class="ban-card__actions">
        <button class="btn btn--subtle" type="button" data-ban="logout">${t("退出登录")}</button>
        <button class="btn btn--accent" type="button" data-ban="appeal">${t("去申诉")}</button>
      </div>
    </div>`;
  el.classList.add("is-open");
  mounted = true;
  el.querySelector('[data-ban="logout"]').addEventListener("click", () => onLogout?.());
  el.querySelector('[data-ban="appeal"]').addEventListener("click", () => onAppeal?.());
}

export function hideBanNotice() {
  if (!mounted) return;
  const el = document.getElementById(HOST_ID);
  if (el) {
    el.classList.remove("is-open");
    el.innerHTML = "";
  }
  mounted = false;
}

export function banNoticeOpen() {
  return mounted;
}
