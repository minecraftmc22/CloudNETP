/**
 * 花瓣飘落层：单 canvas、≤24 粒、页面隐藏即暂停
 */
const COLORS = ["#ffd0e6", "#ffb3da", "#f9a8d4", "#e6d4ff", "#ffe3f1"];
const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)");

let canvas = null;
let ctx = null;
let rafId = 0;
let petals = [];
let width = 0;
let height = 0;
let last = 0;

function spawn(initial = false) {
  const size = 5 + Math.random() * 7;
  return {
    x: Math.random() * width,
    y: initial ? Math.random() * height : -20 - Math.random() * 60,
    r: size,
    rot: Math.random() * Math.PI * 2,
    vr: (Math.random() - 0.5) * 0.018,
    vy: 18 + Math.random() * 26,
    vx: -10 + Math.random() * 20,
    sway: Math.random() * Math.PI * 2,
    swaySpeed: 0.5 + Math.random() * 0.8,
    alpha: 0.35 + Math.random() * 0.45,
    color: COLORS[(Math.random() * COLORS.length) | 0],
  };
}

function resize() {
  if (!canvas) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  width = window.innerWidth;
  height = window.innerHeight;
  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const count = Math.min(24, Math.max(10, Math.round(width / 78)));
  petals = Array.from({ length: count }, () => spawn(true));
}

function drawPetal(p) {
  const w = p.r * 0.62;
  ctx.save();
  ctx.translate(p.x, p.y);
  ctx.rotate(p.rot);
  ctx.globalAlpha = p.alpha;
  ctx.fillStyle = p.color;
  ctx.beginPath();
  ctx.moveTo(0, -p.r);
  ctx.bezierCurveTo(w, -p.r * 0.62, w * 0.86, p.r * 0.5, 0, p.r);
  ctx.bezierCurveTo(-w * 0.86, p.r * 0.5, -w, -p.r * 0.62, 0, -p.r);
  ctx.fill();
  ctx.restore();
}

function frame(now) {
  rafId = 0;
  const dt = Math.min(0.05, (now - (last || now)) / 1000) || 0.016;
  last = now;

  ctx.clearRect(0, 0, width, height);
  for (let i = 0; i < petals.length; i += 1) {
    const p = petals[i];
    p.sway += p.swaySpeed * dt;
    p.x += (p.vx + Math.sin(p.sway) * 14) * dt;
    p.y += p.vy * dt;
    p.rot += p.vr;
    if (p.y - p.r > height + 24 || p.x < -40 || p.x > width + 40) {
      petals[i] = spawn(false);
    } else {
      drawPetal(p);
    }
  }
  rafId = window.requestAnimationFrame(frame);
}

export function startPetals() {
  if (REDUCED.matches) return;
  canvas = canvas || document.getElementById("petal-layer");
  if (!canvas || rafId) return;
  resize();
  window.addEventListener("resize", resize);
  document.addEventListener("visibilitychange", onVisibility);
  last = 0;
  rafId = window.requestAnimationFrame(frame);
}

function onVisibility() {
  if (document.hidden) stopPetals();
  else startPetals();
}

export function stopPetals() {
  if (rafId) {
    window.cancelAnimationFrame(rafId);
    rafId = 0;
  }
  window.removeEventListener("resize", resize);
  document.removeEventListener("visibilitychange", onVisibility);
  if (ctx) ctx.clearRect(0, 0, width, height);
}

export function setPetalsEnabled(enabled) {
  if (enabled) startPetals();
  else stopPetals();
}
