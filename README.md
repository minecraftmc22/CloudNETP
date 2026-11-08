<p align="center">
  <img src="docs/logo.png" width="132" alt="CloudNETP">
</p>

<h1 align="center">CloudNETP</h1>

<p align="center">
  <b>自托管的二次元个人网盘</b> · WinUI 3 / Fluent Design 风格 · 文件只放在你自己的机器上
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white">
  <img alt="SQLAlchemy" src="https://img.shields.io/badge/SQLAlchemy-MySQL%208%20%2F%20SQLite-D71F00">
  <img alt="i18n" src="https://img.shields.io/badge/i18n-%E4%B8%AD%E6%96%87%20%2F%20English-f9a8d4">
  <img alt="License" src="https://img.shields.io/badge/License-PolyForm%20Noncommercial-c084fc">
</p>

Python 3.14 + FastAPI 后端，零构建原生 ES Module 前端；分片秒传、在线预览、分享链接、WebDAV、管理后台与看板娘 AI 助手一应俱全，多用户各占独立空间与配额。

## 界面预览

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/files.jpg" alt="文件管理"></td>
    <td width="50%"><img src="docs/screenshots/viewer-image.jpg" alt="图片预览"></td>
  </tr>
  <tr>
    <td><b>文件管理</b> · 网格 / 列表双视图，多选、拖拽上传、右键菜单</td>
    <td><b>在线预览</b> · 图片缩放拖动，音视频与文档就地打开</td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/viewer-markdown.jpg" alt="Markdown 预览"></td>
    <td><img src="docs/screenshots/settings.jpg" alt="设置"></td>
  </tr>
  <tr>
    <td><b>文档预览</b> · Markdown / Word / Excel / PPT / ODF / PDF 直接在浏览器里看</td>
    <td><b>外观设置</b> · 亮暗主题、主题色、预设壁纸、各模块不透明度</td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/admin.jpg" alt="管理后台"></td>
    <td><img src="docs/screenshots/share.jpg" alt="公开分享页"></td>
  </tr>
  <tr>
    <td><b>管理后台</b> · 用户与配额、审计日志、数据库状态、孤儿文件清理</td>
    <td><b>分享链接</b> · 独立公开页，有效期 / 提取码 / 次数上限</td>
  </tr>
</table>

<p align="center">
  <img src="docs/screenshots/login.jpg" width="860" alt="登录页">
  <br>
  <sub><b>登录页</b> · 花瓣粒子背景，右上角直接切换中英文</sub>
</p>

<p align="center">
  <img src="docs/screenshots/mobile.jpg" width="240" alt="手机端">
  <br>
  <sub><b>手机端</b> · 三列网格 + 底部图标导航</sub>
</p>

> 截图由 `scripts/make_readme_shots.py` 生成：临时库 + 临时存储里的演示数据，跑完自动清理。

## 特性

- **文件管理**：目录树、网格 / 列表双视图、多选（Ctrl / Shift）、右键菜单、面包屑、搜索、重命名、移动、回收站
- **上传**：拖拽上传（文件夹保留目录结构、同名目录自动合并、空目录也建）、分片断点续传、SHA-256 秒传去重、实时进度与速度
- **下载**：支持 Range（断点续传 / 视频拖动进度条）
- **在线预览**：图片（缩放 / 拖动 / 快捷键）、文本（多编码探测）、音视频（ArtPlayer + 弹幕 / 多字幕 / 章节 / 进度缩略图 / 氛围光，同名伴生文件自动挂载）、音频全屏歌词（读取 ID3 内嵌元数据与封面，逐词滚动歌词 + 控制栏，歌词经服务端代理走歌词库 API，密钥不出服务端）、PDF
- **意见反馈**：入口在「关于」页，站内提交建议 / 问题，管理员在后台标记处理，反馈只有管理员可见
- **版本检查与更新**：关于页可手动检查；管理员可开启自动检查提示，或在后台手动/自动更新（git fast-forward + 重启，要求 main 分支、工作区干净）
- **分享链接**：有效期 / 提取码（scrypt 哈希存储）/ 下载次数上限、独立公开页；合集链接支持勾选转存，双击文件就地预览
- **回收站**：保留期自动清理、可整体关闭（需抄写服务端签发的一次性验证码）
- **管理后台**：运行概览、用户管理（配额 / 封禁 / 重置密码 / 角色 / 级联删除）、系统设置、审计日志（35 类动作）、孤儿文件清理
- **WebDAV**：`/dav` 直接挂载（Windows 资源管理器 / Finder / RaiDrive），Basic 认证、PROPFIND / LOCK / MOVE / COPY 全支持，写入同样受配额与审计约束
- **界面**：Mica 亚克力卡片、可折叠 NavigationView、亮 / 暗 / 跟随系统、主题色（预设 / 取色器 / 从背景图取主色）、4 种预设壁纸或上传自己的图片当背景、侧栏 / 标题栏 / 内容面板 / 弹窗各自可调背景不透明度、花瓣粒子、看板娘「CDP娘」（可拖动、可缩放、悬停出「锁定 / 对话 / 关闭」，均可关闭）；设置页按 WinUI 3 的分组卡片排版，手机端底部栏只留图标
- **CDP娘 AI 对话**：点「对话」开右下角浮层，SSE 流式输出、思考过程单独折叠、工具调用（翻目录 / 搜文件 / 读文本 / 查容量）；勾选「数据操作权限」后还能新建文件夹、写改文本文件、改名、复制、移动，未授权时模型手里没有这些工具，每次写操作都会在对话里标色并记审计日志。接口在「设置 → 看板娘 → 对话设置」里自填（OpenAI 兼容或 Anthropic，密钥 Fernet 加密落库、只回掩码），填完自动探测模型列表，支持图片输入、思考模式、自定义协议与输入/输出上限；系统提示词由管理员维护
- **账号**：多用户注册、独立空间与配额、scrypt 密码哈希、HMAC 会话 Cookie、登录限流
- **数据**：SQLAlchemy 2.x + MySQL 8（开发可用 SQLite，改一行 `DATABASE_URL` 即可切换）；MySQL 连不上时自动降级本地 SQLite 并自动回迁（见「数据库容灾」）

