/**
 * 纯 JS 增量 SHA-256。
 *
 * 为什么要自己写：crypto.subtle.digest() 只能一次性吃下整个 ArrayBuffer，
 * 传 2GB 文件会把标签页内存打爆；这里按块喂数据，内存恒定。
 * 计算过程分片 yield 回主线程，避免大文件哈希时页面卡死。
 */

const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function compress(h, w, bytes, offset) {
  for (let i = 0; i < 16; i += 1) {
    const j = offset + i * 4;
    w[i] = (bytes[j] << 24) | (bytes[j + 1] << 16) | (bytes[j + 2] << 8) | bytes[j + 3];
  }
  for (let i = 16; i < 64; i += 1) {
    const x = w[i - 15];
    const y = w[i - 2];
    const s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
    const s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
    w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
  }

  let a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];

  for (let i = 0; i < 64; i += 1) {
    const s1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
    const ch = (e & f) ^ (~e & g);
    const t1 = (hh + s1 + ch + K[i] + w[i]) | 0;
    const s0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
    const maj = (a & b) ^ (a & c) ^ (b & c);
    const t2 = (s0 + maj) | 0;
    hh = g;
    g = f;
    f = e;
    e = (d + t1) | 0;
    d = c;
    c = b;
    b = a;
    a = (t1 + t2) | 0;
  }

  h[0] = (h[0] + a) | 0;
  h[1] = (h[1] + b) | 0;
  h[2] = (h[2] + c) | 0;
  h[3] = (h[3] + d) | 0;
  h[4] = (h[4] + e) | 0;
  h[5] = (h[5] + f) | 0;
  h[6] = (h[6] + g) | 0;
  h[7] = (h[7] + hh) | 0;
}

export class Sha256 {
  constructor() {
    this.h = new Uint32Array([
      0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    ]);
    this.w = new Uint32Array(64);
    this.buffer = new Uint8Array(64);
    this.bufLen = 0;
    this.length = 0;
    this.finished = false;
  }

  update(input) {
    if (this.finished) throw new Error("哈希已结束");
    return this._update(input);
  }

  _update(input) {
    const bytes = input instanceof Uint8Array ? input : new Uint8Array(input);
    this.length += bytes.length;
    let offset = 0;

    if (this.bufLen > 0) {
      const take = Math.min(64 - this.bufLen, bytes.length);
      this.buffer.set(bytes.subarray(0, take), this.bufLen);
      this.bufLen += take;
      offset = take;
      if (this.bufLen < 64) return this;
      compress(this.h, this.w, this.buffer, 0);
      this.bufLen = 0;
    }

    while (offset + 64 <= bytes.length) {
      compress(this.h, this.w, bytes, offset);
      offset += 64;
    }
    if (offset < bytes.length) {
      this.buffer.set(bytes.subarray(offset), 0);
      this.bufLen = bytes.length - offset;
    }
    return this;
  }

  digestHex() {
    if (this.finished) throw new Error("哈希已结束");
    this.finished = true;

    const bitLen = this.length * 8;
    const padLen = this.bufLen < 56 ? 56 - this.bufLen : 120 - this.bufLen;
    const tail = new Uint8Array(padLen + 8);
    tail[0] = 0x80;
    // 64 位长度：高 32 位在低地址（文件长度不会超过 2^53，直接拆）
    const high = Math.floor(bitLen / 0x100000000);
    const low = bitLen >>> 0;
    tail[padLen] = (high >>> 24) & 0xff;
    tail[padLen + 1] = (high >>> 16) & 0xff;
    tail[padLen + 2] = (high >>> 8) & 0xff;
    tail[padLen + 3] = high & 0xff;
    tail[padLen + 4] = (low >>> 24) & 0xff;
    tail[padLen + 5] = (low >>> 16) & 0xff;
    tail[padLen + 6] = (low >>> 8) & 0xff;
    tail[padLen + 7] = low & 0xff;

    const saved = this.length;
    this.length = 0; // _update 会累加，这里不影响已算好的 bitLen
    this._update(tail);
    this.length = saved;

    let hex = "";
    for (let i = 0; i < 8; i += 1) hex += (this.h[i] >>> 0).toString(16).padStart(8, "0");
    return hex;
  }
}

export function sha256Hex(bytes) {
  return new Sha256().update(bytes).digestHex();
}

/** 分片读取文件算哈希，onProgress 收到 0~1。signal 可中断。 */
export async function hashFile(file, { onProgress, signal, sliceSize = 4 * 1024 * 1024 } = {}) {
  const hasher = new Sha256();
  let offset = 0;
  if (file.size === 0) {
    onProgress?.(1);
    return hasher.digestHex();
  }
  while (offset < file.size) {
    if (signal?.aborted) throw new DOMException("已取消", "AbortError");
    const end = Math.min(offset + sliceSize, file.size);
    const buffer = await file.slice(offset, end).arrayBuffer();
    hasher.update(new Uint8Array(buffer));
    offset = end;
    onProgress?.(offset / file.size);
    // 让出主线程，保持按钮与动画可响应
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  return hasher.digestHex();
}
