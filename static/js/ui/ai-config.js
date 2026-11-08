/**
 * 设置页「看板娘 · 对话设置」
 *
 * 一个用户一份配置，存在 user_settings.ai_config：
 *   接口地址 + 密钥（服务端加密、只回掩码）+ 模型 + 四个高级开关 + 输入/输出上限
 * 系统提示词是站点级配置，只有管理员能看到并修改。
 *
 * 这里刻意不做「改一个字就整块重绘」：输入框会掉焦点。只更新受影响的节点。
 */

import { api, escapeHtml } from "../api.js";
import { H, getLang, t } from "../i18n.js";
import { Toast } from "./toast.js";

const TOKEN_CHOICES = [
  { value: 32000, label: "32K" },
  { value: 64000, label: "64K" },
  { value: 128000, label: "128K" },
  { value: 256000, label: "256K" },
];

const CUSTOM_MODEL = "__custom__";

const state = {
  cfg: null,
  isAdmin: false,
  prompt: "",
  defaultPrompt: "",
  models: [],
  card: null,
  saveTimer: 0,
  keyDirty: false,
  autoProbe: null,
};

/* ------------------------------------------------------------------ */
/* 模板                                                               */
/* ------------------------------------------------------------------ */
function tokenRow(key, title, desc, value) {
  const custom = Number(value) > 0;
  const matched = TOKEN_CHOICES.some((c) => c.value === Number(value));
  return H`
    <div class="setting-row setting-row--stack">
      <div class="setting-row__main">
        <div class="setting-row__label">${title}</div>
        <div class="setting-row__desc">${desc}</div>
      </div>
      <div class="ai-tokens" data-ai-tokens="${key}">
        <label class="ai-default">
          <input type="checkbox" data-ai-default="${key}" ${custom ? "" : "checked"}>
          <span>使用模型提供商默认值</span>
        </label>
        <div class="ai-chips" ${custom ? "" : "hidden"}>
          ${TOKEN_CHOICES.map(
            (choice) => H`<button type="button" class="ai-chip ${matched && choice.value === Number(value) ? "is-active" : ""}"
              data-ai-token="${key}" data-value="${choice.value}">${choice.label}</button>`
          ).join("")}
          <span class="ai-token-custom">
            <input type="number" min="1024" step="1024" placeholder="自定义"
                   data-ai-token-input="${key}"
                   value="${custom && !matched ? Number(value) : ""}">
          </span>
        </div>
      </div>
    </div>`;
}