> 全站不使用原生 `alert` / `confirm`，所有提示均为自绘 Dialog / Toast / 菜单组件。

## 快速开始

需要 **Python 3.10+**（推荐 3.13 / 3.14）。仓库自带启动脚本，首次运行会自动建 `.venv`、
装依赖、从 `.env.example` 生成 `.env` 并补一个随机 `SECRET_KEY`；之后每次启动直接跑脚本即可。

**Windows**（PowerShell，或直接双击 `start.bat`）：

```powershell
.\start.ps1          # 生产启动：读 .env，不热重载
.\start.ps1 -Dev     # 开发模式：监视 app/ 与 static/，改代码自动重启
```

若提示「在此系统上禁止运行脚本」，改用
`powershell -ExecutionPolicy Bypass -File .\start.ps1`（`start.bat` 已经这么做了）。

**Linux / Ubuntu**：

```bash
chmod +x start.sh    # 只需一次
./start.sh           # 生产启动
./start.sh --dev     # 开发模式
```

Ubuntu 缺 `venv` 时先装：`sudo apt install -y python3 python3-venv python3-pip`。
脚本默认用阿里云 PyPI 镜像，要换源：

```bash
# Linux
PIP_INDEX_URL=https://pypi.org/simple ./start.sh
# Windows PowerShell
$env:PIP_INDEX_URL = "https://pypi.org/simple"; .\start.ps1
```

**手动启动**（不用脚本时，注意用虚拟环境里的解释器；直接 `python run.py` 是系统 Python，
没装依赖会报 `ModuleNotFoundError`）：

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

> ⚠️ `uvicorn app.main:app --reload` 这类**直接调用 uvicorn 的启动方式不会读 .env**，
> 会退回 uvicorn 自己的默认值 `127.0.0.1:8000`。要自定义地址请补上
> `--host 0.0.0.0 --port 8000`，或者干脆用上面的 `run.py` / `main.py`。
> 另外环境变量优先级高于 .env，IDE 运行配置里写了 `PORT` 会盖掉 .env。
>
> ⚠️ 两处容易踩的坑（0.3.0 起启动日志会直接打印正确地址）：
> - **`0.0.0.0` 只能用于监听，不能填进浏览器**。它表示「本机所有网卡」，不是一个可路由的目的地址，
>   Chrome 124+ 会直接拦掉。本机请开 `http://localhost:8000`，别的设备用 `http://<本机局域网IP>:8000`。
> - **`uvicorn main:app --reload` 会监视整个项目根目录**（日志第一行的 `Will watch for changes in these directories`），
>   上传文件写 `storage/` 也会触发重启。开发请用 `main.py` 入口（`start.sh --dev` / `start.ps1 -Dev` 就是它），
>   它只监视 `app/` 与 `static/`。

首次启动默认用 SQLite（项目根目录的 `dev.db`），要接 MySQL 改 `.env` 里的 `DATABASE_URL`
再重启即可。启动脚本会打印实际监听地址与 WebDAV 地址。想改端口或绑域名改 `.env` 即可：

