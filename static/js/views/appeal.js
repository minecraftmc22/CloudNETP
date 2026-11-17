/**
 * 封禁申诉页：被ban的人在这里看封禁详情、提交申诉、看审核结果。
 *
 * 不依赖登录态：身份来自登录被拒时下发的票据（query / sessionStorage）
 * 或仍然存在的封禁会话 cookie。两者都没有时提示回去重新登录。
 */
import { api, escapeHtml, ApiError } from "../api.js";
import { H, t } from "../i18n.js";
import { Toast } from "../ui/toast.js";
import { absTime, banInfoRows } from "../ui/ban.js";

const TICKET_KEY = "cn:ban-ticket";
const MAX = 1000;
// 申诉页的轮询定时器：重复进入 / 离开页面都要清掉，别越积越多
let pollTimer = null;

export function storedBanTicket() {
  try {
    return sessionStorage.getItem(TICKET_KEY) || "";
  } catch {
    return "";
  }
}

export function rememberBanTicket(ticket) {
  try {
    if (ticket) sessionStorage.setItem(TICKET_KEY, ticket);
  } catch {
    /* 隐私模式下写不了，不影响本次使用 */
  }
}

export function forgetBanTicket() {
  try {
    sessionStorage.removeItem(TICKET_KEY);
  } catch {
    /* 忽略 */
  }
}

const statusText = (status) =>
  status === "approved" ? t("已通过") : status === "rejected" ? t("未通过") : t("审核中");