function configMarkup() {
  const cfg = state.cfg || {};
  const model = cfg.model || "";
  const inList = state.models.includes(model);
  const useCustomModel = !state.models.length || (model && !inList);
  return H`
    <div class="setting-row setting-row--stack">
      <div class="setting-row__main">
        <div class="setting-row__label">快速选择</div>
        <div class="setting-row__desc">只帮你把地址填上，密钥仍要自己贴</div>
      </div>
      <div class="ai-presets">
        <button class="btn btn--subtle btn--sm" type="button" data-ai-preset="openai">OPENAI</button>
        <button class="btn btn--subtle btn--sm" type="button" data-ai-preset="deepseek">DEEPSEEK</button>
      </div>
    </div>

    <div class="setting-row setting-row--stack">
      <label class="field ai-field">
        <span class="field__label">API 地址</span>
        <span class="textbox">
          <svg class="icon icon--s"><use href="#i-link"/></svg>
          <input data-ai-base placeholder="https://api.openai.com/v1" spellcheck="false"
                 value="${escapeHtml(cfg.baseUrl || "")}">
        </span>
        <span class="field__hint">OpenAI 兼容接口一般填到 <code>/v1</code>；Anthropic 填 <code>https://api.anthropic.com</code></span>
      </label>
    </div>

    <div class="setting-row setting-row--stack">
      <label class="field ai-field">
        <span class="field__label">API 密钥</span>
        <span class="textbox">
          <svg class="icon icon--s"><use href="#i-key"/></svg>
          <input type="password" data-ai-key autocomplete="off" spellcheck="false"
                 placeholder="${cfg.hasKey ? `已保存 ${escapeHtml(cfg.keyMask)}，留空表示不改` : "sk-…"}">
        </span>
      </label>
      <div class="ai-actions">
        <button class="btn btn--sm" type="button" data-ai-test>
          <svg class="icon icon--s"><use href="#i-refresh"/></svg>测试连接
        </button>
        <button class="btn btn--subtle btn--sm" type="button" data-ai-clear ${cfg.hasKey ? "" : "hidden"}>清除密钥</button>
        <span class="ai-status" data-ai-status></span>
      </div>
    </div>

    <div class="setting-row setting-row--stack">
      <label class="field ai-field">
        <span class="field__label">模型名称</span>
        <span class="selectbox" data-ai-model-wrap ${useCustomModel ? "hidden" : ""}>
          <select data-ai-model>
            ${state.models
              .map(
                (name) =>
                  H`<option value="${escapeHtml(name)}" ${name === model ? "selected" : ""}>${escapeHtml(name)}</option>`
              )
              .join("")}
            <option value="${CUSTOM_MODEL}">自定义…</option>
          </select>
          <svg class="icon icon--s"><use href="#i-chevron-down"/></svg>
        </span>
        <span class="textbox" data-ai-model-custom ${useCustomModel ? "" : "hidden"}>
          <input data-ai-model-input placeholder="模型 ID，例如 gpt-4o-mini / deepseek-chat"
                 spellcheck="false" value="${escapeHtml(model)}">
        </span>
        <span class="field__hint" data-ai-model-hint>${
          state.models.length
            ? t("点「测试连接」可以刷新可选模型")
            : t("填好地址和密钥后点「测试连接」，会自动拉取模型列表")
        }</span>
      </label>
    </div>

    <div class="setting-row setting-row--stack">
      <div class="setting-row__main">
        <div class="setting-row__label">高级配置</div>
        <div class="setting-row__desc">按服务商能力勾选；勾了不支持的能力，接口会直接报错</div>
      </div>
      <div class="ai-switches">
        <label class="ai-line">
          <span class="switch">
            <input type="checkbox" data-ai-flag="tools" ${cfg.tools ? "checked" : ""}>
            <span class="switch__track"><span class="switch__knob"></span></span>
          </span>
          <span class="ai-line__text"><b>工具调用</b><small>允许 CDP娘 翻目录、搜文件、读文本（只读；写权限要单独勾；轮次上限在下面一行调）</small></span>
        </label>
        <label class="ai-line">
          <span class="switch">
            <input type="checkbox" data-ai-flag="vision" ${cfg.vision ? "checked" : ""}>
            <span class="switch__track"><span class="switch__knob"></span></span>
          </span>
          <span class="ai-line__text"><b>图片输入</b><small>多模态模型才能勾；勾上后对话框出现发图按钮</small></span>
        </label>
        <label class="ai-line">
          <span class="switch">
            <input type="checkbox" data-ai-flag="thinking" ${cfg.thinking ? "checked" : ""}>
            <span class="switch__track"><span class="switch__knob"></span></span>
          </span>
          <span class="ai-line__text"><b>思考模式</b><small>OpenAI 系会带 reasoning_effort，Anthropic 走 thinking 预算</small></span>
        </label>
        <label class="ai-line">
          <span class="switch">
            <input type="checkbox" data-ai-protocol-toggle ${cfg.protocol && cfg.protocol !== "auto" ? "checked" : ""}>
            <span class="switch__track"><span class="switch__knob"></span></span>
          </span>
          <span class="ai-line__text"><b>自定义协议</b><small>地址不像标准 OpenAI / Anthropic 时手动指定</small></span>
        </label>
        <div class="ai-protocol" data-ai-protocol ${cfg.protocol && cfg.protocol !== "auto" ? "" : "hidden"}>
          <label class="ai-radio">
            <input type="radio" name="ai-protocol" value="openai" ${cfg.protocol !== "anthropic" ? "checked" : ""}>
            <span>OpenAI 兼容（/chat/completions）</span>
          </label>
          <label class="ai-radio">
            <input type="radio" name="ai-protocol" value="anthropic" ${cfg.protocol === "anthropic" ? "checked" : ""}>
            <span>Anthropic（/messages）</span>
          </label>
        </div>
      </div>
    </div>

    <div class="setting-row setting-row--stack">
      <div class="setting-row__main">
        <div class="setting-row__label">工具轮次上限</div>
        <div class="setting-row__desc">一次对话里模型最多来回调几轮工具；到上限会收回工具，让它基于已有结果收尾</div>
      </div>
      <div class="ai-tokens">
        <div class="ai-chips" data-ai-rounds>
          ${[3, 5, 8, 12, 20]
            .map(
              (n) =>
                H`<button type="button" class="ai-chip ${Number(cfg.maxRounds) === n ? "is-active" : ""}" data-ai-round="${n}">${t("{n} 轮", { n })}</button>`
            )
            .join("")}
          <span class="ai-token-custom">
            <input type="number" min="1" max="50" step="1" placeholder="1-50"
                   data-ai-rounds-input
                   value="${[3, 5, 8, 12, 20].includes(Number(cfg.maxRounds)) ? "" : Number(cfg.maxRounds || 8)}">
          </span>
        </div>
      </div>
    </div>

    <div class="setting-row setting-row--stack ai-perms">
      <div class="setting-row__main">
        <div class="setting-row__label">数据操作权限<span class="chip ai-perms__flag">需授权</span></div>
        <div class="setting-row__desc">
          不勾选时CDP娘只能看，不能改。勾选后她可以真的动你的文件，每一次操作都会在对话里列出来并记进审计日志
        </div>
      </div>
      <div class="ai-switches">
        <label class="ai-line">
          <span class="switch">
            <input type="checkbox" data-ai-flag="writeFiles" ${cfg.writeFiles ? "checked" : ""}>
            <span class="switch__track"><span class="switch__knob"></span></span>
          </span>
          <span class="ai-line__text">
            <b>新建 / 编辑 文件与文件夹</b>
            <small>建目录、写文本、改名；覆盖已有文本文件前会先说明</small>
          </span>
        </label>
        <label class="ai-line">
          <span class="switch">
            <input type="checkbox" data-ai-flag="moveFiles" ${cfg.moveFiles ? "checked" : ""}>
            <span class="switch__track"><span class="switch__knob"></span></span>
          </span>
          <span class="ai-line__text">
            <b>复制 / 移动 文件与文件夹</b>
            <small>在网盘内搬运或复制内容，不会删除任何东西</small>
          </span>
        </label>
      </div>
    </div>

    ${tokenRow("maxInput", t("输入上限"), t("历史消息超过这个长度会从最旧的开始丢掉"), cfg.maxInput)}
    ${tokenRow("maxOutput", t("输出上限"), t("单次回答最多生成多少 token"), cfg.maxOutput)}

    ${
      state.isAdmin
        ? H`<div class="setting-row setting-row--stack ai-prompt">
            <div class="setting-row__main">
              <div class="setting-row__label">系统提示词<span class="chip chip--accent">管理员</span></div>
              <div class="setting-row__desc">决定 CDP娘 的人设和说话方式，全站生效；留空则用内置默认（中英各一版，跟随界面语言）</div>
            </div>
            <textarea class="ai-textarea" data-ai-prompt rows="6" maxlength="8000"
                      placeholder="留空使用默认提示词">${escapeHtml(state.prompt)}</textarea>
            <div class="ai-actions">
              <button class="btn btn--sm" type="button" data-ai-prompt-save>保存提示词</button>
              <button class="btn btn--subtle btn--sm" type="button" data-ai-prompt-default>填入默认</button>
              <span class="ai-status" data-ai-prompt-status></span>
            </div>
          </div>`
        : ""
    }

    <div class="setting-row ai-save">
      <div class="setting-row__main">
        <div class="setting-row__label">保存配置</div>
        <div class="setting-row__desc">改一项就即时存一项；想确认整块配置都落库了，点这里一次性提交</div>
      </div>
      <div class="ai-actions">
        <span class="ai-status" data-ai-save-status></span>
        <button class="btn btn--accent" type="button" data-ai-save>保存配置</button>
      </div>
    </div>
  `;
}

