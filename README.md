# CloudNETP

WinUI3 / Fluent Design 风格的二次元个人网盘，Python 3.14 + FastAPI 后端，零构建原生 ES Module 前端。

## 特性

- **文件管理**：目录树、网格 / 列表双视图、多选（Ctrl / Shift）、右键菜单、面包屑、搜索、重命名、移动、回收站
- **上传**：拖拽上传、分片断点续传、SHA-256 秒传去重、实时进度与速度
- **下载**：支持 Range（断点续传 / 视频拖动进度条）
- **在线预览**：图片（缩放 / 拖动 / 快捷键）、文本（多编码探测）、音视频（ArtPlayer + 弹幕 / 多字幕 / 章节 / 进度缩略图 / 氛围光，同名伴生文件自动挂载）、音频全屏歌词（读取 ID3 内嵌元数据与封面，逐词滚动歌词 + 控制栏，歌词经服务端代理走歌词库 API，密钥不出服务端）、PDF
- **分享链接**：有效期 / 提取码（scrypt 哈希存储）/ 下载次数上限、独立公开页
- **回收站**：保留期自动清理、可整体关闭（需抄写服务端签发的一次性验证码）
- **管理后台**：运行概览、用户管理（配额 / 封禁 / 重置密码 / 角色 / 级联删除）、系统设置、审计日志（35 类动作）、孤儿文件清理
- **WebDAV**：`/dav` 直接挂载（Windows 资源管理器 / Finder / RaiDrive），Basic 认证、PROPFIND / LOCK / MOVE / COPY 全支持，写入同样受配额与审计约束
- **界面**：Mica 亚克力卡片、可折叠 NavigationView、亮 / 暗 / 跟随系统、6 种主题色、4 种壁纸、花瓣粒子、看板娘（均可关闭）
- **账号**：多用户注册、独立空间与配额、scrypt 密码哈希、HMAC 会话 Cookie、登录限流
- **数据**：SQLAlchemy 2.x + MySQL 8（开发可用 SQLite，改一行 `DATABASE_URL` 即可切换）；MySQL 连不上时自动降级本地 SQLite 并自动回迁（见「数据库容灾」）

> 全站不使用原生 `alert` / `confirm`，所有提示均为自绘 Dialog / Toast / 菜单组件。

## 快速开始

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
cp .env.example .env        # 填 DATABASE_URL 与 SECRET_KEY
python run.py               # 监听地址/端口/对外地址/SSL 全部读 .env
python main.py              # 开发用：同样读 .env，另外默认开热重载
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
>   上传文件写 `storage/` 也会触发重启。开发请用 `python main.py`，它只监视 `app/` 与 `static/`。

启动脚本会打印实际监听地址与 WebDAV 地址。想改端口或绑域名改 `.env` 即可：

| 变量 | 说明 |
| --- | --- |
| `HOST` / `PORT` | 监听地址与端口（`0.0.0.0` 允许局域网/公网访问） |
| `SITE_URL` | 对外站点地址（绑域名 / 反向代理时填），分享链接与 WebDAV 地址会用它 |
| `SSL_CERTFILE` / `SSL_KEYFILE` | 由 uvicorn 直接提供 HTTPS 时填证书路径；交给 Nginx/Caddy 就留空 |
| `RELOAD` / `TRUST_PROXY` | 开发热重载；反向代理转发时信任 X-Forwarded 头

首个注册的账号自动成为管理员。

## 目录结构

```
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
  routers/           auth / nodes / uploads / shares / admin / dav
static/
  css/               theme / layout / components / views / files / viewer / admin / share
  js/                api / store / theme / uploader / sha256 / views / ui
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
| `SECRET_KEY` | 会话签名密钥，留空会生成临时密钥（重启即失效） |
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


## 路线图

- [x] M1 地基：配置、模型、认证、Fluent 主题骨架
- [x] M2 文件核心：目录 CRUD、搜索、拖拽上传、下载 Range
- [x] M3 分片上传：断点续传、秒传、并发分片
- [x] M4 在线预览：图片 / 视频（ArtPlayer）/ 音频 / PDF / 文本
- [x] M5 分享链接与回收站增强：提取码 / 次数限制 / 回收站开关
- [x] M7 后台管理：概览、用户、审计日志、系统设置
- [x] M6 WebDAV：Basic 认证、PROPFIND / PUT / MOVE / COPY / LOCK、配额与审计接入
