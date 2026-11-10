/**
 * 登录 / 注册 / 找回密码
 *
 * 认证相关的判定全部在服务端：
 * - 图形验证码由服务端生成，前端只拿 SVG 和 captchaId
 * - 登录失败 2 次后服务端返回 needCaptcha，前端据此把验证码框露出来
 * - 用户名是否可用、密码强度只是即时反馈，最终以提交时的服务端校验为准
 */
import { api, ApiError } from "../api.js";
import { H, LANGS, getLang, setLang, t } from "../i18n.js";
import { PASSWORD_MAX, passwordProblem, strengthOf } from "../password.js";
import { renderAnnounce } from "../ui/announce.js";

// 注意是函数：模块加载时语言还没初始化，模板必须在渲染时现算
// 勾选状态放在模块作用域：登录页切语言会整块重绘，勾过的不该被清掉
let agreeChecked = false;

const template = () => H`
  <div class="auth-stage">
    <div class="auth-lang">
      ${LANGS.map(
        (l) =>
          `<button class="auth-lang__btn ${l.id === getLang() ? "is-active" : ""}"
                   type="button" data-auth-lang="${l.id}" title="${l.label}">${l.short}</button>`
      ).join("")}
    </div>
    <div class="auth-art">
      <div class="auth-art__figure">
        <img src="/assets/brand-avatar-256.png" alt="" width="256" height="256" draggable="false">
      </div>
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
        <div class="auth-card__logo"><img src="/assets/brand-avatar.png" alt="" width="46" height="46"></div>
        <div class="auth-card__title" data-title>欢迎回来</div>
        <div class="auth-card__sub" data-sub>登录后继续管理你的文件</div>
      </div>

      <div class="announce announce--card" data-auth-announce hidden></div>

      <div class="segmented" data-segmented>
        <div class="segmented__thumb" data-thumb></div>
        <button class="segmented__btn is-active" type="button" data-mode="login">登录</button>
        <button class="segmented__btn" type="button" data-mode="register">注册</button>
      </div>

      <form class="auth-form" data-form novalidate>
        <label class="field">
          <span class="field__label" data-account-label>用户名</span>
          <span class="textbox">
            <svg class="icon"><use href="#i-user"/></svg>
            <input name="account" type="text" autocomplete="username" placeholder="用户名" spellcheck="false" maxlength="254">
          </span>
          <span class="field__hint" data-account-hint hidden></span>
        </label>

        <label class="field" data-f-email hidden>
          <span class="field__label">邮箱 <span class="field__opt">可选</span></span>
          <span class="textbox">
            <svg class="icon"><use href="#i-mail"/></svg>
            <input name="email" type="email" autocomplete="email" placeholder="用于找回密码，可稍后绑定" maxlength="254">
          </span>
        </label>

        <label class="field" data-f-password>
          <span class="field__label">密码</span>
          <span class="textbox">
            <svg class="icon"><use href="#i-shield"/></svg>
            <input name="password" type="password" autocomplete="current-password" maxlength="${PASSWORD_MAX}" placeholder="至少 8 位，含数字与小写字母">
          </span>
        </label>

        <div class="pwd" data-strength hidden>
          <div class="pwd__bar">
            <span class="pwd__cell" data-cell="0"></span>
            <span class="pwd__cell" data-cell="1"></span>
            <span class="pwd__cell" data-cell="2"></span>
            <span class="pwd__cell" data-cell="3"></span>
          </div>
          <span class="pwd__label" data-strength-label>密码强度</span>
          <ul class="pwd__rules">
            <li data-rule="len"><svg class="icon icon--s"><use href="#i-check"/></svg>至少 8 位</li>
            <li data-rule="digit"><svg class="icon icon--s"><use href="#i-check"/></svg>包含数字</li>
            <li data-rule="lower"><svg class="icon icon--s"><use href="#i-check"/></svg>包含小写字母</li>
            <li data-rule="ascii"><svg class="icon icon--s"><use href="#i-check"/></svg>仅英文/数字/符号</li>
          </ul>
        </div>

        <label class="field" data-f-confirm hidden>
          <span class="field__label">确认密码</span>
          <span class="textbox">
            <svg class="icon"><use href="#i-check"/></svg>
            <input name="confirm" type="password" autocomplete="new-password" placeholder="再输入一次">
          </span>
        </label>

        <div class="field" data-f-captcha hidden>
          <span class="field__label">验证码</span>
          <div class="captcha">
            <span class="textbox">
              <svg class="icon"><use href="#i-shield"/></svg>
              <input name="captcha" type="text" autocomplete="off" placeholder="输入右侧字符" maxlength="8" spellcheck="false">
            </span>
            <button class="captcha__img" type="button" data-captcha-refresh title="点击换一张">
              <img data-captcha-img alt="验证码">
            </button>
          </div>
        </div>

        <div class="auth-agree" data-agree-row>
          <label class="auth-agree__pick">
            <input type="checkbox" data-agree ${agreeChecked ? "checked" : ""}>
            <span class="auth-agree__box"><svg class="icon"><use href="#i-check"/></svg></span>
            <span>${t("已阅读并同意")}</span>
          </label>
          <a class="auth-agree__link" href="/terms" target="_blank" rel="noopener">${t("《用户协议》")}</a>
          <span>${t("和")}</span>
          <a class="auth-agree__link" href="/privacy" target="_blank" rel="noopener">${t("《隐私政策》")}</a>
        </div>

        <div class="auth-form__msg" data-msg></div>
        <button class="btn btn--accent btn--block auth-form__submit" type="submit" data-submit>
          <span>登录</span>
        </button>
        <div class="auth-links" data-links>
          <button class="link-btn" type="button" data-act="forgot">忘记密码？</button>
        </div>
      </form>

      <form class="auth-form" data-forgot novalidate hidden>
        <div class="auth-card__title auth-card__title--sm">找回密码</div>
        <div class="auth-card__sub" data-fsub>先填账号，我们再告诉你能怎么走</div>

        <label class="field">
          <span class="field__label">用户名 / 邮箱</span>
          <span class="textbox">
            <svg class="icon"><use href="#i-user"/></svg>
            <input name="faccount" type="text" autocomplete="username" placeholder="注册时用的用户名或邮箱" maxlength="254">
          </span>
        </label>

        <label class="field" data-f-femail hidden>
          <span class="field__label">绑定的邮箱</span>
          <span class="textbox">
            <svg class="icon"><use href="#i-mail"/></svg>
            <input name="femail" type="email" autocomplete="email" placeholder="输入注册时绑定的邮箱" maxlength="254">
          </span>
        </label>

        <label class="field" data-f-fnew hidden>
          <span class="field__label">新密码</span>
          <span class="textbox">
            <svg class="icon"><use href="#i-shield"/></svg>
            <input name="fnew" type="password" autocomplete="new-password" maxlength="${PASSWORD_MAX}" placeholder="至少 8 位，含数字与小写字母">
          </span>
        </label>

        <div class="pwd" data-fstrength hidden>
          <div class="pwd__bar">
            <span class="pwd__cell" data-fcell="0"></span>
            <span class="pwd__cell" data-fcell="1"></span>
            <span class="pwd__cell" data-fcell="2"></span>
            <span class="pwd__cell" data-fcell="3"></span>
          </div>
          <span class="pwd__label" data-fstrength-label>密码强度</span>
          <ul class="pwd__rules">
            <li data-frule="len"><svg class="icon icon--s"><use href="#i-check"/></svg>至少 8 位</li>
            <li data-frule="digit"><svg class="icon icon--s"><use href="#i-check"/></svg>包含数字</li>
            <li data-frule="lower"><svg class="icon icon--s"><use href="#i-check"/></svg>包含小写字母</li>
            <li data-frule="ascii"><svg class="icon icon--s"><use href="#i-check"/></svg>仅英文/数字/符号</li>
          </ul>
        </div>

        <label class="field" data-f-fconfirm hidden>
          <span class="field__label">确认新密码</span>
          <span class="textbox">
            <svg class="icon"><use href="#i-check"/></svg>
            <input name="fconfirm" type="password" autocomplete="new-password" maxlength="${PASSWORD_MAX}" placeholder="再输入一次">
          </span>
        </label>

        <div class="auth-form__msg" data-fmsg></div>
        <button class="btn btn--accent btn--block" type="submit" data-fsubmit><span>下一步</span></button>
        <div class="forgot-help" data-fhelp hidden></div>
        <div class="auth-links">
          <button class="link-btn" type="button" data-act="back-login">返回登录</button>
        </div>
      </form>

      <div class="auth-foot">
        <span data-version>CloudNETP</span>
      </div>
    </div>
  </div>
`;