/* ------------------------------------------------------------------ */
/* 挂载                                                               */
/* ------------------------------------------------------------------ */
export async function mountAiSettings(card) {
  const body = card.querySelector("[data-ai-body]");
  if (!body) return;
  state.card = card;

  try {
    // 带上当前界面语言：内置系统提示词要按它取中文版 / 英文版
    const data = await api.get(`/api/ai/config?lang=${encodeURIComponent(getLang())}`);
    state.cfg = data.config || {};
    state.isAdmin = Boolean(data.isAdmin);
    state.prompt = data.systemPrompt || "";
    state.defaultPrompt = data.defaultPrompt || "";
  } catch {
    body.innerHTML = `<div class="settings-card__note">${t("配置读取失败，刷新页面再试")}</div>`;
    return;
  }

  body.innerHTML = configMarkup();
  bind(body);
}

function bind(body) {
  const base = body.querySelector("[data-ai-base]");
  const key = body.querySelector("[data-ai-key]");
  const select = body.querySelector("[data-ai-model]");
  const modelInput = body.querySelector("[data-ai-model-input]");
  const status = body.querySelector("[data-ai-status]");

  /* --- 预设 --- */
  body.querySelectorAll("[data-ai-preset]").forEach((button) => {
    button.addEventListener("click", () => {
      const preset = state.cfg?.presets?.[button.dataset.aiPreset];
      const url = preset?.base_url;
      if (!url) return;
      base.value = url;
      save({ baseUrl: url, provider: button.dataset.aiPreset });
      Toast.info(`已填入 ${preset.label} 的地址`);
    });
  });

  /* --- 地址 / 密钥：输入停止 800ms 再存 --- */
  const queueSave = (patch) => {
    window.clearTimeout(state.saveTimer);
    state.saveTimer = window.setTimeout(() => save(patch()), 800);
  };

  base.addEventListener("input", () => queueSave(() => ({ baseUrl: base.value.trim(), provider: "custom" })));
  key.addEventListener("input", () => {
    state.keyDirty = true;
    queueSave(() => ({ key: key.value }));
  });

  body.querySelector("[data-ai-clear]")?.addEventListener("click", async () => {
    key.value = "";
    state.keyDirty = true;
    await save({ key: "" });
    Toast.info("密钥已清除");
  });

  /* --- 测试连接 --- */
  const test = body.querySelector("[data-ai-test]");

  async function runProbe({ silent = false } = {}) {
    const url = base.value.trim();
    if (!url) {
      if (!silent) Toast.warn("先填 API 地址");
      return;
    }
    if (!key.value.trim() && !state.cfg?.hasKey) {
      if (!silent) Toast.warn("先填 API 密钥");
      return;
    }
    test.disabled = true;
    setStatus(status, "正在连接…", "");
    try {
      const result = await api.post("/api/ai/probe", {
        baseUrl: url,
        key: key.value.trim(),
        protocol: currentProtocol(),
      });
      state.models = result.models || [];
      syncModelsToSelect(result.models || [], state.cfg?.model || "");
      setStatus(
        status,
        `${result.message}${result.latency ? ` · ${result.latency}ms` : ""}`,
        "ok"
      );
      if (!state.cfg?.model && state.models.length) {
        const first = state.models[0];
        if (select) select.value = first;
        if (modelInput) modelInput.value = first;
        save({ model: first });
      }
      if (!silent) Toast.success("连接成功");
    } catch (err) {
      setStatus(status, err.message || "连接失败", "bad");
      if (silent) Toast.warn(`自动检测模型失败：${err.message || "连接失败"}`);
    } finally {
      test.disabled = false;
    }
  }

  // 密钥存好之后自动探一次模型列表，省得用户还要手动点「测试连接」
  state.autoProbe = () => runProbe({ silent: true });
  test.addEventListener("click", () => runProbe());

  /* --- 模型下拉 / 自定义 --- */
  select?.addEventListener("change", () => {
    if (select.value === CUSTOM_MODEL) {
      body.querySelector("[data-ai-model-wrap]").hidden = true;
      body.querySelector("[data-ai-model-custom]").hidden = false;
      modelInput.value = state.cfg?.model || "";
      modelInput.focus();
      return;
    }
    save({ model: select.value });
  });

  modelInput?.addEventListener("change", () => {
    const value = modelInput.value.trim();
    if (value) save({ model: value });
  });

  /* --- 四个开关 --- */
  body.querySelectorAll("[data-ai-flag]").forEach((input) => {
    input.addEventListener("change", () => {
      save({ [input.dataset.aiFlag]: input.checked });
    });
  });

  const protocolToggle = body.querySelector("[data-ai-protocol-toggle]");
  const protocolBox = body.querySelector("[data-ai-protocol]");
  protocolToggle?.addEventListener("change", () => {
    protocolBox.hidden = !protocolToggle.checked;
    if (!protocolToggle.checked) {
      save({ protocol: "auto" });
      return;
    }
    save({ protocol: currentProtocol() });
  });
  body.querySelectorAll('input[name="ai-protocol"]').forEach((input) => {
    input.addEventListener("change", () => {
      if (input.checked) save({ protocol: input.value });
    });
  });

  /* --- 输入 / 输出上限 --- */
  ["maxInput", "maxOutput"].forEach((field) => {
    const wrap = body.querySelector(`[data-ai-tokens="${field}"]`);
    if (!wrap) return;
    const useDefault = wrap.querySelector(`[data-ai-default="${field}"]`);
    const chips = wrap.querySelector(".ai-chips");

    useDefault.addEventListener("change", () => {
      chips.hidden = useDefault.checked;
      if (useDefault.checked) save({ [field]: 0 });
    });

    wrap.querySelectorAll("[data-ai-token]").forEach((chip) => {
      chip.addEventListener("click", () => {
        save({ [field]: Number(chip.dataset.value) });
        wrap.querySelectorAll("[data-ai-token]").forEach((c) => c.classList.toggle("is-active", c === chip));
        const custom = wrap.querySelector(`[data-ai-token-input="${field}"]`);
        if (custom) custom.value = "";
      });
    });

    wrap.querySelector(`[data-ai-token-input="${field}"]`)?.addEventListener("change", (event) => {
      const raw = Number(event.target.value);
      if (!Number.isFinite(raw) || raw <= 0) {
        Toast.warn("填一个大于 0 的数字，或者勾「使用默认」");
        return;
      }
      const value = Math.max(1024, Math.round(raw));
      useDefault.checked = false;
      chips.hidden = false;
      wrap.querySelectorAll("[data-ai-token]").forEach((c) => c.classList.remove("is-active"));
      save({ [field]: value });
    });
  });

  /* --- 工具轮次上限 --- */
  const roundsWrap = body.querySelector("[data-ai-rounds]");
  const roundsInput = body.querySelector("[data-ai-rounds-input]");
  roundsWrap?.querySelectorAll("[data-ai-round]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const value = Number(chip.dataset.aiRound);
      roundsWrap.querySelectorAll("[data-ai-round]").forEach((c) => c.classList.toggle("is-active", c === chip));
      if (roundsInput) roundsInput.value = "";
      save({ maxRounds: value });
    });
  });
  roundsInput?.addEventListener("change", () => {
    const raw = Number(roundsInput.value);
    if (!Number.isFinite(raw) || raw < 1) {
      Toast.warn("填 1–50 之间的轮次");
      roundsInput.value = "";
      return;
    }
    const value = Math.max(1, Math.min(50, Math.round(raw)));
    roundsInput.value = String(value);
    roundsWrap.querySelectorAll("[data-ai-round]").forEach((c) => c.classList.remove("is-active"));
    save({ maxRounds: value });
  });

  /* --- 管理员：系统提示词 --- */
  body.querySelector("[data-ai-prompt-save]")?.addEventListener("click", async (event) => {
    const area = body.querySelector("[data-ai-prompt]");
    const tip = body.querySelector("[data-ai-prompt-status]");
    // currentTarget 在 await 之后就没了，先把按钮存下来
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api.put("/api/ai/prompt", { prompt: area.value });
      state.prompt = result.prompt || "";
      setStatus(tip, "已保存", "ok");
    } catch (err) {
      setStatus(tip, err.message || "保存失败", "bad");
    } finally {
      button.disabled = false;
    }
  });

  body.querySelector("[data-ai-prompt-default]")?.addEventListener("click", () => {
    const area = body.querySelector("[data-ai-prompt]");
    if (area && state.defaultPrompt) {
      area.value = state.defaultPrompt;
      Toast.info("已填入默认提示词，记得点保存");
    }
  });

  /* --- 显式保存：把整块配置一次性提交 --- */
  const saveButton = body.querySelector("[data-ai-save]");
  const saveTip = body.querySelector("[data-ai-save-status]");

  function collectAll() {
    const patch = {
      baseUrl: base.value.trim(),
      protocol: currentProtocol(),
    };

    // 模型：下拉框看得见就用下拉框的值，切到「自定义」时用输入框
    const customBox = body.querySelector("[data-ai-model-custom]");
    const picked = customBox && !customBox.hidden ? modelInput?.value.trim() : select?.value;
    const model = picked === CUSTOM_MODEL ? modelInput?.value.trim() : picked;
    if (model) patch.model = model;

    body.querySelectorAll("[data-ai-flag]").forEach((input) => {
      patch[input.dataset.aiFlag] = input.checked;
    });

    ["maxInput", "maxOutput"].forEach((field) => {
      const wrap = body.querySelector(`[data-ai-tokens="${field}"]`);
      if (!wrap) return;
      if (wrap.querySelector(`[data-ai-default="${field}"]`).checked) {
        patch[field] = 0; // 0 = 用模型提供商默认值
        return;
      }
      const chip = wrap.querySelector("[data-ai-token].is-active");
      const custom = wrap.querySelector(`[data-ai-token-input="${field}"]`);
      const raw = custom?.value ? Number(custom.value) : chip ? Number(chip.dataset.value) : 0;
      patch[field] = Number.isFinite(raw) && raw > 0 ? Math.max(1024, Math.round(raw)) : 0;
    });

    const chipRounds = roundsWrap?.querySelector("[data-ai-round].is-active");
    const rawRounds = roundsInput?.value
      ? Number(roundsInput.value)
      : chipRounds
        ? Number(chipRounds.dataset.aiRound)
        : 8;
    patch.maxRounds = Math.min(50, Math.max(1, Math.round(rawRounds || 8)));

    // 密钥留空 = 不改（后端也是这个语义），只有真填了才提交
    const keyValue = key.value.trim();
    if (keyValue) patch.key = keyValue;
    return patch;
  }

  saveButton?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    setStatus(saveTip, "正在保存…", "");
    const ok = await save(collectAll(), { quiet: true });
    button.disabled = false;
    if (ok) {
      const time = new Date().toLocaleTimeString("zh-CN", { hour12: false });
      setStatus(saveTip, `已保存 ${time}`, "ok");
      Toast.success("配置已保存");
    } else {
      setStatus(saveTip, "保存失败", "bad");
    }
  });

  function currentProtocol() {
    if (!protocolToggle?.checked) return "auto";
    return body.querySelector('input[name="ai-protocol"]:checked')?.value || "openai";
  }
}

