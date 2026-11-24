/**
 * 公开个人主页（/u/<token>）：不依赖登录态。
 *
 * 只显示对方打开了「显示」开关的字段：服务端已经过滤过一遍，
 * 这里拿不到未公开的数据，前端也不再做二次判断。
 */
import { H, initI18n, t } from "./i18n.js";
import { escapeHtml } from "./api.js";

initI18n();

const root = document.getElementById("profile-root");
const token = (location.pathname.match(/\/u\/([^/?#]+)/) || [])[1] || "";

const ICONS = {
  email: "i-mail",
  qq: "i-chat",
  wechat: "i-chat",
  x: "i-arrow-up-right",
  line: "i-chat",
  google: "i-search",
  discord: "i-users",
  youtube: "i-play",
  netease: "i-music",
  spotify: "i-music",
  website: "i-link",
  weibo: "i-arrow-up-right",
  github: "i-github",
  steam: "i-play",
  bilibili: "i-video",
  douyin: "i-video",
  facebook: "i-users",
  instagram: "i-image",
};

const LABELS = {
  email: "邮箱",
  qq: "QQ",
  wechat: "微信",
  x: "X",
  line: "Line",
  google: "Google",
  discord: "Discord",
  youtube: "YouTube",
  netease: "网易云音乐",
  spotify: "Spotify",
  website: "个人网站",
  weibo: "微博",
  github: "GitHub",
  steam: "Steam",
  bilibili: "Bilibili",
  douyin: "抖音 / TikTok",
  facebook: "Facebook",
  instagram: "Instagram",
};

/* 主题：先按本机缓存，再跟随系统（与分享页一致） */
(function applyTheme() {
  const el = document.documentElement;
  let cached = null;
  try {
    cached = JSON.parse(localStorage.getItem("cn:appearance") || "null");
  } catch {
    cached = null;
  }
  const preferDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const mode = cached?.themeMode ?? 2;
  const theme = mode === 0 ? "light" : mode === 1 ? "dark" : preferDark ? "dark" : "light";
  el.dataset.theme = theme;
  el.dataset.wallpaper = cached?.wallpaper || "sakura";
  if (cached?.accentColor) el.style.setProperty("--accent-user", cached.accentColor);
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", theme === "dark" ? "#150f21" : "#fdf4fa");
})();

function genderText(value) {
  if (value === "male") return t("男");
  if (value === "female") return t("女");
  if (value === "other") return t("其他");
  return "";
}

function avatarMarkup(profile) {
  if (profile.avatarUrl) {
    return `<img src="${escapeHtml(profile.avatarUrl)}" alt="" draggable="false">`;
  }
  const name = (profile.displayName || "?").trim();
  const initial = [...name][0] || "?";
  return `<span class="avatar-initial">${escapeHtml(initial.toUpperCase())}</span>`;
}

function render(profile) {
  const basics = [];
  if (profile.gender) {
    basics.push(H`
      <div class="profile-link">
        <svg class="icon icon--s"><use href="#i-user"/></svg>
        <span class="profile-link__label">${t("性别")}</span>
        <span class="profile-link__value">${genderText(profile.gender)}</span>
      </div>`);
  }
  if (profile.birthday) {
    basics.push(H`
      <div class="profile-link">
        <svg class="icon icon--s"><use href="#i-clock"/></svg>
        <span class="profile-link__label">${t("出生日期")}</span>
        <span class="profile-link__value">${escapeHtml(profile.birthday)}${
          profile.age ? H` · ${t("{n} 岁", { n: profile.age })}` : ""
        }</span>
      </div>`);
  }
  const links = (profile.links || [])
    .map((link) => {
      const label = t(LABELS[link.key] || link.key);
      const icon = ICONS[link.key] || "i-link";
      const value = link.url
        ? H`<a class="profile-link__value" href="${escapeHtml(link.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(link.value)}</a>`
        : H`<span class="profile-link__value">${escapeHtml(link.value)}</span>`;
      return H`
        <div class="profile-link">
          <svg class="icon icon--s"><use href="#${icon}"/></svg>
          <span class="profile-link__label">${label}</span>
          ${value}
        </div>`;
    })
    .join("");

  root.innerHTML = H`
    <div class="profile-card ${profile.coverUrl ? "has-cover" : ""}">
      <div class="profile-card__cover ${profile.coverUrl ? "has-image" : ""}"
           ${profile.coverUrl ? H`style="background-image:url('${escapeHtml(profile.coverUrl)}')"` : ""}></div>
      <div class="profile-card__main">
        <span class="profile-card__avatar">${avatarMarkup(profile)}</span>
        <div class="profile-card__text">
          <div class="profile-card__name">${escapeHtml(profile.displayName)}</div>
          <div class="profile-card__meta">
            <svg class="icon icon--s"><use href="#i-share"/></svg>
            ${t("分享的个人主页")}
          </div>
          ${profile.bio ? H`<p class="profile-card__bio">${escapeHtml(profile.bio)}</p>` : ""}
        </div>
      </div>
    </div>
    ${
      basics.length || links
        ? H`<div class="profile-panels">
            ${basics.length ? H`<section class="profile-panel">
                <h3 class="profile-panel__title">${t("基本信息")}</h3>
                ${basics.join("")}
              </section>` : ""}
            ${links ? H`<section class="profile-panel">
                <h3 class="profile-panel__title">${t("联系方式与社交账号")}</h3>
                ${links}
              </section>` : ""}
          </div>`
        : H`<div class="profile-panel profile-panel--empty">
            <svg class="icon"><use href="#i-lock"/></svg>
            <span>${t("对方没有公开更多资料")}</span>
          </div>`
    }`;
  document.title = `${profile.displayName} · CloudNETP`;
}

async function load() {
  if (!token) {
    root.innerHTML = H`<div class="empty"><div class="empty__title">${t("主页地址不完整")}</div></div>`;
    return;
  }
  try {
    const res = await fetch(`/api/public/profile/${encodeURIComponent(token)}`);
    if (!res.ok) throw new Error(res.status === 404 ? "gone" : "failed");
    const data = await res.json();
    if (!data?.profile) throw new Error("failed");
    render(data.profile);
  } catch (err) {
    const gone = err?.message === "gone";
    root.innerHTML = H`<div class="empty">
        <div class="empty__art"><svg class="icon"><use href="#i-lock"/></svg></div>
        <div class="empty__title">${
          gone ? t("这个主页不存在或分享已关闭") : t("加载失败，请稍后再试")
        }</div>
      </div>`;
  }
}

void load();
