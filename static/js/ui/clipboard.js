/**
 * 复制文本到剪贴板。
 *
 * `navigator.clipboard` 只在安全上下文（https 或 localhost）里存在：
 * 私有部署常跑在 http://内网IP，直接写 `navigator.clipboard.writeText()` 会抛
 * TypeError（读 undefined 的属性），用户看到的就是「点了复制没反应」。
 * 这里统一兜一层：异步剪贴板 → 隐藏 textarea + execCommand。
 *
 * 返回是否真的复制成功，调用方据此给成功 / 失败的 Toast。
 */
export async function copyText(text) {
  const value = String(text ?? "");
  if (!value) return false;

  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch {
      /* 权限被拒 / 文档未聚焦：继续走兜底 */
    }
  }
  return legacyCopy(value);
}

/** 老路子：临时 textarea + execCommand("copy")，无权限弹窗也能过 */
function legacyCopy(value) {
  let area = null;
  const selection = document.getSelection();
  const previous = selection && selection.rangeCount ? selection.getRangeAt(0) : null;
  try {
    area = document.createElement("textarea");
    area.value = value;
    area.setAttribute("readonly", "");
    // 不能 display:none / visibility:hidden，那样选不中也就复制不了
    area.style.cssText =
      "position:fixed;top:0;left:-9999px;width:1px;height:1px;opacity:0;pointer-events:none";
    document.body.appendChild(area);
    area.select();
    area.setSelectionRange(0, value.length);
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    area?.remove();
    // 复制完了把用户原来的选区还回去，别顺手把人选中的东西清掉
    if (previous && selection) {
      selection.removeAllRanges();
      selection.addRange(previous);
    }
  }
}
