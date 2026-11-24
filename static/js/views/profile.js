/**
 * 个人主页（#/profile/<用户 id>）
 *
 * - 自己：可以编辑资料、开/关分享链接
 * - 别人：按对方设定的可见性显示；能直接加好友 / 发消息
 * - 分享页（/u/<token>）走 public-profile.js，不依赖登录
 */
import { api, escapeHtml } from "../api.js";
import { H, t } from "../i18n.js";
import { avatarMarkup } from "../ui/avatar.js";
import { copyText } from "../ui/clipboard.js";
import { Dialog } from "../ui/dialog.js";
import { Toast } from "../ui/toast.js";
import { LINK_ICONS, LINK_LABELS } from "../ui/usercard.js";

const GENDERS = [
  { value: "", label: "不填" },
  { value: "male", label: "男" },
  { value: "female", label: "女" },
  { value: "other", label: "其他" },
];

const LINK_ORDER = Object.keys(LINK_LABELS);

/* 主页横幅固定 4:1（与服务端 COVER_RATIO 一致），先在本地裁好再上传：
   既省流量，也让用户看到的预览和存下来的就是同一块画面。 */
const COVER_RATIO = 4;
const COVER_MAX_WIDTH = 1600;

function loadBitmap(file) {
  if (typeof createImageBitmap === "function") return createImageBitmap(file);
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve(img);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("decode"));
    };
    img.src = url;
  });
}

async function prepareCover(file) {
  const bmp = await loadBitmap(file);
  const w = bmp.width || bmp.naturalWidth;
  const h = bmp.height || bmp.naturalHeight;
  if (!w || !h) throw new Error("decode");
  let cw = w;
  let ch = Math.round(w / COVER_RATIO);
  if (ch > h) {
    ch = h;
    cw = Math.round(h * COVER_RATIO);
  }
  const outW = Math.min(COVER_MAX_WIDTH, cw);
  const outH = Math.max(1, Math.round(outW / COVER_RATIO));
  const canvas = document.createElement("canvas");
  canvas.width = outW;
  canvas.height = outH;
  canvas.getContext("2d").drawImage(
    bmp,
    Math.round((w - cw) / 2),
    Math.round((h - ch) / 2),
    cw,
    ch,
    0,
    0,
    outW,
    outH
  );
  bmp.close?.();
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/webp", 0.9));
  return blob || file;
}

export function genderText(value) {
  if (!value) return "";
  return t(GENDERS.find((g) => g.value === value)?.label || "其他");
}

export function linkItem(link) {
  const label = t(LINK_LABELS[link.key] || link.key);
  const icon = LINK_ICONS[link.key] || "i-link";
  const body = link.url
    ? H`<a class="profile-link__value" href="${escapeHtml(link.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(link.value)}</a>`
    : H`<span class="profile-link__value">${escapeHtml(link.value)}</span>`;
  return H`
    <div class="profile-link">
      <svg class="icon icon--s"><use href="#${icon}"/></svg>
      <span class="profile-link__label">${label}</span>
      ${body}
    </div>`;
}

