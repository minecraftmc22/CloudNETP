/**
 * 登录 / 注册视图
 */
import { api, ApiError } from "../api.js";

const TEMPLATE = `
  <div class="auth-stage">
    <div class="auth-art">
      <span class="auth-art__eyebrow">
        <svg class="icon icon--s"><use href="#i-star"/></svg>
        私有部署 · 文件留在自己手里
      </span>
      <h1 class="auth-art__title">把文件收进<br>属于你的樱花云</h1>
      <p class="auth-art__desc">
        多用户独立空间、分片断点续传、在线预览、分享链接与 WebDAV 挂载，
        全部跑在你自己的一台机器上。没有广告，没有限速。
      </p>
      <div class="auth-art__points">
        <span class="chip chip--accent"><svg class="icon icon--s"><use href="#i-upload"/></svg>断点续传</span>
        <span class="chip"><svg class="icon icon--s"><use href="#i-image"/></svg>在线预览</span>
        <span class="chip"><svg class="icon icon--s"><use href="#i-link"/></svg>分享链接</span>
        <span class="chip"><svg class="icon icon--s"><use href="#i-shield"/></svg>独立空间</span>
      </div>
    </div>

    <div class="auth-card">
      <div class="auth-card__head">
        <div class="auth-card__logo"><svg class="icon"><use href="#i-cloud"/></svg></div>
        <div class="auth-card__title" data-title>欢迎回来</div>
        <div class="auth-card__sub" data-sub>登录后继续管理你的文件</div>
      </div>

      <div class="segmented" data-segmented>
        <div class="segmented__thumb" data-thumb></div>
        <button class="segmented__btn is-active" type="button" data-mode="login">登录</button>
        <button class="segmented__btn" type="button" data-mode="register">注册</button>
      </div>

      <form class="auth-form" novalidate>
        <label class="field">
          <span class="field__label">用户名</span>
          <span class="textbox">
            <svg class="icon"><use href="#i-user"/></svg>
            <input name="username" type="text" autocomplete="username" placeholder="2-32 位，中英文/数字" spellcheck="false">
          </span>
        </label>
        <label class="field">
          <span class="field__label">密码</span>
          <span class="textbox">
            <svg class="icon"><use href="#i-shield"/></svg>
            <input name="password" type="password" autocomplete="current-password" placeholder="至少 8 位">
          </span>
        </label>
        <label class="field" data-confirm hidden>
          <span class="field__label">确认密码</span>
          <span class="textbox">
            <svg class="icon"><use href="#i-check"/></svg>
            <input name="confirm" type="password" autocomplete="new-password" placeholder="再输入一次">
          </span>
        </label>
        <div class="auth-form__msg" data-msg></div>
        <button class="btn btn--accent btn--block auth-form__submit" type="submit" data-submit>
          <span>登录</span>
        </button>
      </form>

      <div class="auth-foot">
        <span data-version>CloudNETP</span>
        <span data-announcement></span>
      </div>
    </div>
  </div>
`;

export function renderAuth(root, { meta = {}, onSuccess }) {
  root.innerHTML = TEMPLATE;

  const form = root.querySelector("form");
  const msg = root.querySelector("[data-msg]");
  const submit = root.querySelector("[data-submit]");
  const confirmField = root.querySelector("[data-confirm]");
  const thumb = root.querySelector("[data-thumb]");
  const segButtons = [...root.querySelectorAll("[data-mode]")];
  const titleEl = root.querySelector("[data-title]");
  const subEl = root.querySelector("[data-sub]");
  const versionEl = root.querySelector("[data-version]");
  const announcementEl = root.querySelector("[data-announcement]");

  versionEl.textContent = `${meta.appName || "CloudNETP"} v${meta.version || "0.1.0"}`;
  if (meta.announcement) announcementEl.textContent = meta.announcement;

  let mode = "login";

  function layoutThumb() {
    const activeBtn = segButtons.find((b) => b.classList.contains("is-active"));
    if (!activeBtn) return;
    thumb.style.width = `${activeBtn.offsetWidth}px`;
    thumb.style.transform = `translateX(${activeBtn.offsetLeft - 3}px)`;
  }

  function setMode(next) {
    mode = next;
    segButtons.forEach((b) => b.classList.toggle("is-active", b.dataset.mode === next));
    confirmField.hidden = next !== "register";
    titleEl.textContent = next === "login" ? "欢迎回来" : "创建你的空间";
    subEl.textContent =
      next === "login" ? "登录后继续管理你的文件" : "首个注册账号会自动成为管理员";
    submit.querySelector("span").textContent = next === "login" ? "登录" : "注册并进入";
    msg.textContent = "";
    msg.classList.remove("is-ok");
    form.querySelector('[name="password"]').autocomplete =
      next === "login" ? "current-password" : "new-password";
    layoutThumb();
    window.requestAnimationFrame(layoutThumb);
  }

  segButtons.forEach((btn) => btn.addEventListener("click", () => setMode(btn.dataset.mode)));

  if (meta.allowRegister === false) {
    const regBtn = segButtons.find((b) => b.dataset.mode === "register");
    if (regBtn) {
      regBtn.disabled = true;
      regBtn.style.opacity = "0.4";
      regBtn.title = "站点已关闭注册";
    }
  }

  function showError(text) {
    msg.textContent = text;
    msg.classList.remove("is-ok");
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submit.dataset.busy === "1") return;

    const username = form.username.value.trim();
    const password = form.password.value;
    const confirm = form.confirm.value;

    if (!username) return showError("请输入用户名");
    if (!password) return showError("请输入密码");
    if (mode === "register") {
      if (password.length < 8) return showError("密码至少 8 位");
      if (password !== confirm) return showError("两次输入的密码不一致");
    }

    submit.dataset.busy = "1";
    submit.disabled = true;
    const original = submit.innerHTML;
    submit.innerHTML = `<span class="spinner" style="width:15px;height:15px;border-width:2px"></span>`;
    msg.textContent = "";

    try {
      const path = mode === "login" ? "/api/auth/login" : "/api/auth/register";
      const data = await api.post(path, { username, password });
      submit.innerHTML = original;
      submit.dataset.busy = "0";
      onSuccess?.(data.user, data.firstAdmin);
    } catch (err) {
      const text = err instanceof ApiError ? err.message : "操作失败，请稍后再试";
      showError(text);
      submit.innerHTML = original;
      submit.disabled = false;
      submit.dataset.busy = "0";
    }
  });

  setMode("login");
  window.addEventListener("resize", layoutThumb, { passive: true });
  window.requestAnimationFrame(() => {
    layoutThumb();
    form.username.focus();
  });
}