| 变量 | 说明 |
| --- | --- |
| `HOST` / `PORT` | 监听地址与端口（`0.0.0.0` 允许局域网/公网访问） |
| `SITE_URL` | 对外站点地址（绑域名 / 反向代理时填），分享链接与 WebDAV 地址会用它 |
| `SSL_CERTFILE` / `SSL_KEYFILE` | 由 uvicorn 直接提供 HTTPS 时填证书路径；交给 Nginx/Caddy 就留空 |
| `RELOAD` / `TRUST_PROXY` | 开发热重载；反向代理转发时信任 X-Forwarded 头 |
| `UPDATE_REPO` / `UPDATE_TOKEN` | 检查更新用的 GitHub 仓库；私有仓库需只读 PAT（Contents: Read） |
| `UPDATE_PROXY` | 直连 GitHub 失败时的本地代理，如 `http://127.0.0.1:7897` |
| `CLOUDNETP_RESTART_CMD` | 自动更新后的重启命令（systemd / nssm 等）；留空用内置 `restart_helper.py` |

> 自动/手动更新只对 **git 检出** 生效：要求 main 分支、工作区无未提交改动、`git fetch/pull` 凭据可用；更新只做 fast-forward，不会覆盖本地提交。Docker / 压缩包部署请在宿主机重新拉取镜像或覆盖文件。

首个注册的账号自动成为管理员。

## 目录结构

```
start.sh / start.ps1 / start.bat   一键启动（Linux / Windows）
run.py               生产入口：读 .env，热重载看 RELOAD
main.py              开发入口：读 .env + 强制热重载
app/
  config.py          .env 解析与配置
  db.py              引擎 / 会话 / 建表
  models.py          9 张表的 ORM 模型
  security.py        scrypt 哈希、HMAC 令牌、限流
  storage.py         blob 内容寻址存储、Range 响应
  node_service.py    目录树与回收站逻辑
  upload_service.py  分片上传、合并、秒传
  share_service.py   分享链接与访问控制
  admin_service.py   管理后台统计与维护
  dav_service.py     WebDAV 路径解析、multistatus 拼装、内存锁、子树复制
  ai_service.py      看板娘 AI：配置、模型探测、SSE 流式、工具调用、系统提示词
  routers/           auth / nodes / uploads / shares / admin / dav / ai / live2d
static/
  css/               theme / layout / components / views / files / viewer / admin / share / legal
  js/                api / store / theme / uploader / sha256 / i18n / views / ui
  terms.html         用户协议（/terms）
  privacy.html       隐私政策（/privacy）
scripts/
  bootstrap.py       首次启动：生成 .env 与随机 SECRET_KEY
  make_readme_shots.py  生成 docs/screenshots 里的界面截图（临时库 + 演示数据）
docs/
  logo.png           图标（与站点 PWA 图标同源）
  screenshots/       README 界面截图
sql/schema.sql       MySQL 建表参考
```

## 依赖说明

只依赖 6 个包（fastapi、uvicorn、sqlalchemy、pymysql、cryptography、pillow）。
上传走裸流与分片协议，未使用 `python-multipart`；密码哈希用标准库 `hashlib.scrypt`，
会话令牌自行 HMAC 签名，因此不引入 passlib / itsdangerous / PyJWT。

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `DATABASE_URL` | `mysql+pymysql://user:pass@host:3306/db?charset=utf8mb4` 或 `sqlite:///./dev.db` |
| `SECRET_KEY` | 会话签名密钥。`start.sh` / `start.ps1` 首次运行会自动生成并写进 `.env`；留空则用临时密钥（重启即失效） |
| `STORAGE_DIR` | 文件落盘目录，默认 `./storage` |
| `DEFAULT_QUOTA_GB` | 新用户默认配额 |
| `MAX_FILE_SIZE_MB` | 单文件大小上限 |
| `DB_CONNECT_TIMEOUT` / `DB_TIMEOUT` | 远程数据库连接 / 读写超时（秒） |
| `DB_AUTO_CREATE` | 启动时是否自动建表 |
| `DB_FALLBACK_ENABLED` | 主库连不上时是否降级本地 SQLite（默认开；多实例部署请关） |
| `DB_PROBE_INTERVAL` | 主库探活间隔秒数（`0` = 只在启动时选库，运行期不切换） |
| `DB_MIRROR_INTERVAL` | 主库 → SQLite 镜像间隔秒数（`0` = 不做周期镜像） |

## 数据库容灾

`DATABASE_URL` 指向的**主库是唯一真相**；本地 SQLite（`storage/db/fallback.db`）是它的实时副本和逃生舱。

| 阶段 | 行为 |
| --- | --- |
| 正常 | 每 `DB_MIRROR_INTERVAL` 秒把主库全表镜像进 SQLite（审计日志只留最近 5000 条） |
| 主库断连 | 看门狗（`DB_PROBE_INTERVAL`）探测失败 → 新请求全部改走 SQLite，写 `__failover_dirty` 标记；文件本体在磁盘，不受影响 |
| 主库恢复 | 先把 SQLite 期间的变更**回迁**主库（按主键覆盖，可信副本才覆盖），成功后切回主库并立即重新镜像 |
| 降级期间重启 | 启动时发现 `__failover_dirty` 就先回迁再镜像，避免把降级期间的写入覆盖掉 |