export function renderProfile(root, { user: me, sub = "" } = {}) {
  const targetId = Number(sub) || me?.id || 0;
  const state = { profile: null, loading: true, busy: false };

  root.innerHTML = H`
    <div class="view-head">
      <div class="view-title"><svg class="icon"><use href="#i-user"/></svg>个人主页</div>
      <span class="view-sub" data-sub></span>
      <div class="grow"></div>
      <button class="icon-btn" type="button" data-act="refresh" title="${t("刷新")}">
        <svg class="icon"><use href="#i-refresh"/></svg>
      </button>
    </div>
    <div class="view-body"><div class="profile-body" data-body></div></div>`;

  const bodyEl = root.querySelector("[data-body]");
  const subEl = root.querySelector("[data-sub]");

  function paint() {
    if (state.loading) {
      bodyEl.innerHTML = H`<div class="empty"><div class="empty__title">${t("正在加载…")}</div></div>`;
      return;
    }
    const p = state.profile;
    if (!p) {
      bodyEl.innerHTML = H`<div class="empty">
          <div class="empty__art"><svg class="icon"><use href="#i-user"/></svg></div>
          <div class="empty__title">${t("这个主页不存在或已关闭")}</div>
        </div>`;
      return;
    }
    const name = p.alias || p.displayName;
    subEl.textContent = p.isSelf ? t("这是你的主页") : `@${p.username || ""}`;

    const ops = [];
    if (p.isSelf) {
      ops.push(H`<button class="btn btn--accent" type="button" data-act="edit">${t("编辑资料")}</button>`);
      ops.push(H`<button class="btn btn--subtle" type="button" data-act="share">${t("分享主页")}</button>`);
    } else if (p.relation === "friends") {
      ops.push(H`<button class="btn btn--accent" type="button" data-act="chat">${t("发消息")}</button>`);
    } else if (p.relation === "pending_out") {
      ops.push(H`<button class="btn btn--subtle" type="button" disabled>${t("已发送申请")}</button>`);
    } else if (p.relation === "pending_in") {
      ops.push(H`<button class="btn btn--accent" type="button" data-act="incoming">${t("去处理申请")}</button>`);
    } else if (p.relation === "blocked") {
      ops.push(H`<span class="chip chip--danger">${t("已拉黑")}</span>`);
    } else {
      ops.push(H`<button class="btn btn--accent" type="button" data-act="add">${t("加好友")}</button>`);
    }

    const basics = [];
    if (p.gender) {
      basics.push(H`<div class="profile-link">
          <svg class="icon icon--s"><use href="#i-user"/></svg>
          <span class="profile-link__label">${t("性别")}</span>
          <span class="profile-link__value">${genderText(p.gender)}</span>
        </div>`);
    }
    if (p.birthday) {
      basics.push(H`<div class="profile-link">
          <svg class="icon icon--s"><use href="#i-clock"/></svg>
          <span class="profile-link__label">${t("出生日期")}</span>
          <span class="profile-link__value">${escapeHtml(p.birthday)}${
            p.age ? H` · ${t("{n} 岁", { n: p.age })}` : ""
          }</span>
        </div>`);
    }
    const links = (p.links || []).map(linkItem).join("");
    // 只有真的一条资料都没填时才提示「去补一下」，填了还挂这句会让人以为没保存
    const emptyHint =
      p.isSelf && !basics.length && !links
        ? H`<div class="profile-empty">${t("还没有填写资料，点「编辑资料」补充一下吧")}</div>`
        : "";

    bodyEl.innerHTML = H`
      <div class="profile-card ${p.coverUrl ? "has-cover" : ""}">
        <div class="profile-card__cover ${p.coverUrl ? "has-image" : ""}"
             ${p.coverUrl ? H`style="background-image:url('${escapeHtml(p.coverUrl)}')"` : ""}>
          ${
            p.isSelf
              ? H`<div class="profile-cover__ops">
                   <button class="btn btn--subtle btn--sm" type="button" data-act="cover">
                     <svg class="icon icon--s"><use href="#i-image"/></svg>${p.coverUrl ? t("更换背景") : t("设置背景")}
                   </button>
                   ${
                     p.coverUrl
                       ? H`<button class="btn btn--subtle btn--sm" type="button" data-act="cover-remove">${t("移除")}</button>`
                       : ""
                   }
                 </div>`
              : ""
          }
        </div>
        <div class="profile-card__main">
          <span class="profile-card__avatar">${avatarMarkup({ ...p, displayName: name })}</span>
          <div class="profile-card__text">
            <div class="profile-card__name">
              ${escapeHtml(name)}
              ${p.alias ? H`<span class="chip">${escapeHtml(p.displayName)}</span>` : ""}
              ${p.role === 1 ? H`<span class="chip chip--accent">${t("管理员")}</span>` : ""}
            </div>
            <div class="profile-card__meta">@${escapeHtml(p.username || "")}${
              p.joinDays ? H` · ${t("加入 {n} 天", { n: p.joinDays })}` : ""
            }</div>
            ${p.bio ? H`<p class="profile-card__bio">${escapeHtml(p.bio)}</p>` : ""}
          </div>
          <div class="profile-card__ops">${ops.join("")}</div>
        </div>
      </div>
      ${
        basics.length || links || !p.isSelf
          ? H`<div class="profile-panels">
              <section class="profile-panel">
                <h3 class="profile-panel__title">${t("基本信息")}</h3>
                ${basics.length ? basics.join("") : H`<div class="profile-empty">${t("没有公开的信息")}</div>`}
              </section>
              <section class="profile-panel">
                <h3 class="profile-panel__title">${t("联系方式与社交账号")}</h3>
                ${links || H`<div class="profile-empty">${t("没有公开的信息")}</div>`}
              </section>
            </div>`
          : ""
      }
      ${emptyHint}
      ${p.isSelf ? H`<input type="file" accept="image/*" data-cover-input hidden>` : ""}`;
  }

  async function load() {
    try {
      const data = await api.get(`/api/users/${targetId}/profile`);
      state.profile = data.profile;
    } catch (err) {
      state.profile = null;
      if (err?.status && err.status !== 404) Toast.error(err?.message || t("加载失败"));
    }
    state.loading = false;
    paint();
  }

  root.querySelector('[data-act="refresh"]').addEventListener("click", () => {
    state.loading = true;
    paint();
    void load();
  });

  bodyEl.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-act]");
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === "edit") void openEditor();
    else if (act === "share") void openShare();
    else if (act === "add") void addFriend();
    else if (act === "chat") void openChat();
    else if (act === "cover") bodyEl.querySelector("[data-cover-input]")?.click();
    else if (act === "cover-remove") void removeCover();
    else if (act === "incoming") {
      location.hash = "#/friends";
    }
  });

  bodyEl.addEventListener("change", (event) => {
    if (event.target.matches("[data-cover-input]")) void uploadCover(event.target);
  });

  async function uploadCover(input) {
    const file = input.files?.[0];
    input.value = "";
    if (!file) return;
    if (!/^image\//.test(file.type)) {
      Toast.error(t("请选择图片文件"));
      return;
    }
    if (file.size > 16 * 1024 * 1024) {
      Toast.error(t("图片太大了，请选 16 MB 以内的"));
      return;
    }
    try {
      const blob = await prepareCover(file);
      await api.raw("/api/me/profile/cover", blob, "PUT");
      Toast.success(t("主页背景已更新"));
      await load();
    } catch (err) {
      Toast.error(err?.message || t("背景上传失败"));
    }
  }

  async function removeCover() {
    const ok = await Dialog.confirm({
      title: t("移除主页背景"),
      message: t("移除后回到默认渐变，图片文件会从服务器上删掉。"),
      confirmText: t("移除"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del("/api/me/profile/cover");
      Toast.success(t("主页背景已移除"));
      await load();
    } catch (err) {
      Toast.error(err?.message || t("操作失败"));
    }
  }

  async function addFriend() {
    const p = state.profile;
    const message = await Dialog.prompt({
      title: t("发送好友申请"),
      message: t("可以附一句说明，让对方知道你是谁（可留空）"),
      label: t("附言"),
      placeholder: t("例如：我们在同一个群里"),
      confirmText: t("发送申请"),
    });
    if (message === null) return;
    try {
      await api.post("/api/friends/requests", { userId: targetId, message });
      Toast.success(t("好友申请已发送"));
      p.relation = "pending_out";
      paint();
    } catch (err) {
      Toast.error(err?.message || t("发送失败"));
    }
  }

  async function openChat() {
    try {
      const data = await api.post("/api/chat/conversations/direct", { userId: targetId });
      const conv = data?.conversation;
      location.hash = conv ? `#/chat/${conv.id}` : "#/chat";
    } catch (err) {
      Toast.error(err?.message || t("打不开这个会话"));
    }
  }

  async function openShare() {
    let detail;
    try {
      detail = await api.get("/api/me/profile/detail");
    } catch (err) {
      Toast.error(err?.message || t("加载失败"));
      return;
    }
    const token = detail.profile?.shareToken;
    const backdrop = document.createElement("div");
    backdrop.className = "picker-backdrop";
    const paintShare = (info) => {
      const enabled = Boolean(info.token);
      const link = enabled ? `${location.origin}/u/${info.token}` : "";
      backdrop.innerHTML = H`
        <div class="picker picker--narrow" role="dialog" aria-modal="true">
          <div class="picker__head">
            <svg class="icon icon--s"><use href="#i-share"/></svg><span>${t("分享个人主页")}</span>
          </div>
          <div class="picker__body">
            <p class="muted">${t("开启后会生成一个公开链接，任何拿到链接的人都能看到你公开的资料（名称、简介与你打开的选项）。随时可以关掉，关掉后旧链接立即失效。")}</p>
            ${
              enabled
                ? H`<div class="share-link">
                     <span class="textbox"><input type="text" readonly value="${escapeHtml(link)}" data-share-link></span>
                     <button class="btn btn--subtle" type="button" data-act="copy">${t("复制")}</button>
                   </div>`
                : H`<div class="profile-empty">${t("分享链接当前是关闭的")}</div>`
            }
          </div>
          <div class="picker__foot">
            <button class="btn btn--subtle" type="button" data-act="toggle">${
              enabled ? t("关闭分享") : t("开启分享")
            }</button>
            <span style="flex:1"></span>
            <button class="btn btn--accent" type="button" data-act="close">${t("关闭")}</button>
          </div>
        </div>`;
      const close = () => {
        backdrop.remove();
        document.removeEventListener("keydown", onKey);
      };
      const onKey = (event) => {
        if (event.key === "Escape") close();
      };
      document.addEventListener("keydown", onKey);
      backdrop.addEventListener("click", async (event) => {
        if (!event.composedPath().includes(backdrop.querySelector(".picker"))) {
          close();
          return;
        }
        const btn = event.target.closest("[data-act]");
        if (!btn) return;
        if (btn.dataset.act === "close") {
          close();
        } else if (btn.dataset.act === "copy") {
          await copyText(link, t("链接已复制"));
        } else if (btn.dataset.act === "toggle") {
          try {
            const data = await api.post("/api/me/profile/share", { enabled: !enabled });
            Toast.success(data.enabled ? t("分享链接已开启") : t("分享链接已关闭"));
            paintShare({ token: data.token });
          } catch (err) {
            Toast.error(err?.message || t("操作失败"));
          }
        }
      });
    };
    paintShare({ token });
  }

  async function openEditor() {
    let detail;
    try {
      detail = await api.get("/api/me/profile/detail");
    } catch (err) {
      Toast.error(err?.message || t("加载失败"));
      return;
    }
    const data = detail.profile || {};
    const draft = {
      displayName: data.displayName || "",
      bio: data.bio || "",
      gender: data.gender || "",
      birthday: data.birthday || "",
      links: { ...(data.links || {}) },
      show: { ...(data.show || {}) },
    };

    const backdrop = document.createElement("div");
    backdrop.className = "picker-backdrop";
    backdrop.innerHTML = H`
      <div class="picker picker--editor" role="dialog" aria-modal="true">
        <div class="picker__head">
          <svg class="icon icon--s"><use href="#i-user"/></svg><span>${t("编辑个人资料")}</span>
        </div>
        <div class="picker__body">
          <div class="profile-form">
            <label class="field">
              <span class="field__label">${t("名称")}</span>
              <span class="textbox"><input type="text" maxlength="32" data-f="displayName"
                     value="${escapeHtml(draft.displayName)}" placeholder="${t("留空则显示用户名")}"></span>
            </label>
            <label class="field">
              <span class="field__label">${t("简介")}</span>
              <span class="textbox"><textarea rows="3" maxlength="200" data-f="bio"
                     placeholder="${t("介绍一下自己…")}">${escapeHtml(draft.bio)}</textarea></span>
            </label>
            <div class="profile-form__row">
              <div class="field">
                <span class="field__label">${t("性别")}</span>
                <div class="segmented" data-gender>
                  ${GENDERS.map(
                    (g) => H`<button class="segmented__btn ${draft.gender === g.value ? "is-active" : ""}"
                              type="button" data-gender-val="${g.value}">${t(g.label)}</button>`
                  ).join("")}
                </div>
              </div>
              <label class="field">
                <span class="field__label">${t("出生日期")}</span>
                <span class="textbox"><input type="date" data-f="birthday" value="${escapeHtml(draft.birthday)}"></span>
              </label>
            </div>
            <div class="profile-form__hint">${t("下面每一项都能单独决定是否在主页上显示；关掉开关就只对自己可见。")}</div>
            <div class="profile-form__links">
              ${LINK_ORDER.map((key) => {
                const shown = draft.show[key] !== false;
                return H`
                <div class="profile-form__link" data-key="${key}">
                  <span class="profile-form__link-label">
                    <svg class="icon icon--s"><use href="#${LINK_ICONS[key] || "i-link"}"/></svg>
                    ${t(LINK_LABELS[key])}
                  </span>
                  <span class="textbox">
                    <input type="text" maxlength="120" data-link="${key}"
                           value="${escapeHtml(draft.links[key] || "")}"
                           placeholder="${key === "email" ? escapeHtml(data.email || t("未绑定邮箱")) : t("可留空")}">
                  </span>
                  <label class="profile-form__toggle" title="${t("是否在主页显示")}">
                    <input type="checkbox" data-show="${key}" ${shown ? "checked" : ""}>
                    <span>${t("显示")}</span>
                  </label>
                </div>`;
              }).join("")}
            </div>
          </div>
        </div>
        <div class="picker__foot">
          <span class="muted">${t("名称与简介总是显示")}</span>
          <span style="flex:1"></span>
          <button class="btn btn--subtle" type="button" data-act="cancel">${t("取消")}</button>
          <button class="btn btn--accent" type="button" data-act="save">${t("保存")}</button>
        </div>
      </div>`;

    const close = () => {
      backdrop.remove();
      document.removeEventListener("keydown", onKey);
    };
    const onKey = (event) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("keydown", onKey);
    document.body.appendChild(backdrop);

    const form = backdrop.querySelector(".profile-form");
    form.addEventListener("input", (event) => {
      const el = event.target;
      if (el.dataset.f) draft[el.dataset.f] = el.value;
      if (el.dataset.link) draft.links[el.dataset.link] = el.value;
      if (el.dataset.show) draft.show[el.dataset.show] = el.checked;
    });
    backdrop.querySelectorAll("[data-gender-val]").forEach((btn) => {
      btn.addEventListener("click", () => {
        draft.gender = btn.dataset.genderVal;
        backdrop.querySelectorAll("[data-gender-val]").forEach((b) => {
          b.classList.toggle("is-active", b === btn);
        });
      });
    });

    backdrop.addEventListener("click", async (event) => {
      if (!event.composedPath().includes(backdrop.querySelector(".picker"))) {
        close();
        return;
      }
      const btn = event.target.closest("[data-act]");
      if (!btn) return;
      if (btn.dataset.act === "cancel") {
        close();
        return;
      }
      if (btn.dataset.act !== "save") return;
      btn.disabled = true;
      try {
        // 名称 / 简介走原来的资料接口（同一套校验），其余走扩展资料接口
        await api.patch("/api/me/profile", {
          displayName: draft.displayName,
          bio: draft.bio,
        });
        await api.patch("/api/me/profile/detail", {
          gender: draft.gender,
          birthday: draft.birthday,
          links: draft.links,
          show: draft.show,
        });
        Toast.success(t("资料已更新"));
        close();
        await load();
      } catch (err) {
        Toast.error(err?.message || t("保存失败"));
      } finally {
        btn.disabled = false;
      }
    });
  }

  void load();
}