export function renderAuth(root, { meta = {}, onSuccess }) {
  root.innerHTML = template();

  const el = (sel) => root.querySelector(sel);
  const form = el("[data-form]");
  const forgotForm = el("[data-forgot]");
  const msg = el("[data-msg]");
  const fmsg = el("[data-fmsg]");
  const submit = el("[data-submit]");
  const segButtons = [...root.querySelectorAll("[data-mode]")];
  const thumb = el("[data-thumb]");
  const titleEl = el("[data-title]");
  const subEl = el("[data-sub]");
  const versionEl = el("[data-version]");
  const announceEl = el("[data-auth-announce]");
  const accountLabel = el("[data-account-label]");
  const accountInput = form.querySelector('[name="account"]');
  const accountHint = el("[data-account-hint]");
  const passwordInput = form.querySelector('[name="password"]');
  const confirmField = el("[data-f-confirm]");
  const emailField = el("[data-f-email]");
  const strengthBox = el("[data-strength]");
  const captchaField = el("[data-f-captcha]");
  const captchaImg = el("[data-captcha-img]");
  const links = el("[data-links]");
  const forgotSub = el("[data-fsub]");
  const forgotEmailField = el("[data-f-femail]");
  const forgotNewField = el("[data-f-fnew]");
  const forgotStrength = el("[data-fstrength]");
  const forgotConfirmField = el("[data-f-fconfirm]");
  const forgotHelp = el("[data-fhelp]");
  const forgotSubmit = el("[data-fsubmit]");
  const agreeRow = el("[data-agree-row]");
  const agreeInput = el("[data-agree]");

  // 勾选状态跟着模块变量走，切语言重绘后还在
  agreeInput?.addEventListener("change", () => {
    agreeChecked = Boolean(agreeInput.checked);
    if (agreeChecked) agreeRow?.classList.remove("is-warn");
  });

  root.querySelectorAll("[data-auth-lang]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.authLang;
      if (!id || id === getLang()) return;
      setLang(id);
      // 登录页没有全局重绘（i18n:change 只重画已登录界面），这里自己来一次
      renderAuth(root, { meta, onSuccess });
    });
  });

  versionEl.textContent = `${meta.appName || "CloudNETP"} v${meta.version || "0.1.0"}`;
  renderAnnounce(announceEl, meta.announcement, { card: true });

  let mode = "login";
  let captchaId = "";
  let usernameTimer = 0;
  let usernameOk = false;

  /* ---------- 验证码 ---------- */
  async function refreshCaptcha() {
    captchaImg.removeAttribute("src");
    try {
      const data = await api.get("/api/auth/captcha");
      captchaId = data.captchaId;
      captchaImg.src = data.image;
    } catch (err) {
      captchaId = "";
      showError(err instanceof ApiError ? err.message : "验证码加载失败");
    }
  }

  function showCaptcha(needRefresh = true) {
    if (captchaField.hidden) {
      captchaField.hidden = false;
      if (needRefresh) refreshCaptcha();
    }
  }

  function hideCaptcha() {
    captchaField.hidden = true;
    captchaId = "";
    form.querySelector('[name="captcha"]').value = "";
  }

  el("[data-captcha-refresh]").addEventListener("click", refreshCaptcha);

  /* ---------- 分段切换 ---------- */
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
    emailField.hidden = next !== "register";
    accountLabel.textContent = next === "login" ? t("用户名 / 邮箱") : t("用户名");
    accountInput.placeholder =
      next === "login" ? t("用户名或邮箱") : t("至少 4 位，中英文/数字");
    accountInput.autocomplete = next === "login" ? "username" : "username";
    titleEl.textContent = next === "login" ? t("欢迎回来") : t("创建你的空间");
    subEl.textContent =
      next === "login" ? t("登录后继续管理你的文件") : t("首个注册账号会自动成为管理员");
    submit.querySelector("span").textContent = next === "login" ? t("登录") : t("注册并进入");
    passwordInput.autocomplete = next === "login" ? "current-password" : "new-password";
    strengthBox.hidden = next !== "register";
    if (next === "register") {
      showCaptcha();
      updateStrength();
    } else {
      hideCaptcha();
    }
    accountHint.hidden = true;
    msg.textContent = "";
    msg.classList.remove("is-ok");
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

  /* ---------- 提示 ---------- */
  function showError(text, target = msg) {
    target.textContent = t(text);
    target.classList.remove("is-ok");
  }

  function showOk(text, target = msg) {
    target.textContent = t(text);
    target.classList.add("is-ok");
  }

  /* ---------- 密码强度 ---------- */
  // 注册与重置共用同一套渲染：结构相同，只有外壳的 data 前缀不同
  function paintStrength(box, value) {
    if (!box) return;
    const { level, label, rules } = strengthOf(value);
    box.dataset.level = String(level);
    const labelEl = box.querySelector(".pwd__label");
    if (labelEl) labelEl.textContent = value ? t("密码强度：{label}", { label }) : t("密码强度");
    box.querySelectorAll(".pwd__cell").forEach((cell, index) => {
      cell.classList.toggle("is-on", index < level);
    });
    box.querySelectorAll(".pwd__rules li").forEach((li) => {
      const key = li.dataset.rule || li.dataset.frule;
      li.classList.toggle("is-ok", Boolean(rules[key]));
    });
  }

  function updateStrength() {
    paintStrength(strengthBox, passwordInput.value);
  }

  function updateForgotStrength() {
    paintStrength(forgotStrength, forgotForm.querySelector('[name="fnew"]').value);
  }

  passwordInput.addEventListener("input", () => {
    if (mode === "register") updateStrength();
  });

  // 重置密码第 3 步：输入新密码时实时给强度与规则勾选，并清掉上一次的报错
  function clearForgotMsg() {
    if (!fmsg.textContent) return;
    fmsg.textContent = "";
    fmsg.classList.remove("is-ok");
  }

  forgotForm.querySelector('[name="fnew"]').addEventListener("input", () => {
    if (forgotStep !== 3) return;
    updateForgotStrength();
    clearForgotMsg();
  });

  forgotForm.querySelector('[name="fconfirm"]').addEventListener("input", () => {
    if (forgotStep === 3) clearForgotMsg();
  });

  /* ---------- 用户名可用性 ---------- */
  function setAccountHint(text, state) {
    accountHint.hidden = !text;
    accountHint.textContent = text || "";
    accountHint.dataset.state = state || "";
  }

  accountInput.addEventListener("input", () => {
    if (mode !== "register") return;
    window.clearTimeout(usernameTimer);
    const name = accountInput.value.trim();
    usernameOk = false;
    if (!name) {
      setAccountHint("", "");
      return;
    }
    if (name.length < 4) {
      setAccountHint("用户名至少 4 个字符", "bad");
      return;
    }
    setAccountHint("检查中…", "wait");
    usernameTimer = window.setTimeout(async () => {
      try {
        const data = await api.get(`/api/auth/check-username?username=${encodeURIComponent(name)}`);
        if (accountInput.value.trim() !== name) return;
        usernameOk = Boolean(data.available);
        setAccountHint(data.available ? "这个用户名可以用" : data.reason, data.available ? "ok" : "bad");
      } catch {
        setAccountHint("", "");
      }
    }, 420);
  });

  /* ---------- 提交：登录 / 注册 ---------- */
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submit.dataset.busy === "1") return;

    const account = accountInput.value.trim();
    const password = passwordInput.value;
    const confirm = form.querySelector('[name="confirm"]').value;
    const email = form.querySelector('[name="email"]').value.trim();
    const captcha = form.querySelector('[name="captcha"]').value.trim();

    if (!account) return showError(mode === "login" ? "请输入用户名或邮箱" : "请输入用户名");
    if (!password) return showError("请输入密码");

    if (mode === "register") {
      if (account.length < 4) return showError("用户名至少 4 个字符");
      if (usernameOk === false && accountHint.dataset.state === "bad") {
        return showError(accountHint.textContent || "这个用户名不能用");
      }
      const pwdIssue = passwordProblem(password);
      if (pwdIssue) return showError(pwdIssue);
      if (password !== confirm) return showError("两次输入的密码不一致");
      if (!captcha) return showError("请输入图形验证码");
    }

    // 登录与注册都要先同意协议；未勾选时把勾选行抖一下，比只飘一行字更容易看见
    if (!agreeInput?.checked) {
      if (agreeRow) {
        agreeRow.classList.remove("is-warn");
        void agreeRow.offsetWidth; // 强制重排，让动画能重放
        agreeRow.classList.add("is-warn");
      }
      agreeInput?.focus();
      return showError("请先阅读并同意《用户协议》和《隐私政策》");
    }

    submit.dataset.busy = "1";
    submit.disabled = true;
    const original = submit.innerHTML;
    submit.innerHTML = H`<span class="spinner" style="width:15px;height:15px;border-width:2px"></span>`;
    msg.textContent = "";
    msg.classList.remove("is-ok");

    const restore = () => {
      submit.innerHTML = original;
      submit.disabled = false;
      submit.dataset.busy = "0";
    };

    try {
      if (mode === "login") {
        const data = await api.post("/api/auth/login", {
          username: account,
          password,
          captchaId,
          captcha,
        });
        restore();
        onSuccess?.(data.user, data.firstAdmin);
      } else {
        const data = await api.post("/api/auth/register", {
          username: account,
          password,
          email: email || null,
          captchaId,
          captcha,
          // 服务端也会校验：前端只是提前拦一次，接口不允许绕过
          agree: true,
        });
        restore();
        onSuccess?.(data.user, data.firstAdmin);
      }
    } catch (err) {
      restore();
      const apiErr = err instanceof ApiError ? err : null;
      const captchaVisible = !captchaField.hidden;
      if (mode === "register" || apiErr?.payload?.needCaptcha || captchaVisible) {
        showCaptcha();
        // 一张码只能试一次：失败后立刻换新图并清空输入，
        // 否则用户会拿着已经作废的旧图重试，白白多错一次
        form.querySelector('[name="captcha"]').value = "";
        refreshCaptcha();
      } else if (mode === "login") {
        hideCaptcha();
      }
      showError(apiErr ? apiErr.message : "操作失败，请稍后再试");
    }
  });

  /* ---------- 找回密码 ---------- */
  // 管理员联系方式是可选的：没配就只给一句指引，别把「请联系管理员」重复说两遍
  const contactLine = [];
  if (meta.contactEmail) contactLine.push(`邮箱 ${meta.contactEmail}`);
  if (meta.contactQq) contactLine.push(`QQ ${meta.contactQq}`);
  const contactHtml = contactLine.length
    ? `<p>${t("也可以直接联系站点管理员：")}<strong>${contactLine.join(" · ")}</strong></p>`
    : "";

  let forgotStep = 1;
  let resetToken = "";
  let forgotAccount = "";

  function setForgotStep(step) {
    forgotStep = step;
    const onNew = step === 3;
    forgotNewField.hidden = !onNew;
    forgotStrength.hidden = !onNew;
    forgotConfirmField.hidden = !onNew;
    forgotEmailField.hidden = step !== 2;
    const labels = ["下一步", "验证并继续", "重置密码"];
    forgotSubmit.querySelector("span").textContent = labels[step - 1];
  }

  function openForgot() {
    form.hidden = true;
    el("[data-segmented]").hidden = true;
    el("[data-links]").hidden = true;
    forgotForm.hidden = false;
    links.hidden = true;
    titleEl.textContent = t("找回密码");
    subEl.textContent = t("两种方式：用户名 + 绑定邮箱，或邮箱链接（未开通）");
    fmsg.textContent = "";
    fmsg.classList.remove("is-ok");
    forgotHelp.hidden = true;
    forgotForm.querySelector('[name="faccount"]').value = accountInput.value.trim();
    // 重置密码的三个输入不留上一次的残留
    ["femail", "fnew", "fconfirm"].forEach((name) => {
      const input = forgotForm.querySelector(`[name="${name}"]`);
      if (input) input.value = "";
    });
    paintStrength(forgotStrength, "");
    setForgotStep(1);
    forgotForm.querySelector('[name="faccount"]').focus();
  }

  function backToLogin() {
    forgotForm.hidden = true;
    form.hidden = false;
    el("[data-segmented]").hidden = false;
    links.hidden = false;
    setMode(mode);
    resetToken = "";
    ["femail", "fnew", "fconfirm"].forEach((name) => {
      const input = forgotForm.querySelector(`[name="${name}"]`);
      if (input) input.value = "";
    });
    paintStrength(forgotStrength, "");
    setForgotStep(1);
  }

  el('[data-act="forgot"]').addEventListener("click", openForgot);
  el('[data-act="back-login"]').addEventListener("click", backToLogin);

  function forgotHelpBox(text) {
    forgotHelp.hidden = false;
    forgotHelp.innerHTML = H`<p>${t(text)}</p>${contactHtml}`;
  }

  forgotForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (forgotSubmit.dataset.busy === "1") return;

    const account = forgotForm.querySelector('[name="faccount"]').value.trim();
    const email = forgotForm.querySelector('[name="femail"]').value.trim();
    const newPassword = forgotForm.querySelector('[name="fnew"]').value;
    const confirmNew = forgotForm.querySelector('[name="fconfirm"]').value;

    forgotSubmit.dataset.busy = "1";
    forgotSubmit.disabled = true;
    const original = forgotSubmit.innerHTML;
    forgotSubmit.innerHTML = H`<span class="spinner" style="width:15px;height:15px;border-width:2px"></span>`;

    try {
      if (forgotStep === 1) {
        if (!account) throw new ApiError("请输入用户名或邮箱", 0);
        const data = await api.post("/api/auth/forgot/start", { account });
        forgotAccount = account;

        if (data.mode === "email") {
          forgotSub.textContent = t("邮箱找回");
          if (!data.mailReady) {
            showError("邮件服务还没开通，暂时无法通过邮箱链接重置。", fmsg);
            forgotHelpBox("请改用「用户名」方式：输入用户名，再填注册时绑定的邮箱。");
            return;
          }
          showOk("重置链接会发到你的邮箱，请查收。", fmsg);
          forgotHelpBox("没收到邮件的话，注意看一下垃圾箱。");
          return;
        }

        if (!data.found) {
          showError("这个账号不存在", fmsg);
          forgotHelpBox("确认一下用户名有没有输错。");
          return;
        }
        if (!data.hasEmail) {
          showError("该账号还没有绑定邮箱，无法自助重置。", fmsg);
          forgotHelpBox("请把用户名和注册时间发给站点管理员，核验身份后由管理员帮你重置密码。");
          return;
        }
        forgotSub.textContent = t("账号 {account} 已绑定邮箱 {email}，请补全邮箱完成验证", {
          account,
          email: data.maskedEmail || "",
        });
        setForgotStep(2);
        fmsg.textContent = "";
        forgotHelp.hidden = true;
        forgotForm.querySelector('[name="femail"]').focus();
      } else if (forgotStep === 2) {
        if (!email) throw new ApiError("请输入绑定的邮箱", 0);
        const data = await api.post("/api/auth/forgot/email", { account: forgotAccount, email });
        resetToken = data.token;
        forgotSub.textContent = t("邮箱对上了，设置一个新密码即可");
        setForgotStep(3);
        fmsg.textContent = "";
        forgotHelp.hidden = true;
        forgotForm.querySelector('[name="fnew"]').focus();
      } else {
        // 重置密码与注册用同一套规则；前端先挡一次，后端仍会再校验
        const pwdIssue = passwordProblem(newPassword);
        if (pwdIssue) throw new ApiError(pwdIssue, 0);
        if (newPassword !== confirmNew) throw new ApiError("两次输入的新密码不一致", 0);
        const data = await api.post("/api/auth/reset", { token: resetToken, password: newPassword });
        backToLogin();
        showOk(t("@{name} 的密码已重置，请用新密码登录。", { name: data.username }));
        accountInput.value = data.username;
      }
    } catch (err) {
      showError(err instanceof ApiError ? err.message : "操作失败，请稍后再试", fmsg);
    } finally {
      forgotSubmit.innerHTML = original;
      // 步骤可能在 try 里已经推进，original 是上一步的快照：
      // 这里按当前步骤重新写一遍文案，否则会退回「验证并继续」甚至「下一步」
      const labelEl = forgotSubmit.querySelector("span");
      if (labelEl) {
        labelEl.textContent = ["下一步", "验证并继续", "重置密码"][forgotStep - 1];
      }
      forgotSubmit.disabled = false;
      forgotSubmit.dataset.busy = "0";
    }
  });

  // 分享页右上角的「注册」会带 ?mode=register 过来，直接落在注册页签
  const startMode = new URLSearchParams(location.search).get("mode") === "register" ? "register" : "login";
  setMode(startMode);
  window.addEventListener("resize", layoutThumb, { passive: true });
  window.requestAnimationFrame(() => {
    layoutThumb();
    accountInput.focus();
  });
}
