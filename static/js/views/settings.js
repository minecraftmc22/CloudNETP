/**
 * 设置视图：外观 / 装饰 / 账号
 */
import { api, escapeHtml, formatBytes } from "../api.js";
import { store, siteOrigin } from "../store.js";
import { Toast } from "../ui/toast.js";
import { Dialog } from "../ui/dialog.js";
import { avatarMarkup } from "../ui/avatar.js";
import { ACCENT_PRESETS, WALLPAPERS, patchSettingsLocal } from "../theme.js";

const THEME_MODES = [
  { value: 0, label: "亮色" },
  { value: 1, label: "暗色" },
  { value: 2, label: "跟随系统" },
];

async function persist(patch) {
  const local = patchSettingsLocal(patch);
  if (!local) return;
  try {
    await api.patch("/api/me/settings", patch);
  } catch {
    Toast.error("设置已应用，但同步到服务器失败");
  }
}

/** 回收站开关的说明文案 */
function trashDesc(s) {
  const days = Number(store.get().meta?.trashRetentionDays) || 30;
  if (s?.trashEnabled === false) return "已关闭：删除会直接彻底删除，无法恢复";
  return `开启中：删除的文件先进入回收站，${days} 天后自动清理`;
}

/* ------------------------------------------------------------------ */
/* 头像：本地裁成正方形再上传（省流量，也避免服务端收到巨图）           */
/* ------------------------------------------------------------------ */
function loadBitmap(file) {
  if (window.createImageBitmap) {
    return createImageBitmap(file).catch(() => loadViaImg(file));
  }
  return loadViaImg(file);
}

function loadViaImg(file) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve(img);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("图片读取失败，请换一张试试"));
    };
    img.src = url;
  });
}

async function squareBlob(file, side = 256) {
  const bmp = await loadBitmap(file);
  const canvas = document.createElement("canvas");
  canvas.width = side;
  canvas.height = side;
  const ctx = canvas.getContext("2d");
  const w = bmp.width || bmp.naturalWidth;
  const h = bmp.height || bmp.naturalHeight;
  const edge = Math.min(w, h);
  ctx.drawImage(bmp, (w - edge) / 2, (h - edge) / 2, edge, edge, 0, 0, side, side);
  bmp.close?.();
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/webp", 0.92));
  return blob || file;
}

