---
title: 快速开始
order: 1
---

# 快速开始

## 环境要求

| 项目 | 要求 |
| --- | --- |
| Python | 3.10 及以上，推荐 3.13 / 3.14 |
| 磁盘 | 装依赖约 60 MB，文件存储按你实际使用的空间算 |
| 数据库 | 默认本地 SQLite，无需安装；要用 MySQL 8 就自己准备一个 |
| 端口 | 默认 `8000`，被占用时在 `.env` 里改 `PORT` |

## 一键启动（推荐）

仓库根目录自带启动脚本。首次运行会自动建 `.venv`、装依赖、从 `.env.example` 生成 `.env`，
并补一个随机的 `SECRET_KEY`；之后每次启动直接跑脚本即可。

::: tabs

@tab Windows

```powershell
.\start.ps1          # 生产启动：读 .env，不热重载
.\start.ps1 -Dev     # 开发模式：监视 app/ 与 static/，改代码自动重启
```

也可以直接双击 `start.bat`。若提示「在此系统上禁止运行脚本」，改用：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

@tab Linux / Ubuntu

```bash
chmod +x start.sh    # 只需一次
./start.sh           # 生产启动
./start.sh --dev     # 开发模式
```

Ubuntu 缺 `venv` 时先装：

```bash
sudo apt install -y python3 python3-venv python3-pip
```

:::

脚本默认用阿里云 PyPI 镜像，要换源：

```bash
# Linux
PIP_INDEX_URL=https://pypi.org/simple ./start.sh

# Windows PowerShell
$env:PIP_INDEX_URL = "https://pypi.org/simple"; .\start.ps1
```

## 手动启动

不用脚本时，注意**要用虚拟环境里的解释器** —— 直接 `python run.py` 是系统 Python，
没装依赖会报 `ModuleNotFoundError`。

```bash
python3 -m venv .venv

# Linux / Ubuntu
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/bootstrap.py   # 生成 .env + SECRET_KEY
.venv/bin/python run.py                 # 生产
.venv/bin/python main.py                # 开发（热重载）

# Windows（PowerShell / cmd）
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe scripts\bootstrap.py
.venv\Scripts\python.exe run.py
.venv\Scripts\python.exe main.py
```

::: warning 别用 `uvicorn app.main:app --reload` 直接起
这种启动方式**不读 `.env`**，会退回 uvicorn 自己的默认值 `127.0.0.1:8000`；而且
`--reload` 会监视整个项目根目录，上传文件写进 `storage/` 时也会触发重启。
要自定义地址请补 `--host 0.0.0.0 --port 8000`，或者干脆用 `run.py` / `main.py`。
:::

## 第一次访问

启动日志会打印实际监听地址与 WebDAV 地址，形如：

```text
CloudNETP 0.3.50  http://localhost:8000
WebDAV            http://localhost:8000/dav
```

- **本机**：<http://localhost:8000>
- **同局域网的其他设备**：`http://<本机局域网 IP>:8000`（如 `http://192.168.1.24:8000`）

::: warning `0.0.0.0` 只能监听，不能填进浏览器
它表示「本机所有网卡」，不是可路由的目的地址，Chrome 124+ 会直接拦掉。
:::

首次打开是登录页，切到「注册」建号即可：

<img src="/images/login.jpg" alt="登录页" width="860">

::: tip 第一个账号自动是管理员
第一个注册成功的账号 `role = 1`，登录后侧栏会出现「管理后台」入口，可以进后台改站点名、
配额、开关注册与回收站等。
:::

## 数据放在哪

默认都落在项目目录下：

```text
dev.db                  SQLite 数据库（换 MySQL 后可以删）
storage/
  blob/<aa>/<bb>/<sha>  文件本体，按内容哈希分片存放
  thumbs/               图片 / 视频缩略图
  avatars/              头像
  db/fallback.db        容灾用的 SQLite 副本
```

备份只要拷 `storage/` 加上数据库（`dev.db` 或 MySQL dump）。因为按内容寻址，同一个文件
传两次只占一份空间。

## 换成 MySQL

改 `.env` 里的 `DATABASE_URL` 后重启即可，启动时自动建表（`DB_AUTO_CREATE=true`）：

```dotenv
DATABASE_URL=mysql+pymysql://用户名:密码@主机:3306/库名?charset=utf8mb4
```

也可以先手动建表，参考 `sql/schema.sql`。

## 下一步

- 建好号之后：[文件管理](/guide/files.html) —— 先把东西传上来
- 想挂成本地盘符：[WebDAV 挂载](/guide/webdav.html)
- 想放到服务器上跑：[部署与运维](/guide/deploy.html)