function setStatus(node, text, tone) {
  if (!node) return;
  node.textContent = text;
  node.dataset.state = tone || "";
}

function syncModelsToSelect(models, current) {
  const select = document.querySelector("[data-ai-model]");
  if (!select) return;
  const list = models.slice();
  if (current && !list.includes(current)) list.unshift(current);
  select.innerHTML =
    list
      .map((name) => H`<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`)
      .join("") + `<option value="${CUSTOM_MODEL}">自定义…</option>`;
  if (current) select.value = current;
  const wrap = document.querySelector("[data-ai-model-wrap]");
  const custom = document.querySelector("[data-ai-model-custom]");
  if (list.length) {
    if (wrap) wrap.hidden = false;
    if (custom) custom.hidden = true;
  }
}

/* ------------------------------------------------------------------ */
/* 保存                                                               */
/* ------------------------------------------------------------------ */
async function save(patch, { quiet = false } = {}) {
  window.clearTimeout(state.saveTimer);
  try {
    const result = await api.put("/api/ai/config", patch);
    state.cfg = result.config || state.cfg;
    if (patch.key !== undefined) {
      state.keyDirty = false;
      const input = document.querySelector("[data-ai-key]");
      if (input && state.cfg?.hasKey) {
        input.placeholder = `已保存 ${state.cfg.keyMask}，留空表示不改`;
      }
      const clear = document.querySelector("[data-ai-clear]");
      if (clear) clear.hidden = !state.cfg?.hasKey;
      // 刚填完密钥：顺手把模型列表拉回来（失败只提示，不打扰）
      if (state.cfg?.hasKey && state.cfg?.baseUrl && !state.models.length) {
        window.setTimeout(() => state.autoProbe?.(), 400);
      }
    }
    const base = document.querySelector("[data-ai-base]");
    if (base && patch.baseUrl !== undefined) base.value = state.cfg?.baseUrl || base.value;
    return true;
  } catch (err) {
    // 显式保存自己会显示失败原因，这里就不再弹一次
    if (!quiet) Toast.error(err.message || "保存失败");
    return false;
  }
}
