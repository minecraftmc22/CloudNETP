/**
 * 密码规则与强度（前端唯一来源）。
 *
 * 规则与后端 app/security.py 的 password_problem() 一一对应，
 * 注册 / 找回密码 / 修改密码都从这里取，避免三处各写一套、放宽了其中一处。
 */

export const PASSWORD_MAX = 128;

/**
 * 返回第一条不满足的规则文案；全部满足返回 null。
 * 文案与后端保持一致，用户前后看到的提示不会变来变去。
 */
export function passwordProblem(password) {
  const value = String(password ?? "");
  if (value.length < 8) return "密码至少 8 位";
  if (value.length > PASSWORD_MAX) return `密码太长（上限 ${PASSWORD_MAX} 位）`;
  if (value.trim() !== value) return "密码首尾不能是空格";
  // eslint-disable-next-line no-control-regex
  if (/[^\x21-\x7e]/.test(value)) {
    return "密码只能用键盘上的英文、数字和符号，不支持中文、空格和 emoji";
  }
  if (!/\d/.test(value)) return "密码需要包含数字";
  if (!/[a-z]/.test(value)) return "密码需要包含小写字母";
  if (!/[^0-9]/.test(value)) return "密码不能是纯数字";
  return null;
}

/**
 * 密码强度：长度 + 字符种类，只做进度提示，不参与最终判定。
 * 返回 { level: 0-4, label, rules }。
 */
export function strengthOf(password) {
  const rules = {
    len: password.length >= 8,
    digit: /\d/.test(password),
    lower: /[a-z]/.test(password),
    uppercase: /[A-Z]/.test(password),
    symbol: /[^A-Za-z0-9]/.test(password),
    ascii: password.length > 0 && /^[\x21-\x7e]+$/.test(password),
  };
  let score = 0;
  if (rules.len) score += 1;
  if (password.length >= 12) score += 1;
  if (rules.lower && rules.uppercase) score += 1;
  else if (rules.lower) score += 0.5;
  if (rules.digit) score += 1;
  if (rules.symbol) score += 1;
  if (!rules.ascii) score = 0;

  const level = password ? Math.max(1, Math.min(4, Math.round((score / 5) * 4))) : 0;
  const label = ["", "弱", "一般", "较强", "很强"][level];
  return { level, label, rules };
}