几条边界：

- 按**单实例部署**设计。多实例同时写主库时，回迁的「SQLite 为准」会打架，请关掉 `DB_FALLBACK_ENABLED`。
- fallback 若是**从未镜像成功过**的新库（`__failover_mirror_at` 不存在），回迁只补缺失行、绝不覆盖主库 —— 宁可少迁，不能拿空库盖掉线上数据。
- 切换瞬间正在飞行的请求可能失败一次（旧连接已断），重试即可；新请求已走新库。
- 内部标记键都以 `__` 开头，只存在于 fallback 库，不会被同步进主库。
- 镜像与回迁都按表逐行 upsert、单表一个事务；中途失败下一轮重试，不会写坏数据。

### 在后台管理（不用重启）

管理后台 → **系统设置 → 数据库** 卡片会显示当前服务库、主库连接串（密码脱敏）、探活延迟、
容灾副本的路径/体积/可信状态，以及看门狗的探测与镜像间隔。三个动作：

| 按钮 | 作用 |
| --- | --- |
| 刷新配置 | 重读 `.env` 里的 `DATABASE_URL` 与容灾参数，就地更新配置、重建连接池并重新探活；连接串变了会一起生效 |
| 重新连接 MySQL | 丢弃旧连接池重新握手；若当前跑在 SQLite，先回迁本地数据，**回迁成功才切回**，连不上就报错并保持现状 |
| 重新连接 SQLite | 先镜像一份最新副本，再切到本地库服务；切过来后**暂停自动回切**（否则 30 秒后看门狗就改回去了），点「重新连接 MySQL」或重启进程才恢复 |

刷新与切换都会写入审计日志（`admin_db_refresh` / `admin_db_reconnect`）。刷新配置只覆盖
`.env` 里读进来的值：运维用 systemd / 命令行显式 `export` 的同名变量优先级更高，不会被文件覆盖。


## 多语言（i18n）

界面支持 **简体中文 / English** 两套文案，切换入口在「设置 → 外观 → 界面语言」，登录页右上角也能直接切。

- 语言存在 `user_settings.language`，跟着账号走；未登录时按「本机 localStorage → 浏览器语言 → 中文」兜底。
- 翻译只作用于**界面文案**：模板里的静态文字通过 `` H`...` `` 标记翻译，JS 里的句子用 `t("中文原文", { 参数 })`；
  插值进去的文件名、昵称、分享标题等用户数据永远原样输出，不会被误翻。
- 字典是「中文原文 → 译文」的普通对象（`static/js/i18n/en-US.js`）。漏翻的句子会原样显示中文，不会出现空白或占位符。
- 新增语言：在 `static/js/i18n.js` 的 `LANGS` 里加一项，再建 `static/js/i18n/<id>.js` 字典即可（服务端 `language` 白名单也要加）。
- 检查还差哪些没翻（Windows 把 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`）：

  ```bash
  .venv/bin/python scripts/i18n_check.py
  .venv/bin/python scripts/i18n_html_check.py
  ```

- 端到端冒烟（起独立实例、中英各跑一遍并统计残留中文）：

  ```bash
  .venv/bin/python scripts/i18n_shot.py --lang en-US
  .venv/bin/python scripts/i18n_share_shot.py --lang en-US
  ```

内置系统提示词（CDP娘的人设）也有中英两版，跟着**界面语言**走，但只在服务端拼装：设置页
只展示管理员自己写过的内容，内置正文不通过任何接口下发。提示词留空即用内置默认（随语言切换）；
管理员自定义过的提示词属于用户内容，不会被翻译。

当前覆盖：导航与顶栏、文件列表、设置页（含看板娘对话设置）、回收站、分享、公开分享页、
关于、登录/注册/找回密码、管理后台全部页签。仅「关于 → 更新记录」的历史条目只保留中文原文。

## 路线图

- [x] M1 地基：配置、模型、认证、Fluent 主题骨架
- [x] M2 文件核心：目录 CRUD、搜索、拖拽上传、下载 Range
- [x] M3 分片上传：断点续传、秒传、并发分片
- [x] M4 在线预览：图片 / 视频（ArtPlayer）/ 音频 / PDF / 文本
- [x] M5 分享链接与回收站增强：提取码 / 次数限制 / 回收站开关
- [x] M7 后台管理：概览、用户、审计日志、系统设置
- [x] M6 WebDAV：Basic 认证、PROPFIND / PUT / MOVE / COPY / LOCK、配额与审计接入
