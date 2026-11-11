---
title: 快速开始
order: 1
---

# 快速开始

## 环境要求

| 项目 | 要求 |
| --- | --- |
| 系统 | Windows / Linux / macOS 都行；走 Docker 时宿主机不用装 Python |
| Python | 3.10 及以上，推荐 3.13 / 3.14（选 Docker 可以不装） |
| Docker | 可选，Docker Engine 24+ 且 `docker compose version` 有输出即可 |
| 磁盘 | 装依赖约 60 MB，文件存储按你实际使用的空间算；镜像本身约 300 MB |
| 数据库 | 默认本地 SQLite，无需安装；要用 MySQL 8 就自己准备一个（也可以用自带的 MySQL compose） |
| 端口 | 默认 `8000`（Docker 镜像的默认值是 `8123`），都在 `.env` 里改 `PORT` |

两种装法任选其一：**Docker**（服务器 / NAS 推荐，机器上不用装 Python）或**脚本 / 手动装 Python**。
两者的配置方式完全一样 —— 都只读项目根目录的 `.env`；Docker 用 `env_file` 把 `.env` 注入容器，
文件本身不会被打进镜像。

## 方式一：Docker 部署（推荐）

镜像里已经带好 Python 与全部依赖，数据只落在宿主机的 `./data`，升级就是重建镜像。

### 1. 准备配置

```bash
cp .env.example .env
python -c "import secrets;print(secrets.token_hex(32))"   # 生成一串随机值填进 SECRET_KEY
```

`.env` 里至少确认这三项：

| 变量 | 说明 |
| --- | --- |
| `SECRET_KEY` | 必填一串长随机值；留空则每次重启后所有登录会话失效 |
| `PORT` | 对外端口，compose 的端口映射跟着它走（`.env.example` 里是 `8000`；删掉这行就用镜像默认的 `8123`） |
| `SITE_URL` | 有域名 / 反向代理时填 `https://pan.example.com`，分享链接与 WebDAV 地址会用它 |

### 2. 起服务

```bash
docker compose up -d --build      # 首次会构建镜像
docker compose logs -f            # 看启动日志（含实际监听地址与 WebDAV 地址）
```

国内默认走阿里云 PyPI 镜像，海外构建可以换源：

```bash
PIP_INDEX_URL=https://pypi.org/simple docker compose build
```

### 3. 打开

`http://<主机 IP>:<PORT>`，本机就是 <http://localhost:8000>。**第一个注册的账号自动成为管理员**。
容器自带 healthcheck，`docker compose ps` 里状态显示 `healthy` 就表示接口已经能应答。

::: warning Linux 上先给 ./data 改属主
容器里进程是 uid `10001` 的非 root 用户。宿主机 `./data` 若是 root 建的，上传会报权限错误，
执行一次 `sudo chown -R 10001:10001 data` 即可；不想 chown 就在 `docker-compose.yml` 里打开
`user: "1000:1000"` 那两行，改成 `id -u` / `id -g` 的结果。
:::

### 4. 数据在哪

| 容器内路径 | 宿主机 | 内容 |
| --- | --- | --- |
| `/data/dev.db` | `./data/dev.db` | SQLite 数据库（接了 MySQL 后这里只剩容灾副本） |
| `/data/storage` | `./data/storage` | 上传的原始文件、缩略图、头像、背景图 |

镜像里已经把 `/app/dev.db` 与 `/app/storage` 软链接到 `/data`，所以 `.env` 里的
`sqlite:///./dev.db`、留空的 `STORAGE_DIR` 都不用为容器改。备份 = 拷走宿主机上的 `./data`。

### 5. 常用命令

| 目的 | 命令 |
| --- | --- |
| 启动 / 重建 | `docker compose up -d --build` |
| 看日志 | `docker compose logs -f` |
| 停掉（保留数据） | `docker compose down` |
| 改了 `.env` | 必须 `docker compose up -d` 重新创建容器；`restart` 不会重新读 `.env` |
| 升级 | `docker compose build --pull && docker compose up -d` |

### 6. 换成 MySQL

```bash
docker compose -f docker-compose.mysql.yml up -d --build
```

它会额外起一个 MySQL 8.4（数据在 `mysql-data` 卷里），并把 `DATABASE_URL` 指过去；
密码改 `.env` 的 `MYSQL_ROOT_PASSWORD` / `MYSQL_PASSWORD`。接外部 MySQL 就直接改 `.env` 的
`DATABASE_URL`，容器里访问宿主机用 `host.docker.internal`。

### 7. 或者直接用发布好的镜像

不想在本机构建也行。仓库每次打 tag 都会由 GitHub Actions 构建多架构镜像（amd64 + arm64）推到
Docker Hub，`.env`、`./data`、端口都和上面一样，只是不再 `build`：

```bash
docker compose -f docker-compose.hub.yml up -d                    # 跟随 latest
IMAGE_TAG=0.3.67 docker compose -f docker-compose.hub.yml up -d   # 固定版本
```

| 镜像 tag | 含义 |
| --- | --- |
| `latest` | 最近一次正式发布的 tag（预发布 tag 不会顶掉它） |
| `0.3.67` | 与 `app/config.py` 的 `VERSION` 一致；打 `v0.3.67` 时镜像名里不带 `v` |
| `0.3.67-beta` | 手动运行工作流时自定义的 tag |

升级就是换 tag 重新拉：`docker compose -f docker-compose.hub.yml pull` 后 `up -d`。

::: tip 还有这些和容器相关
反向代理、HTTPS、备份、镜像升级注意事项都收在[部署与运维 → 用 Docker 跑](/guide/deploy.md#用-docker-跑)。
:::

## 方式二：脚本一键启动（Python）

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

### 手动启动（不用脚本时）

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

::: tip Docker 部署时路径不一样
容器里的 `/data` 就是宿主机的 `./data`，所以备份对象是项目根目录下的
`data/dev.db` + `data/storage/`，项目目录里那些同名文件只是软链接。
:::

## 换成 MySQL

改 `.env` 里的 `DATABASE_URL` 后重启即可，启动时自动建表（`DB_AUTO_CREATE=true`）：

```dotenv
DATABASE_URL=mysql+pymysql://用户名:密码@主机:3306/库名?charset=utf8mb4
```

也可以先手动建表，参考 `sql/schema.sql`。用 Docker 的话，直接上
`docker compose -f docker-compose.mysql.yml up -d --build` 会连数据库一起起好。

## 下一步

- 建好号之后：[文件管理](/guide/files.md) —— 先把东西传上来
- 想挂成本地盘符：[WebDAV 挂载](/guide/webdav.md)
- 想放到服务器上跑：[部署与运维](/guide/deploy.md)
