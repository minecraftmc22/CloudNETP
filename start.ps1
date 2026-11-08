# CloudNETP 一键启动（Windows PowerShell）
#
#   .\start.ps1          生产启动：读 .env，不热重载
#   .\start.ps1 -Dev     开发模式：监视 app/ 与 static/，改代码自动重启
#
# 首次运行会自动：建 .venv → 装依赖 → 从 .env.example 生成 .env
# → 补一个随机 SECRET_KEY。重复运行只会补缺失的部分。
#
# 如果提示「无法加载文件，因为在此系统上禁止运行脚本」，用：
#   powershell -ExecutionPolicy Bypass -File .\start.ps1

param(
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
# 中文提示统一按 UTF-8 输出：Windows Terminal / 管道捕获都不会乱码
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Say($message) { Write-Host "[start] $message" -ForegroundColor Cyan }
function Die($message) { Write-Host "[错误] $message" -ForegroundColor Red; exit 1 }

# ---------- 1. 找一个 Python 3.10+ ----------
function Test-Python([string]$exe, [string[]]$prefix) {
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { return $false }
    $code = "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
    $nativeArgs = @($prefix) + @("-c", $code)
    & $exe $nativeArgs | Out-Null
    return ($LASTEXITCODE -eq 0)
}

$python = $null
$pythonPrefix = @()
$candidates = @(
    @{ Exe = "py";      Prefix = @("-3") },
    @{ Exe = "python";  Prefix = @() },
    @{ Exe = "python3"; Prefix = @() }
)
foreach ($item in $candidates) {
    if (Test-Python $item.Exe $item.Prefix) {
        $python = $item.Exe
        $pythonPrefix = $item.Prefix
        break
    }
}
if (-not $python) {
    Die "没找到 Python 3.10+。请先安装 Python（安装时勾选 Add python.exe to PATH），或执行 winget install Python.Python.3.13"
}
$version = (& $python @(@($pythonPrefix) + @("-c", "import platform; print(platform.python_version())"))).Trim()
Say "使用解释器：$python $version"

# ---------- 2. 虚拟环境 ----------
$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Say "创建虚拟环境 .venv"
    & $python @(@($pythonPrefix) + @("-m", "venv", ".venv"))
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        Die "创建虚拟环境失败。可先删除残留的 .venv 目录再试"
    }
}

# ---------- 3. 依赖（仅 requirements.txt 变化时重装） ----------
$stamp = Join-Path $Root ".venv\.requirements.sha256"
$want = (Get-FileHash -Algorithm SHA256 (Join-Path $Root "requirements.txt")).Hash.ToLower()
$have = ""
if (Test-Path $stamp) { $have = (Get-Content $stamp -Raw).Trim() }
if ($have -ne $want) {
    $index = $env:PIP_INDEX_URL
    if (-not $index) { $index = "https://mirrors.aliyun.com/pypi/simple/" }
    Say "安装依赖（源：$index）"
    & $venvPython -m pip install --disable-pip-version-check -q -r requirements.txt -i $index
    if ($LASTEXITCODE -ne 0) {
        Die "依赖安装失败。可换源重试：`$env:PIP_INDEX_URL='https://pypi.org/simple'; .\start.ps1"
    }
    Set-Content -Path $stamp -Value $want -NoNewline -Encoding ascii
} else {
    Say "依赖已是最新（跳过 pip install）"
}

# ---------- 4. .env 与 SECRET_KEY ----------
& $venvPython (Join-Path $Root "scripts\bootstrap.py")
if ($LASTEXITCODE -ne 0) { Die "初始化 .env 失败" }

# ---------- 5. 启动 ----------
if ($Dev) {
    Say "开发模式（热重载，监视 app/ 与 static/）"
    & $venvPython (Join-Path $Root "main.py")
} else {
    Say "启动 CloudNETP"
    & $venvPython (Join-Path $Root "run.py")
}
exit $LASTEXITCODE
