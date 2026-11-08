#!/usr/bin/env bash
# CloudNETP 一键启动（Linux / Ubuntu / macOS）
#
#   ./start.sh          生产启动：读 .env，不热重载
#   ./start.sh --dev    开发模式：监视 app/ 与 static/，改代码自动重启
#
# 首次运行会自动：建 .venv → 装依赖 → 从 .env.example 生成 .env
# → 补一个随机 SECRET_KEY。重复运行只会补缺失的部分。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

DEV=0
for arg in "$@"; do
  case "$arg" in
    --dev|-d) DEV=1 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$arg（可用 --dev）" >&2; exit 2 ;;
  esac
done

say() { printf '\033[36m[start]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 1. 找一个 Python 3.10+ ----------
PY=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
[ -n "$PY" ] || die "没找到 Python 3.10+。Ubuntu 上先装：sudo apt install -y python3 python3-venv python3-pip"
say "使用解释器：$PY（$("$PY" -c 'import platform; print(platform.python_version())' 2>/dev/null)）"

# ---------- 2. 虚拟环境 ----------
if [ ! -x .venv/bin/python ]; then
  say "创建虚拟环境 .venv"
  "$PY" -m venv .venv || die "创建虚拟环境失败。Ubuntu 需要 python3-venv：sudo apt install -y python3-venv"
fi
VPY="$ROOT/.venv/bin/python"

# ---------- 3. 依赖（仅 requirements.txt 变化时重装） ----------
STAMP=".venv/.requirements.sha256"
if command -v sha256sum >/dev/null 2>&1; then
  WANT="$(sha256sum requirements.txt | cut -d' ' -f1)"
else
  WANT="$("$VPY" -c 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path("requirements.txt").read_bytes()).hexdigest())')"
fi
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$WANT" ]; then
  INDEX="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
  say "安装依赖（源：$INDEX）"
  "$VPY" -m pip install --disable-pip-version-check -q -r requirements.txt -i "$INDEX" \
    || die "依赖安装失败。可换源重试：PIP_INDEX_URL=https://pypi.org/simple ./start.sh"
  printf '%s\n' "$WANT" > "$STAMP"
else
  say "依赖已是最新（跳过 pip install）"
fi

# ---------- 4. .env 与 SECRET_KEY ----------
"$VPY" scripts/bootstrap.py

# ---------- 5. 启动 ----------
if [ "$DEV" = "1" ]; then
  say "开发模式（热重载，监视 app/ 与 static/）"
  exec "$VPY" main.py
fi
say "启动 CloudNETP"
exec "$VPY" run.py
