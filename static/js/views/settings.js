/**
 * 设置视图：外观 / 装饰 / 账号
 */
import { api, formatBytes } from "../api.js";
import { store } from "../store.js";
import { Toast } from "../ui/toast.js";
import { Dialog } from "../ui/dialog.js";
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

export function renderSettings(root) {
  const { user } = store.get();
  const s = user?.settings || {};
  const mode = s.themeMode ?? 2;

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
          <div class="settings-card__head"><svg class="icon icon--s"><use href="#i-user"/></svg>账号</div>
          <div class="setting-row">
            <div class="setting-row__main">
              <div class="setting-row__label">${user?.username || "—"}</div>
              <div class="setting-row__desc">已用 ${formatBytes(user?.usedBytes || 0)} / 配额 ${formatBytes(user?.quotaBytes || 0)}</div>
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
      persist({ [key]: input.checked });
      if (key === "showMascot") {
        document.getElementById("mascot")?.classList.toggle("is-hidden", !input.checked);
      }
    });
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
