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
import { sendCode } from "../ui/email-code.js";
import {
  describeError as describeWebauthnError,
  getAssertion,
  supported as webauthnSupported,
} from "../ui/webauthn.js";

// 注意是函数：模块加载时语言还没初始化，模板必须在渲染时现算
// 勾选状态放在模块作用域：登录页切语言会整块重绘，勾过的不该被清掉
let agreeChecked = false;
/** 上一次渲染挂在 window 上的监听收尾器（#auth-root 常驻，innerHTML 清空不会撤监听） */
let authViewAbort = null;

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
        <!-- 注册拆成两步：先账号、再密码，卡片不用再拉成一长条 -->
        <div class="auth-step" data-reg-step="1">
          <div class="auth-row">
            <label class="field">
              <span class="field__label" data-account-label>用户名</span>
              <span class="textbox">
                <svg class="icon"><use href="#i-user"/></svg>
                <input name="account" type="text" autocomplete="username" placeholder="用户名" spellcheck="false" maxlength="254">
              </span>
              <span class="field__hint" data-account-hint hidden></span>
            </label>

            <label class="field" data-f-email hidden>
              <span class="field__label">邮箱 <span class="field__opt" data-email-opt>可选</span></span>
              <span class="textbox">
                <svg class="icon"><use href="#i-mail"/></svg>
                <input name="email" type="email" autocomplete="email" placeholder="you@example.com" maxlength="254">
              </span>
            </label>
          </div>

          <div class="field" data-f-emailcode hidden>
            <span class="field__label">邮箱验证码</span>
            <div class="code-row">
              <span class="textbox">
                <svg class="icon"><use href="#i-shield"/></svg>
                <input name="emailcode" type="text" inputmode="numeric" autocomplete="one-time-code"
                       placeholder="6 位数字" maxlength="6" spellcheck="false">
              </span>
              <button class="btn" type="button" data-act="send-code"
                      data-idle-label="发送验证码">发送验证码</button>
            </div>
            <span class="field__hint">验证码发送到上面填的邮箱，注册时需要它证明邮箱是你的</span>
          </div>

          <div class="announce announce--card" data-mail-warn hidden></div>
        </div>

        <div class="auth-step" data-reg-step="2" hidden>
          <div class="auth-row">
            <label class="field" data-f-password>
              <span class="field__label">密码</span>
              <span class="textbox">
                <svg class="icon"><use href="#i-shield"/></svg>
                <input name="password" type="password" autocomplete="current-password" maxlength="${PASSWORD_MAX}" placeholder="至少 8 位">
                <button class="pwd-eye" type="button" data-eye aria-label="显示密码" title="显示密码" aria-pressed="false">
                  <svg class="icon"><use href="#i-eye"/></svg>
                </button>
              </span>
            </label>

            <label class="field" data-f-confirm hidden>
              <span class="field__label">确认密码</span>
              <span class="textbox">
                <svg class="icon"><use href="#i-check"/></svg>
                <input name="confirm" type="password" autocomplete="new-password" placeholder="再输入一次">
                <button class="pwd-eye" type="button" data-eye aria-label="显示密码" title="显示密码" aria-pressed="false">
                  <svg class="icon"><use href="#i-eye"/></svg>
                </button>
              </span>
              <span class="field__error" data-confirm-err hidden></span>
            </label>
          </div>

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
        </div>

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
          <button class="link-btn" type="button" data-act="reg-back" hidden>上一步</button>
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
            <button class="pwd-eye" type="button" data-eye aria-label="显示密码" title="显示密码" aria-pressed="false">
              <svg class="icon"><use href="#i-eye"/></svg>
            </button>
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
            <button class="pwd-eye" type="button" data-eye aria-label="显示密码" title="显示密码" aria-pressed="false">
              <svg class="icon"><use href="#i-eye"/></svg>
            </button>
          </span>
          <span class="field__error" data-fconfirm-err hidden></span>
        </label>

        <div class="auth-form__msg" data-fmsg></div>
        <button class="btn btn--accent btn--block" type="submit" data-fsubmit><span>下一步</span></button>
        <div class="forgot-help" data-fhelp hidden></div>
        <div class="auth-links">
          <button class="link-btn" type="button" data-act="back-login">返回登录</button>
        </div>
      </form>

      <form class="auth-form" data-tf novalidate hidden>
        <div class="auth-card__title auth-card__title--sm">两步验证</div>
        <div class="auth-card__sub" data-tf-sub>密码已通过，再验证一次就能进入</div>

        <div class="tf-methods" data-tf-methods>
          <button class="btn btn--accent btn--block" type="button" data-tf-passkey>
            <svg class="icon"><use href="#i-shield"/></svg>
            <span>使用通行密钥</span>
          </button>
          <button class="btn btn--subtle btn--block" type="button" data-tf-email>
            用邮箱验证码
          </button>
        </div>

        <div data-tf-emailbox hidden>
          <div class="field">
            <span class="field__label">邮箱验证码</span>
            <div class="code-row">
              <span class="textbox">
                <svg class="icon"><use href="#i-mail"/></svg>
                <input name="tfcode" type="text" inputmode="numeric" autocomplete="one-time-code"
                       placeholder="6 位数字" maxlength="6" spellcheck="false">
              </span>
              <button class="btn btn--subtle" type="button" data-tf-send
                      data-idle-label="发送验证码">发送验证码</button>
            </div>
            <span class="field__hint" data-tf-hint hidden></span>
          </div>
          <button class="btn btn--accent btn--block" type="submit" data-tf-submit>
            <span>验证并登录</span>
          </button>
          <div class="auth-links">
            <button class="link-btn" type="button" data-act="tf-back">换一种方式</button>
          </div>
        </div>

        <div class="auth-form__msg" data-tfmsg></div>
        <div class="auth-links">
          <button class="link-btn" type="button" data-act="tf-cancel">返回登录</button>
        </div>
      </form>

      <div class="auth-foot">
        <a class="auth-foot__link" href="/pickup">
          <svg class="icon"><use href="#i-bolt"/></svg>
          <span>${t("有取件码？去取件")}</span>
        </a>
      </div>
    </div>
  </div>
