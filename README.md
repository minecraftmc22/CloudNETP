# CloudNETP

WinUI3 / Fluent Design 风格的二次元个人网盘，Python 3.14 + FastAPI 后端，零构建原生 ES Module 前端。

## 特性

- **文件管理**：目录树、网格 / 列表双视图、多选（Ctrl / Shift）、右键菜单、面包屑、搜索、重命名、移动、回收站
- **上传**：拖拽上传、分片断点续传、SHA-256 秒传去重、实时进度与速度
- **下载**：支持 Range（断点续传 / 视频拖动进度条）
- **界面**：Mica 亚克力卡片、可折叠 NavigationView、亮 / 暗 / 跟随系统、6 种主题色、4 种壁纸、花瓣粒子、看板娘（均可关闭）
- **账号**：多用户注册、独立空间与配额、scrypt 密码哈希、HMAC 会话 Cookie、登录限流
- **数据**：SQLAlchemy 2.x + MySQL 8（开发可用 SQLite，改一行 `DATABASE_URL` 即可切换）

> 全站不使用原生 `alert` / `confirm`，所有提示均为自绘 Dialog / Toast / 菜单组件。

## 快速开始

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
cp .env.example .env        # 填 DATABASE_URL 与 SECRET_KEY
python main.py              # 默认 http://127.0.0.1:8000
```

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
  routers/           auth / nodes / uploads
static/
  css/               theme / layout / components / views / files
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

## 路线图

- [x] M1 地基：配置、模型、认证、Fluent 主题骨架
- [x] M2 文件核心：目录 CRUD、搜索、拖拽上传、下载 Range
- [x] M3 分片上传：断点续传、秒传、并发分片
- [ ] M4 在线预览：图片 / 视频 / 音频 / PDF / 代码
- [ ] M5 分享链接与回收站增强
- [ ] M6 WebDAV
- [ ] M7 后台管理