export function renderSettings(root) {
  const { user } = store.get();
  const s = user?.settings || {};
  const mode = s.themeMode ?? 2;
  const displayName = (user?.displayName || user?.username || "").trim();
  const maskedPreview = displayName.slice(0, 2) || "—";

  root.innerHTML = `
    <div class="view-head">
      <div class="view-title"><svg class="icon"><use href="#i-settings"/></svg>设置</div>
      <span class="view-sub">外观与账号</span>
    </div>
    <div class="view-body">
      <div class="settings">

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-palette"/></svg>外观</div>
          <div class="setting-row">
            <div class="setting-row__main">
              <div class="setting-row__label">主题模式</div>
              <div class="setting-row__desc">跟随系统时会随 Windows 深色模式自动切换</div>
            </div>
            <div class="segmented" data-theme-seg style="width:230px">
              <div class="segmented__thumb" data-thumb></div>
              ${THEME_MODES.map(
                (m) =>
                  `<button class="segmented__btn ${m.value === mode ? "is-active" : ""}" type="button" data-mode="${m.value}">${m.label}</button>`
              ).join("")}
            </div>
          </div>
          <div class="setting-row">
            <div class="setting-row__main">
              <div class="setting-row__label">主题色</div>
              <div class="setting-row__desc">用于选中状态、按钮与进度条</div>
            </div>
            <div class="swatch-row" data-swatches>
              ${ACCENT_PRESETS.map(
                (c) =>
                  `<button class="swatch ${c.value === (s.accentColor || "#c084fc") ? "is-active" : ""}"
                     type="button" data-color="${c.value}" title="${c.name}"
                     style="background:${c.value}"></button>`
              ).join("")}
            </div>
          </div>
          <div class="setting-row">
            <div class="setting-row__main">
              <div class="setting-row__label">背景壁纸</div>
              <div class="setting-row__desc">云母（Mica）底层的取色来源</div>
            </div>
            <div class="wallpaper-row" data-wallpapers>
              ${WALLPAPERS.map(
                (w) =>
                  `<button class="wallpaper-opt wp-${w.id} ${w.id === (s.wallpaper || "sakura") ? "is-active" : ""}"
                     type="button" data-wallpaper="${w.id}">
                     ${w.id === "plain" ? '<svg class="icon icon--s"><use href="#i-close"/></svg>' : ""}
                     <span class="wallpaper-opt__name">${w.name}</span>
                   </button>`
              ).join("")}
            </div>
          </div>
        </section>

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-star"/></svg>装饰</div>
          <div class="setting-row">
            <div class="setting-row__main">
              <div class="setting-row__label">花瓣飘落</div>
              <div class="setting-row__desc">极淡的樱瓣粒子，系统开启「减少动态效果」时自动停用</div>
            </div>
            <label class="switch">
              <input type="checkbox" data-toggle="showPetals" ${s.showPetals === false ? "" : "checked"}>
              <span class="switch__track"><span class="switch__knob"></span></span>
            </label>
          </div>
          <div class="setting-row">
            <div class="setting-row__main">
              <div class="setting-row__label">云宝（看板娘）</div>
              <div class="setting-row__desc">右下角的小云朵，会随页面进入打招呼</div>
            </div>
            <label class="switch">
              <input type="checkbox" data-toggle="showMascot" ${s.showMascot === false ? "" : "checked"}>
              <span class="switch__track"><span class="switch__knob"></span></span>
            </label>
          </div>
        </section>

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-trash"/></svg>文件</div>
          <div class="setting-row">
            <div class="setting-row__main">
              <div class="setting-row__label">回收站</div>
              <div class="setting-row__desc" data-trash-desc>${trashDesc(s)}</div>
            </div>
            <label class="switch">
              <input type="checkbox" data-toggle="trashEnabled" ${s.trashEnabled === false ? "" : "checked"}>
              <span class="switch__track"><span class="switch__knob"></span></span>
            </label>
          </div>
        </section>

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-database"/></svg>WebDAV</div>
          <div class="setting-row">
            <div class="setting-row__main">
              <div class="setting-row__label">挂载地址</div>
              <div class="setting-row__desc">资源管理器「映射网络驱动器」、Finder「连接服务器」都填这个地址</div>
            </div>
            <div class="dav-addr">
              <code data-dav-url>—</code>
              <button class="btn btn--subtle" type="button" data-act="dav-copy">复制</button>
            </div>
          </div>
          <div class="setting-row">
            <div class="setting-row__main">
              <div class="setting-row__label">登录凭据<span
                class="chip chip--accent chip--action" role="button" tabindex="0" data-act="dav-activate"
                ${user?.davReady ? "hidden" : ""}>待激活</span></div>
              <div class="setting-row__desc">用户名 <b data-dav-user>—</b> · 密码就是当前账号的登录密码<span
                data-dav-hint ${user?.davReady ? "hidden" : ""}><br>点右侧「待激活」输入一次密码即可启用，不用重新登录</span></div>
            </div>
            <button class="btn" type="button" data-act="dav-help">连接方法</button>
          </div>
        </section>

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-share"/></svg>分享</div>
          <div class="setting-row">
            <div class="setting-row__main">
              <div class="setting-row__label">分享页显示完整昵称</div>
              <div class="setting-row__desc">
                关闭时别人只能看到昵称前两位（当前显示为「${escapeHtml(maskedPreview)}」），头像也不会外露
              </div>
            </div>
            <label class="switch">
              <input type="checkbox" data-toggle="shareShowName" ${s.shareShowName ? "checked" : ""}>
              <span class="switch__track"><span class="switch__knob"></span></span>
            </label>
          </div>
        </section>

        <section class="settings-card">
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-user"/></svg>账号</div>

          <div class="profile">
            <button class="profile__avatar" type="button" data-act="avatar" title="更换头像">
              <span class="profile__avatar-inner" data-avatar>${avatarMarkup(user)}</span>
              <span class="profile__avatar-mask">
                <svg class="icon icon--s"><use href="#i-image"/></svg>
                <span>更换</span>
              </span>
              <span class="profile__spinner" data-avatar-busy hidden><span class="spinner"></span></span>
            </button>
            <div class="profile__fields">
              <label class="field">
                <span class="field__label">昵称</span>
                <span class="textbox">
                  <input type="text" data-field="displayName" maxlength="32" autocomplete="nickname"
                         value="${escapeHtml(user?.displayName || user?.username || "")}">
                </span>
              </label>
              <label class="field">
                <span class="field__label">简介</span>
                <span class="textbox">
                  <input type="text" data-field="bio" maxlength="200" autocomplete="off"
                         placeholder="一句话介绍自己（最多 200 字）" value="${escapeHtml(user?.bio || "")}">
                </span>
              </label>
              <div class="profile__foot">
                <span class="muted">登录名 <b>${escapeHtml(user?.username || "—")}</b> 不可修改 ·
                  已用 ${formatBytes(user?.usedBytes || 0)} / ${formatBytes(user?.quotaBytes || 0)}</span>
                <span style="flex:1"></span>
                <button class="btn btn--subtle" type="button" data-act="avatar-clear"
                        ${user?.avatarUrl ? "" : "hidden"}>移除头像</button>
                <button class="btn btn--accent" type="button" data-act="profile-save">保存资料</button>
              </div>
            </div>
            <input type="file" accept="image/*" hidden data-avatar-input>
          </div>

          <div class="setting-row">
            <div class="setting-row__main">
              <div class="setting-row__label">登录密码</div>
              <div class="setting-row__desc">修改后其它设备上的登录状态会立即失效</div>
            </div>
            <button class="btn" type="button" data-act="password">修改密码</button>
          </div>
          <div class="setting-row">
            <div class="setting-row__main">
              <div class="setting-row__label">关于</div>
              <div class="setting-row__desc" data-version>CloudNETP</div>
            </div>
            <span class="chip">${user?.role === 1 ? "管理员" : "普通用户"}</span>
          </div>
        </section>

      </div>
    </div>
  `;

  root.querySelector("[data-version]").textContent =
    `${store.get().meta?.appName || "CloudNETP"} v${store.get().meta?.version || "0.1.0"}`;

  /* --- WebDAV 挂载 --- */
  const davUrl = `${siteOrigin()}/dav/`;
  const davHost = davUrl.replace(/^https?:\/\//, "");
  // UNC 写法：\\主机@端口\dav（资源管理器的「映射网络驱动器」也吃这个）
  const davUnc = (() => {
    try {
      const url = new URL(davUrl);
      const port = url.port || (url.protocol === "https:" ? 443 : 80);
      return `\\\\${url.hostname}@${port}\\dav`;
    } catch {
      return davHost;
    }
  })();
  root.querySelector("[data-dav-url]").textContent = davHost;
  root.querySelector("[data-dav-user]").textContent = user?.username || "—";

  root.querySelector('[data-act="dav-activate"]')?.addEventListener("click", async () => {
    const values = await Dialog.form({
      title: "启用 WebDAV",
      message:
        "Windows 的 WebDAV 客户端只接受 Digest 认证，需要用你的密码生成一份凭据。<br>这里只做一次校验，不会保存明文。",
      confirmText: "启用",
      fields: [
        { name: "password", label: "当前密码", type: "password", placeholder: "登录密码" },
      ],
    });
    if (!values?.password) return;
    try {
      await api.post("/api/me/dav/activate", { password: values.password });
      const fresh = await api.get("/api/me");
      store.set({ user: fresh.user });
      const badge = root.querySelector('[data-act="dav-activate"]');
      const hint = root.querySelector("[data-dav-hint]");
      if (badge) badge.hidden = true;
      if (hint) hint.hidden = true;
      Toast.success("WebDAV 已启用，现在可以挂载了");
    } catch (err) {
      Toast.error(err.message);
    }
  });

  root.querySelector('[data-act="dav-copy"]')?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(davUrl);
      Toast.success("挂载地址已复制");
    } catch {
      Toast.error("复制失败，请手动选中地址");
    }
  });

  root.querySelector('[data-act="dav-help"]')?.addEventListener("click", () => {
    Dialog.alert({
      title: "连接 WebDAV",
      tone: "info",
      confirmText: "知道了",
      message: `
        <div class="dav-help">
          <div class="dav-help__row"><b>Windows</b>资源管理器右键「此电脑」→ 映射网络驱动器 →
            文件夹填 <code>${davUrl}</code>，或直接写 <code>${davUnc}</code>；
            勾选「使用其他凭据连接」，账号密码见上一行。</div>
          <div class="dav-help__row"><b>macOS</b>Finder → 前往 → 连接服务器（⌘K）→
            输入 <code>${davUrl}</code> → 以「注册用户」登录。</div>
          <div class="dav-help__row"><b>手机</b>Documents / RaiDrive / ES 文件浏览器里新增 WebDAV 连接，地址同上。</div>
          <div class="dav-help__row dav-help__note">Windows 的 WebDAV 客户端默认不允许在 HTTP 上发送
            明文 Basic 密码（会报 <code>0x80070043</code>），本站已改用 Digest 认证，<b>不需要改注册表</b>。
            公网使用仍建议套 HTTPS；通过 WebDAV 删除的文件同样会进回收站。</div>
        </div>`,
    });
  });

  /* --- 主题模式 --- */
  const seg = root.querySelector("[data-theme-seg]");
  const thumb = root.querySelector("[data-thumb]");
  const segBtns = [...seg.querySelectorAll("[data-mode]")];

  function layoutThumb() {
    const active = segBtns.find((b) => b.classList.contains("is-active"));
    if (!active) return;
    thumb.style.width = `${active.offsetWidth}px`;
    thumb.style.transform = `translateX(${active.offsetLeft - 3}px)`;
  }

  segBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      const value = Number(btn.dataset.mode);
      segBtns.forEach((b) => b.classList.toggle("is-active", b === btn));
      layoutThumb();
      persist({ themeMode: value });
    });
  });

  /* --- 主题色 --- */
  root.querySelectorAll("[data-color]").forEach((btn) => {
    btn.addEventListener("click", () => {
      root.querySelectorAll("[data-color]").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      persist({ accentColor: btn.dataset.color });
    });
  });

  /* --- 壁纸 --- */
  root.querySelectorAll("[data-wallpaper]").forEach((btn) => {
    btn.addEventListener("click", () => {
      root.querySelectorAll("[data-wallpaper]").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      persist({ wallpaper: btn.dataset.wallpaper });
    });
  });

  /* --- 开关 --- */
  root.querySelectorAll("[data-toggle]").forEach((input) => {
    input.addEventListener("change", () => {
      const key = input.dataset.toggle;
      if (key === "trashEnabled") return; // 回收站开关有独立的二次确认流程
      persist({ [key]: input.checked });
      if (key === "showMascot") {
        document.getElementById("mascot")?.classList.toggle("is-hidden", !input.checked);
      }
    });
  });

  /* --- 回收站开关（关闭需服务端验证码确认） --- */
  const trashSwitch = root.querySelector('[data-toggle="trashEnabled"]');
  trashSwitch?.addEventListener("change", async () => {
    if (trashSwitch.checked) {
      const ok = await switchTrash(true);
      if (!ok) trashSwitch.checked = false;
      return;
    }
    // 未通过确认前先回弹成"开"，避免开关状态骗人
    trashSwitch.checked = true;
    const done = await confirmDisableTrash();
    if (done) trashSwitch.checked = false;
  });

  async function switchTrash(enabled, code = "") {
    try {
      const res = await api.post("/api/me/trash/switch", { enabled, code });
      applyTrashState(res.settings);
      Toast.success(enabled ? "回收站已开启" : "回收站已关闭，之后删除将直接彻底删除");
      return true;
    } catch (err) {
      Toast.error(err.message);
      return false;
    }
  }

  async function confirmDisableTrash() {
    let challenge;
    try {
      challenge = await api.post("/api/me/trash/challenge");
    } catch (err) {
      Toast.error(err.message);
      return false;
    }

    const minutes = Math.max(1, Math.round((challenge.expiresIn || 300) / 60));

    for (let attempt = 0; attempt < 3; attempt += 1) {
      const code = await Dialog.prompt({
        title: "关闭回收站",
        message: `
          关闭后删除的文件<b>不会</b>进入回收站，而是直接彻底删除，无法恢复。<br>
          请把下面这串验证码原样输入到输入框，完成确认：
          <div class="code-display" data-code>${challenge.code}</div>
          <div class="dialog__hint">由服务器生成，${minutes} 分钟内有效，验证一次即作废</div>`,
        label: "验证码",
        placeholder: "输入上方 6 位字符",
        confirmText: "确认关闭",
        validate: (value) => {
          const text = value.trim();
          if (!text) return "请输入验证码";
          if (text.length !== 6) return "验证码为 6 位字符";
          return null;
        },
      });
      if (code === null) return false;

      try {
        const res = await api.post("/api/me/trash/switch", {
          enabled: false,
          code: code.trim().toUpperCase(),
        });
        applyTrashState(res.settings);
        Toast.success("回收站已关闭，之后删除将直接彻底删除");
        return true;
      } catch (err) {
        Toast.error(err.message);
        if (attempt >= 2) return false;
        // 码已失效/过期：换一张新的，别让用户拿着废码反复试
        if (/重新获取/.test(err.message)) {
          try {
            challenge = await api.post("/api/me/trash/challenge");
          } catch (e) {
            Toast.error(e.message);
            return false;
          }
        }
      }
    }
    return false;
  }

  function applyTrashState(settings) {
    const current = store.get().user;
    if (current) {
      current.settings = { ...current.settings, ...settings };
      store.set({ user: current });
      window.dispatchEvent(new CustomEvent("user:refresh", { detail: current }));
    }
    const desc = root.querySelector("[data-trash-desc]");
    if (desc) desc.textContent = trashDesc(store.get().user?.settings || {});
  }

  /* --- 个人资料：昵称 / 简介 / 头像 --- */
  const avatarInput = root.querySelector("[data-avatar-input]");
  const avatarBox = root.querySelector("[data-avatar]");
  const avatarBusy = root.querySelector("[data-avatar-busy]");
  const clearBtn = root.querySelector('[data-act="avatar-clear"]');

  function applyUser(next) {
    if (!next) return;
    store.set({ user: next });
    window.dispatchEvent(new CustomEvent("user:refresh", { detail: next }));
    if (avatarBox) avatarBox.innerHTML = avatarMarkup(next);
    if (clearBtn) clearBtn.hidden = !next.avatarUrl;
  }

  root.querySelector('[data-act="avatar"]')?.addEventListener("click", () => avatarInput?.click());

  avatarInput?.addEventListener("change", async () => {
    const file = avatarInput.files?.[0];
    avatarInput.value = "";
    if (!file) return;
    if (!/^image\//.test(file.type)) {
      Toast.error("请选择图片文件");
      return;
    }
    if (file.size > 12 * 1024 * 1024) {
      Toast.error("图片太大了，请选 12 MB 以内的");
      return;
    }
    if (avatarBusy) avatarBusy.hidden = false;
    try {
      const blob = await squareBlob(file, 256); // 先在前端裁方压好，服务端再兜底处理
      const res = await api.raw("/api/me/avatar", blob, "PUT");
      applyUser(res.user);
      Toast.success("头像已更新");
    } catch (err) {
      Toast.error(err?.message || "头像上传失败");
    } finally {
      if (avatarBusy) avatarBusy.hidden = true;
    }
  });

  clearBtn?.addEventListener("click", async () => {
    const ok = await Dialog.confirm({
      title: "移除头像",
      message: "移除后会显示昵称首字，随时可以重新上传。",
      confirmText: "移除",
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await api.del("/api/me/avatar");
      applyUser(res.user);
      Toast.success("头像已移除");
    } catch (err) {
      Toast.error(err.message);
    }
  });

  root.querySelector('[data-act="profile-save"]')?.addEventListener("click", async () => {
    const nameEl = root.querySelector('[data-field="displayName"]');
    const bioEl = root.querySelector('[data-field="bio"]');
    const name = nameEl.value.trim();
    if (!name) {
      Toast.error("昵称不能为空");
      nameEl.focus();
      return;
    }
    try {
      const res = await api.patch("/api/me/profile", { displayName: name, bio: bioEl.value.trim() });
      applyUser(res.user);
      Toast.success("资料已保存");
    } catch (err) {
      Toast.error(err.message);
    }
  });

  root.querySelector('[data-field="displayName"]')?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") root.querySelector('[data-act="profile-save"]')?.click();
  });

  /* --- 修改密码 --- */
  root.querySelector('[data-act="password"]')?.addEventListener("click", async () => {
    const result = await Dialog.form({
      title: "修改密码",
      message: "修改成功后，其它设备上的登录状态会立即失效。",
      confirmText: "保存",
      fields: [
        { name: "oldPassword", label: "当前密码", type: "password", placeholder: "当前密码" },
        { name: "newPassword", label: "新密码", type: "password", placeholder: "至少 8 位" },
        {
          name: "confirmPassword",
          label: "确认新密码",
          type: "password",
          placeholder: "再输入一次",
          validate: (v) => (v.length < 8 ? "至少 8 位" : null),
        },
      ],
    });
    if (!result) return;
    if (result.newPassword !== result.confirmPassword) {
      Toast.error("两次输入的新密码不一致");
      return;
    }
    try {
      await api.put("/api/me/password", {
        old_password: result.oldPassword,
        new_password: result.newPassword,
      });
      Toast.success("密码已更新");
    } catch (err) {
      Toast.error(err.message || "修改失败");
    }
  });

  window.requestAnimationFrame(layoutThumb);
}
