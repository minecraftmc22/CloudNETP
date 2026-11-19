/**
 * 通行密钥（WebAuthn）前端封装。
 *
 * 浏览器只认 ArrayBuffer，服务端（py_webauthn）只认 base64url 字符串，
 * 转换集中在这里，调用方不用关心 padding 与 URL-safe 字符。
 *
 * 可用条件：安全上下文（HTTPS 或 localhost）+ 浏览器支持 PublicKeyCredential。
 * http://局域网IP 下 navigator.credentials 是 undefined，必须提前检测并提示。
 */

const B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

export function supported() {
  return Boolean(
    window.isSecureContext && window.PublicKeyCredential && navigator.credentials?.create
  );
}

/** ArrayBuffer → base64url（无填充） */
export function b64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let out = "";
  let i = 0;
  for (; i + 2 < bytes.length; i += 3) {
    const n = (bytes[i] << 16) | (bytes[i + 1] << 8) | bytes[i + 2];
    out += B64_ALPHABET[(n >> 18) & 63] + B64_ALPHABET[(n >> 12) & 63];
    out += B64_ALPHABET[(n >> 6) & 63] + B64_ALPHABET[n & 63];
  }
  if (i < bytes.length) {
    const rem = bytes.length - i;
    const n = (bytes[i] << 16) | (rem === 2 ? bytes[i + 1] << 8 : 0);
    out += B64_ALPHABET[(n >> 18) & 63] + B64_ALPHABET[(n >> 12) & 63];
    if (rem === 2) out += B64_ALPHABET[(n >> 6) & 63];
  }
  return out;
}

/** base64url → Uint8Array（容错标准的 + / 与缺失的 padding） */
export function toBytes(text) {
  const raw = String(text || "").replace(/-/g, "+").replace(/_/g, "/");
  const padded = raw + "=".repeat((4 - (raw.length % 4)) % 4);
  const bin = atob(padded);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function decodeCreationOptions(options) {
  return {
    ...options,
    challenge: toBytes(options.challenge),
    user: { ...options.user, id: toBytes(options.user.id) },
    excludeCredentials: (options.excludeCredentials || []).map((item) => ({
      ...item,
      id: toBytes(item.id),
    })),
  };
}

function decodeRequestOptions(options) {
  return {
    ...options,
    challenge: toBytes(options.challenge),
    allowCredentials: (options.allowCredentials || []).map((item) => ({
      ...item,
      id: toBytes(item.id),
    })),
  };
}

/** 弹系统认证器，返回可直接交给服务端的 JSON */
export async function createCredential(options) {
  const credential = await navigator.credentials.create({
    publicKey: decodeCreationOptions(options),
  });
  if (!credential) throw new Error("cancelled");
  const response = credential.response;
  const transports = response.getTransports ? response.getTransports() : [];
  return {
    id: credential.id,
    rawId: b64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: b64url(response.clientDataJSON),
      attestationObject: b64url(response.attestationObject),
      transports,
    },
  };
}

/** 弹系统认证器做断言（登录第二步） */
export async function getAssertion(options) {
  const credential = await navigator.credentials.get({
    publicKey: decodeRequestOptions(options),
  });
  if (!credential) throw new Error("cancelled");
  const response = credential.response;
  return {
    id: credential.id,
    rawId: b64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: b64url(response.clientDataJSON),
      authenticatorData: b64url(response.authenticatorData),
      signature: b64url(response.signature),
      userHandle: response.userHandle ? b64url(response.userHandle) : null,
    },
  };
}

/** 取消/超时/不支持 这几种都归一成一句人能看懂的话 */
export function describeError(err) {
  const name = err?.name || "";
  if (name === "NotAllowedError" || err?.message === "cancelled") {
    return "已取消，或等待超时";
  }
  if (name === "InvalidStateError") return "这个设备已经添加过了";
  if (name === "NotSupportedError") return "当前浏览器或设备不支持通行密钥";
  if (name === "SecurityError") return "通行密钥需要 HTTPS 访问（localhost 除外）";
  return err?.message || "通行密钥操作失败";
}