export function renderAppeal(root, { detail = null, ticket = "", onExit } = {}) {
  let ban = detail?.ban || null;
  let appeal = null;
  let username = detail?.username || "";
  // 票据可能来自参数、登录被拒时的会话存储、或后端下发的 cookie
  let currentTicket = ticket || storedBanTicket();

  root.hidden = false;
  root.innerHTML = H`
    <div class="appeal-stage">
      <div class="appeal-card" data-card>
        <div class="appeal-card__head">
          <span class="appeal-card__logo"><img src="/assets/brand-avatar.png" alt="" width="42" height="42"></span>
          <div>
            <div class="appeal-card__title">${t("你已被管理员封禁")}</div>
            <div class="appeal-card__sub" data-who>${username ? `@${escapeHtml(username)}` : ""}</div>
          </div>
        </div>
        <div data-body>${H`<div class="appeal-loading"><span class="spinner"></span></div>`}</div>
        <div class="appeal-card__foot">
          <button class="btn btn--subtle btn--sm" type="button" data-act="logout">${t("退出登录")}</button>
          <button class="btn btn--subtle btn--sm" type="button" data-act="login">${t("返回登录")}</button>
        </div>
      </div>
    </div>`;

  const body = root.querySelector("[data-body]");
  const who = root.querySelector("[data-who]");
  root.querySelector('[data-act="logout"]').addEventListener("click", () => banLogout(onExit));
  root.querySelector('[data-act="login"]').addEventListener("click", () => onExit?.());

  function paint() {
    if (!ban || ban.active === false) {
      const idle = !appeal;
      body.innerHTML = H`
        <div class="appeal-state is-ok">
          <svg class="icon"><use href="#i-check"/></svg>
          <div>
            <div class="appeal-state__title">${
              idle ? t("当前账号没有被封禁") : t("账号已恢复，可以正常登录了")
            }</div>
            <div class="appeal-state__desc">${
              idle
                ? t("如果你是误点进来的，直接返回登录即可。")
                : appeal?.status === "approved"
                  ? t("管理员已通过了你的申诉。")
                  : t("封禁已经解除。")
            }</div>
          </div>
        </div>
        <button class="btn btn--accent appeal-submit" type="button" data-act="back">${t("返回登录")}</button>`;
      // 直接重载：还有效的会话会回到网盘，没有会话的自然落到登录页
      body.querySelector('[data-act="back"]').addEventListener("click", () => window.location.assign("/"));
      return;
    }

    const pending = appeal?.status === "pending";
    const rejected = appeal?.status === "rejected";
    body.innerHTML = H`
      <div class="ban-info">${banInfoRows(ban)}</div>
      ${
        appeal
          ? H`
        <div class="appeal-history is-${escapeHtml(appeal.status)}">
          <div class="appeal-history__top">
            <span class="appeal-history__tag">${statusText(appeal.status)}</span>
            <span class="appeal-history__time">${escapeHtml(absTime(appeal.createdAt))}</span>
          </div>
          <p class="appeal-history__msg">${escapeHtml(appeal.message)}</p>
          ${
            appeal.reply
              ? H`<p class="appeal-history__reply">${t("管理员回复：{text}", {
                  text: escapeHtml(appeal.reply),
                })}</p>`
              : ""
          }
        </div>`
          : ""
      }
      ${
        pending
          ? H`<p class="appeal-note">${t("申诉已提交，管理员审核后会在这里更新结果，也可以稍后刷新查看。")}</p>`
          : H`
        <label class="appeal-field">
          <span class="field__label">${rejected ? t("再次申诉") : t("申诉说明")}</span>
          <textarea data-msg maxlength="${MAX}" rows="5"
            placeholder="${t("说说具体情况，比如你并不知道哪里违规、文件来源是否合法等")}"></textarea>
          <span class="appeal-counter" data-count>0 / ${MAX}</span>
        </label>
        <p class="appeal-note">${t("提交后由管理员在后台审核；审核通过会立即解封，驳回时会附上说明。")}</p>
        <button class="btn btn--accent appeal-submit" type="button" data-act="submit">${t("提交申诉")}</button>
      `}`;

    if (!pending) {
      const msg = body.querySelector("[data-msg]");
      const count = body.querySelector("[data-count]");
      msg.addEventListener("input", () => {
        count.textContent = `${msg.value.length} / ${MAX}`;
      });
      body.querySelector('[data-act="submit"]').addEventListener("click", () => submit(msg));
    }
  }

  async function submit(msgEl) {
    const message = msgEl.value.trim();
    if (message.length < 4) {
      Toast.warn(t("再多写几个字吧，方便管理员判断"));
      msgEl.focus();
      return;
    }
    const btn = body.querySelector('[data-act="submit"]');
    btn.disabled = true;
    btn.textContent = t("正在提交…");
    try {
      const res = await api.post("/api/ban/appeal", { message, ticket: currentTicket });
      if (res?.unbanned) {
        ban = { ...(ban || {}), active: false };
        Toast.success(t("封禁刚好到期，已经自动解封了"));
      } else {
        appeal = res?.appeal || null;
        Toast.success(t("申诉已提交，请等待管理员审核"));
      }
      paint();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // 别处已经提交过：按待审核展示，别让用户重复点
        await load(true);
        return;
      }
      Toast.error(err.message);
      btn.disabled = false;
      btn.textContent = t("提交申诉");
    }
  }

  async function load(silent = false) {
    try {
      const query = currentTicket ? `?ticket=${encodeURIComponent(currentTicket)}` : "";
      const data = await api.get(`/api/ban/info${query}`);
      ban = data.ban || ban;
      appeal = data.appeal || null;
      username = data.username || username;
      if (username) who.textContent = `@${username}`;
      paint();
    } catch (err) {
      if (!silent) {
        const expired = err instanceof ApiError && err.status === 401;
        body.innerHTML = H`
          <div class="appeal-state is-warn">
            <svg class="icon"><use href="#i-alert"/></svg>
            <div>
              <div class="appeal-state__title">${
                expired ? t("申诉链接已失效") : t("封禁信息读取失败")
              }</div>
              <div class="appeal-state__desc">${
                expired ? t("请回到登录页重新登录一次，会重新进入申诉页。") : escapeHtml(err.message)
              }</div>
            </div>
          </div>`;
      }
    }
  }

  if (ban) paint();
  load();
  // 审核结果可能在用户停留期间更新：半分钟拉一次，别让页面一直停在「审核中」
  if (pollTimer) window.clearInterval(pollTimer);
  pollTimer = window.setInterval(() => {
    if (!root.querySelector(".appeal-card")) {
      window.clearInterval(pollTimer);
      pollTimer = null;
      return;
    }
    if (appeal?.status === "pending" || !ban) load(true);
  }, 30000);
}

async function banLogout(onExit) {
  try {
    await api.post("/api/ban/logout");
  } catch {
    /* 清不掉也照常回登录页 */
  }
  forgetBanTicket();
  onExit?.();
}