`;

export function renderAuth(root, { meta = {}, onSuccess, onBanned }) {
  // #auth-root 是常驻节点，window 上的监听要随这次渲染一起收尾，别越叠越多
  authViewAbort?.abort();
  authViewAbort = new AbortController();
  const authSignal = authViewAbort.signal;

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
  const announceEl = el("[data-auth-announce]");
  // 注册模式字段多，卡片换成「更宽更矮」的排版，靠这两个类切换
  const stageEl = el(".auth-stage");
  const cardEl = el(".auth-card");
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
  // 注册分两步：1 账号信息 / 2 密码与验证
  const regStep1 = el('[data-reg-step="1"]');
  const regStep2 = el('[data-reg-step="2"]');
  const regBackBtn = el('[data-act="reg-back"]');
  const forgotBtn = el('[data-act="forgot"]');
  const forgotSub = el("[data-fsub]");
  const forgotEmailField = el("[data-f-femail]");
  const forgotNewField = el("[data-f-fnew]");
  const forgotStrength = el("[data-fstrength]");
  const forgotConfirmField = el("[data-f-fconfirm]");
  const forgotHelp = el("[data-fhelp]");
  const forgotSubmit = el("[data-fsubmit]");
  const agreeRow = el("[data-agree-row]");
  const agreeInput = el("[data-agree]");
  const emailCodeField = el("[data-f-emailcode]");
  const emailCodeInput = form.querySelector('[name="emailcode"]');
  const sendCodeBtn = el('[data-act="send-code"]');
  const mailWarn = el("[data-mail-warn]");
  const emailOpt = el("[data-email-opt]");
  const tfForm = el("[data-tf]");
  const tfMsg = el("[data-tfmsg]");
  const tfEmailBox = el("[data-tf-emailbox]");
  const tfMethodsBox = el("[data-tf-methods]");
  const tfSendBtn = el("[data-tf-send]");
  const tfHint = el("[data-tf-hint]");
  const tfSubmit = el("[data-tf-submit]");

  // 邮件服务就绪才强制邮箱验证；「开了但没配完」降级并提醒管理员
  const emailRequired = Boolean(meta.emailCodeRequired);

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

  renderAnnounce(announceEl, meta.announcement, { card: true });

  let mode = "login";
  let regStep = 1;
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

  /* ---------- 注册分步：先账号，再密码 ---------- */
  function setRegStep(next) {
    regStep = next === 2 ? 2 : 1;
    const first = regStep === 1;
    const registering = mode === "register";
    // 登录模式账号和密码都在，只有注册才真的拆成两步
    regStep1.hidden = registering && !first;
    regStep2.hidden = registering && first;
    if (regBackBtn) regBackBtn.hidden = first;
    // 注册流程里不提供「忘记密码」：还没账号，点它只会绕回去
    if (forgotBtn) forgotBtn.hidden = registering;
    if (registering) {
      // 图形码只在最后一步出现，第一步就亮出来容易让人以为马上要提交
      if (first) hideCaptcha();
      else showCaptcha();
      subEl.textContent = first
        ? meta.firstUser
          ? t("第 1 步 / 共 2 步 · 首个注册账号会成为超级管理员")
          : t("第 1 步 / 共 2 步 · 账号信息")
        : t("第 2 步 / 共 2 步 · 设置密码");
      submit.querySelector("span").textContent = first ? t("下一步") : t("注册并进入");
    }
    msg.textContent = "";
    msg.classList.remove("is-ok");
  }

  // 只在本地挡明显错的格式，真正的校验仍然在服务端
  const looksLikeEmail = (value) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);

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
    const registering = next === "register";
    segButtons.forEach((b) => b.classList.toggle("is-active", b.dataset.mode === next));
    stageEl?.classList.toggle("is-register", registering);
    cardEl?.classList.toggle("is-register", registering);
    confirmField.hidden = !registering;
    emailField.hidden = !registering;
    // 邮箱验证码只在「注册 + 邮件服务就绪」时出现
    if (emailCodeField) emailCodeField.hidden = !(registering && emailRequired);
    if (mailWarn) mailWarn.hidden = !(registering && meta.mailMisconfigured);
    if (emailOpt) {
      emailOpt.textContent = t(emailRequired ? "必填" : "可选");
      emailOpt.classList.toggle("is-required", emailRequired);
    }
    if (mailWarn && meta.mailMisconfigured) {
      mailWarn.textContent = t(
        "管理员已开启邮件服务但尚未配置完成，本次注册暂不校验邮箱；请联系管理员补全配置。"
      );
    }
    accountLabel.textContent = next === "login" ? t("用户名 / 邮箱") : t("用户名");
    accountInput.placeholder =
      next === "login" ? t("用户名或邮箱") : t("至少 4 位，中英文/数字");
    accountInput.autocomplete = next === "login" ? "username" : "username";
    titleEl.textContent = next === "login" ? t("欢迎回来") : t("创建你的空间");
    passwordInput.autocomplete = next === "login" ? "current-password" : "new-password";
    strengthBox.hidden = next !== "register";
    if (next === "register") {
      updateStrength();
    } else {
      subEl.textContent = t("登录后继续管理你的文件");
      submit.querySelector("span").textContent = t("登录");
      hideCaptcha();
    }
    // 每次切模式都回到第一步：subEl 与提交按钮文案也在这里统一设置
    setRegStep(1);
    accountHint.hidden = true;
    if (confirmErr) {
      confirmErr.hidden = true;
      confirmInput?.closest(".textbox")?.classList.remove("is-bad");
    }
    msg.textContent = "";
    msg.classList.remove("is-ok");
    layoutThumb();
    window.requestAnimationFrame(layoutThumb);
  }

  segButtons.forEach((btn) => btn.addEventListener("click", () => setMode(btn.dataset.mode)));

  regBackBtn?.addEventListener("click", () => {
    setRegStep(1);
    accountInput.focus();
  });

  if (meta.allowRegister === false) {
    const regBtn = segButtons.find((b) => b.dataset.mode === "register");
    if (regBtn) {
      regBtn.disabled = true;
      regBtn.style.opacity = "0.4";
      regBtn.title = t("站点已关闭注册");
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

  /* ---------- 密码显隐 ---------- */
  // 眼睛按钮就在输入框右侧：按住看明文，再点一次回到掩码
  root.querySelectorAll("[data-eye]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const box = btn.closest(".textbox");
      const input = box?.querySelector("input");
      if (!input) return;
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.classList.toggle("is-on", show);
      btn.setAttribute("aria-pressed", show ? "true" : "false");
      const label = t(show ? "隐藏密码" : "显示密码");
      btn.title = label;
      btn.setAttribute("aria-label", label);
      btn.querySelector("use")?.setAttribute("href", show ? "#i-eye-off" : "#i-eye");
      // 换类型会把光标丢到开头，放回末尾再继续输入
      input.focus({ preventScroll: true });
      const end = input.value.length;
      try {
        input.setSelectionRange(end, end);
      } catch {
        /* 某些输入类型不支持选区，忽略 */
      }
    });
  });

  /* ---------- 确认密码：边输边比对 ---------- */
  const confirmInput = form.querySelector('[name="confirm"]');
  const confirmErr = el("[data-confirm-err]");

  function hintConfirm(input, errEl, first) {
    if (!input || !errEl) return true;
    const box = input.closest(".textbox");
    const again = input.value;
    const mismatch = Boolean(again) && again !== first.value;
    errEl.hidden = !mismatch;
    errEl.textContent = mismatch ? t("两次输入的密码不一致") : "";
    box?.classList.toggle("is-bad", mismatch);
    return !mismatch;
  }

  confirmInput?.addEventListener("input", () => {
    if (mode !== "register") return;
    hintConfirm(confirmInput, confirmErr, passwordInput);
    msg.textContent = "";
    msg.classList.remove("is-ok");
  });

  // 先填确认再回去改密码：主密码一改也要立刻重判，别等提交才发现
  passwordInput.addEventListener("input", () => {
    if (mode !== "register" || !confirmInput?.value) return;
    hintConfirm(confirmInput, confirmErr, passwordInput);
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

  const fNewInput = forgotForm.querySelector('[name="fnew"]');
  const fConfirmInput = forgotForm.querySelector('[name="fconfirm"]');
  const fConfirmErr = el("[data-fconfirm-err]");

  fConfirmInput.addEventListener("input", () => {
    if (forgotStep !== 3) return;
    clearForgotMsg();
    hintConfirm(fConfirmInput, fConfirmErr, fNewInput);
  });

  // 改了新密码也要重判确认框
  fNewInput.addEventListener("input", () => {
    if (forgotStep !== 3 || !fConfirmInput.value) return;
    hintConfirm(fConfirmInput, fConfirmErr, fNewInput);
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

  /* ---------- 注册：发送邮箱验证码 ---------- */
  sendCodeBtn?.addEventListener("click", async () => {
    const email = form.querySelector('[name="email"]').value.trim();
    if (!email) {
      showError("请先填写邮箱");
      form.querySelector('[name="email"]')?.focus();
      return;
    }
    try {
      await sendCode({
        url: "/api/auth/email/code",
        scene: "register",
        body: { target: email },
        button: sendCodeBtn,
      });
      showOk("验证码已发送，请查收邮件");
      emailCodeInput?.focus();
    } catch (err) {
      showError(err instanceof ApiError ? err.message : "发送失败，请稍后再试");
    }
  });

  /* ---------- 两步验证（登录第二步）---------- */
  let tfTicket = "";
  let tfMethods = [];

  function tfShowMessage(text, ok = false) {
    tfMsg.textContent = text ? t(text) : "";
    tfMsg.classList.toggle("is-ok", Boolean(text) && ok);
  }

  function tfReset() {
    tfTicket = "";
    tfMethods = [];
    tfForm.hidden = true;
    tfEmailBox.hidden = true;
    tfMethodsBox.hidden = false;
    tfForm.querySelector('[name="tfcode"]').value = "";
    tfHint.hidden = true;
    tfMsg.textContent = "";
    tfMsg.classList.remove("is-ok");
    form.hidden = false;
    el("[data-segmented]").hidden = false;
    links.hidden = false;
  }

  function tfEnterEmail() {
    tfMethodsBox.hidden = true;
    tfEmailBox.hidden = false;
    tfSendCode();
  }

  async function tfSendCode() {
    try {
      const res = await sendCode({
        url: "/api/auth/2fa/email",
        body: { ticket: tfTicket },
        button: tfSendBtn,
      });
      if (res?.maskedEmail) {
        tfHint.hidden = false;
        tfHint.textContent = t("验证码已发送到 {email}", { email: res.maskedEmail });
      }
      tfShowMessage("验证码已发送，请查收邮件", true);
      tfForm.querySelector('[name="tfcode"]')?.focus();
    } catch (err) {
      tfShowMessage(err instanceof ApiError ? err.message : "发送失败，请稍后再试");
    }
  }

  function enterTwoFactor(payload) {
    tfTicket = payload.ticket || "";
    tfMethods = payload.methods || [];
    form.hidden = true;
    el("[data-segmented]").hidden = true;
    links.hidden = true;
    tfForm.hidden = false;
    tfMethodsBox.hidden = false;
    tfEmailBox.hidden = true;
    const passkeyBtn = el("[data-tf-passkey]");
    const emailBtn = el("[data-tf-email]");
    if (passkeyBtn) passkeyBtn.hidden = !tfMethods.includes("passkey");
    if (emailBtn) emailBtn.hidden = !tfMethods.includes("email");
    // 只有一种方式时别让用户多点一次
    if (!tfMethods.includes("passkey") && tfMethods.includes("email")) tfEnterEmail();
  }

  el('[data-act="tf-cancel"]')?.addEventListener("click", () => {
    tfReset();
    passwordInput.value = "";
    passwordInput.focus();
  });
  el('[data-act="tf-back"]')?.addEventListener("click", () => {
    tfEmailBox.hidden = true;
    tfMethodsBox.hidden = false;
    tfShowMessage("");
  });
  el("[data-tf-email]")?.addEventListener("click", () => tfEnterEmail());
  tfSendBtn?.addEventListener("click", () => tfSendCode());

  el("[data-tf-passkey]")?.addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    if (!webauthnSupported()) {
      tfShowMessage("当前页面无法使用通行密钥（需要 HTTPS 访问），请改用邮箱验证码");
      return;
    }
    if (btn.dataset.busy === "1") return;
    btn.dataset.busy = "1";
    tfShowMessage("");
    try {
      const { options, nonce } = await api.post("/api/auth/2fa/passkey/options", {
        ticket: tfTicket,
      });
      const credential = await getAssertion(options);
      const data = await api.post("/api/auth/2fa/passkey/verify", {
        ticket: tfTicket,
        nonce,
        credential,
      });
      onSuccess?.(data.user);
    } catch (err) {
      if (err instanceof ApiError) tfShowMessage(err.message);
      else tfShowMessage(describeWebauthnError(err));
    } finally {
      btn.dataset.busy = "0";
    }
  });

  tfForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (tfSubmit.dataset.busy === "1") return;
    const code = tfForm.querySelector('[name="tfcode"]').value.trim();
    if (!code) {
      tfShowMessage("请输入邮箱验证码");
      return;
    }
    tfSubmit.dataset.busy = "1";
    tfSubmit.disabled = true;
    try {
      const data = await api.post("/api/auth/2fa/email/verify", { ticket: tfTicket, code });
      onSuccess?.(data.user);
    } catch (err) {
      tfShowMessage(err instanceof ApiError ? err.message : "验证失败，请稍后再试");
    } finally {
      tfSubmit.dataset.busy = "0";
      tfSubmit.disabled = false;
    }
  });

  /* ---------- 提交：登录 / 注册 ---------- */
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submit.dataset.busy === "1") return;

    const account = accountInput.value.trim();
    const password = passwordInput.value;
    const confirm = form.querySelector('[name="confirm"]').value;
    const email = form.querySelector('[name="email"]').value.trim();
    const emailCode = emailCodeInput?.value.trim() || "";
    const captcha = form.querySelector('[name="captcha"]').value.trim();

    if (mode === "login") {
      if (!account) return showError("请输入用户名或邮箱");
      if (!password) return showError("请输入密码");
    } else if (regStep === 1) {
      // 第一步只校验账号与邮箱：密码还没填，别在这里拦
      if (!account) return showError("请输入用户名");
      if (account.length < 4) return showError("用户名至少 4 个字符");
      if (usernameOk === false && accountHint.dataset.state === "bad") {
        return showError(accountHint.textContent || "这个用户名不能用");
      }
      // 邮件服务就绪时邮箱和验证码都是硬要求，服务端还会再校验一次
      if (emailRequired) {
        if (!email) return showError("请填写邮箱");
        if (!looksLikeEmail(email)) return showError("邮箱格式不正确");
        if (!emailCode) return showError("请填写邮箱验证码");
      } else if (email && !looksLikeEmail(email)) {
        return showError("邮箱格式不正确");
      }
      msg.textContent = "";
      msg.classList.remove("is-ok");
      setRegStep(2);
      passwordInput.focus();
      return;
    } else {
      if (!password) return showError("请输入密码");
      const pwdIssue = passwordProblem(password);
      if (pwdIssue) return showError(pwdIssue);
      if (password !== confirm) {
        hintConfirm(confirmInput, confirmErr, passwordInput);
        confirmInput?.focus();
        return showError("两次输入的密码不一致");
      }
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
        // 两步验证：会话还没建立，先拿票据走第二步
        if (data?.twoFactor?.required) {
          passwordInput.value = "";
          enterTwoFactor(data.twoFactor);
          return;
        }
        onSuccess?.(data.user, data.firstAdmin);
      } else {
        const data = await api.post("/api/auth/register", {
          username: account,
          password,
          email: email || null,
          emailCode,
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
      // 账号被封：不当作普通登录失败，直接带去申诉页（服务端已下发票据）
      if (mode === "login" && apiErr?.status === 403 && apiErr.payload?.detail?.code === "banned") {
        onBanned?.(apiErr.payload.detail);
        return;
      }
      // 邮箱码是第一步填的：过期或错了就退回第一步，别让人卡在第二步干瞪眼
      if (mode === "register" && regStep === 2 && emailRequired) {
        const text = apiErr?.message || "";
        if (text.includes("请重新获取") || text.includes("还可尝试")) {
          setRegStep(1);
          emailCodeInput?.focus();
        }
      }
      const captchaVisible = !captchaField.hidden;
      if (
        (mode === "register" && regStep === 2) ||
        apiErr?.payload?.needCaptcha ||
        captchaVisible
      ) {
        showCaptcha();
        // 一张码只能试一次：失败后立刻换新图并清空输入，
        // 否则用户会拿着已经作废的旧图重试，白白多错一次
        form.querySelector('[name="captcha"]').value = "";
        refreshCaptcha();
      } else if (mode === "login" || regStep === 1) {
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
        labelEl.textContent = t(["下一步", "验证并继续", "重置密码"][forgotStep - 1]);
      }
      forgotSubmit.disabled = false;
      forgotSubmit.dataset.busy = "0";
    }
  });

  // 分享页右上角的「注册」会带 ?mode=register 过来，直接落在注册页签
  const startMode = new URLSearchParams(location.search).get("mode") === "register" ? "register" : "login";
  setMode(startMode);
  window.addEventListener("resize", layoutThumb, { passive: true, signal: authSignal });
  window.requestAnimationFrame(() => {
    layoutThumb();
    accountInput.focus();
  });
}
