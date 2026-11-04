/**
 * 分享链接：创建对话框 + 结果展示。
 * 只用页内 Dialog，不碰原生 prompt。
 */
import { api, escapeHtml } from "../api.js";
import { Dialog } from "./dialog.js";
import { Toast } from "./toast.js";
import { siteOrigin } from "../store.js";

const DAY_OPTIONS = [
  { label: "1 天", value: "1" },
  { label: "7 天", value: "7" },
  { label: "30 天", value: "30" },
  { label: "永久有效", value: "" },
];

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // 非 https / 无剪贴板权限时退化为隐藏 textarea
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch {
      return false;
    }
  }
}

const SHARE_FIELDS = [
  {
    name: "days",
    label: "有效期",
    type: "select",
    value: "7",
    options: DAY_OPTIONS,
  },
  {
    name: "code",
    label: "提取码（可选）",
    placeholder: "留空则凭链接直接访问",
    validate: (v) => {
      const t = v.trim();
      if (t && !/^[A-Za-z0-9]{1,8}$/.test(t)) return "只能填 1–8 位字母或数字";
      return null;
    },
  },
  {
    name: "max",
    label: "下载次数上限（可选）",
    placeholder: "留空表示不限次数",
    validate: (v) => {
      const t = v.trim();
      if (t && !/^[1-9]\d{0,5}$/.test(t)) return "请填 1–999999 之间的整数";
      return null;
    },
  },
];

/**
 * 创建分享（单个或多个）。
 * @param {Array<{id:number,name:string,type:string}>} items 至少一项
 * @returns {Promise<object|null>} 创建成功的 share 数据
 */
async function createShare(items) {
  const single = items.length === 1 ? items[0] : null;
  const total = items.length;

  const values = await Dialog.form({
    title: single ? `分享「${single.name}」` : `分享 ${total} 个项目`,
    message: single
      ? single.type === "folder"
        ? "任何人打开链接都能浏览这个文件夹里的内容，你可以随时取消分享。"
        : "任何人打开链接都能在线预览或下载这个文件，你可以随时取消分享。"
      : `这 ${total} 个项目会合并成一条链接，打开后是一个列表，可逐个预览或下载。`,
    confirmText: "创建链接",
    fields: SHARE_FIELDS,
  });
  if (!values) return null;

  const code = values.code.trim().toUpperCase();
  let payload;
  try {
    payload = await api.post("/api/shares", {
      nodeIds: items.map((item) => item.id),
      expiresDays: values.days ? Number(values.days) : null,
      extractCode: code || null,
      maxDownloads: values.max.trim() ? Number(values.max.trim()) : null,
    });
  } catch (err) {
    Toast.error(err.message);
    return null;
  }

  const share = payload.share;
  const url = `${siteOrigin()}${share.url}`;
  const copied = await copyText(url);
  Toast.success(copied ? "链接已复制到剪贴板" : "分享链接已创建");

  await Dialog.alert({
    title: total > 1 ? `已创建合集分享（${total} 项）` : "分享链接已创建",
    message: `
      <div class="share-result">
        <div class="share-result__row">
          <span class="share-result__label">链接</span>
          <code class="share-result__url">${escapeHtml(url)}</code>
        </div>
        ${
          code
            ? `<div class="share-result__row">
                 <span class="share-result__label">提取码</span>
                 <b class="share-result__code">${escapeHtml(code)}</b>
               </div>`
            : ""
        }
        <div class="share-result__hint">
          ${copied ? "链接已自动复制。" : "自动复制被浏览器拦截，请手动选中上面的链接复制。"}
          ${
            share.expiresAt
              ? `有效期至 ${new Date(share.expiresAt).toLocaleString("zh-CN")}。`
              : "永久有效。"
          }
          ${share.maxDownloads ? `最多下载 ${share.maxDownloads} 次。` : ""}
        </div>
      </div>`,
  });

  return share;
}

/** 单个文件/文件夹：弹出创建对话框并提交。 */
export function shareItem(item) {
  return createShare([item]);
}

/** 批量分享：多项合并成一条合集链接。 */
export function shareItems(items) {
  return createShare(items);
}
