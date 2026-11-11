---
title: 部署与运维
order: 11
---

# 部署与运维

## 环境变量

改 `.env` 后重启生效（后台「数据库」卡片的「刷新配置」只重读数据库相关项）。

| 变量 | 说明 |
| --- | --- |
| `HOST` / `PORT` | 监听地址与端口；`0.0.0.0` 允许局域网 / 公网访问 |
| `SITE_URL` | 对外站点地址（绑域名 / 反向代理时填），分享链接与 WebDAV 地址会用它 |
| `SECRET_KEY` | 会话签名密钥；改了会让所有旧会话失效 |
| `COOKIE_SECURE` | 公网 HTTPS 部署时改为 `true` |
| `SSL_CERTFILE` / `SSL_KEYFILE` | 由 uvicorn 直接提供 HTTPS 时填证书；交给 Nginx / Caddy 就留空 |
| `TRUST_PROXY` | 反向代理转发时信任 `X-Forwarded-Proto` / `X-Forwarded-For`（登录限流取真实 IP 靠它） |
| `DATABASE_URL` | MySQL 或 SQLite 连接串 |
| `STORAGE_DIR` | 文件落盘目录，留空用项目下的 `storage/` |
| `DEFAULT_QUOTA_GB` | 新用户默认配额 |
| `MAX_FILE_SIZE_MB` | 单文件大小上限 |
| `CHUNK_SIZE_MB` | 分片大小，默认 5 |
| `WEBDAV_ENABLED` | WebDAV 总开关，`false` 时 `/dav` 的所有方法直接 404（后台「站点」里的开关优先于这里） |
| `AI_ENABLED` | 看板娘 AI 总开关，`false` 时对话入口隐藏、`/api/ai/*` 返回 403 |
| `AI_DEFAULT_*` | 一套默认 AI 接口（地址 / 密钥 / 模型 / 协议 / 图片输入 / 思考 / 工具 / 输入输出上限）。配好后用户不用自己填就能聊，地址与密钥只对管理员可见（后台「看板娘 AI 默认接口」可覆盖，优先级更高） |
| `ALLOW_REGISTER` | 是否开放注册（后台也能开关） |
| `UPDATE_REPO` / `UPDATE_TOKEN` | 检查更新用的仓库；私有仓库要只读 PAT |
| `CLOUDNETP_RESTART_CMD` | 自动更新后的重启命令（systemd / nssm 等） |

完整列表见仓库根目录的 `.env.example`，每一项都有注释。

## 开 HTTPS

两种方式，二选一：

**A. 由 uvicorn 直接提供**（适合内网 / 自签证书）

```dotenv
SSL_CERTFILE=/etc/ssl/pan/fullchain.pem
SSL_KEYFILE=/etc/ssl/pan/privkey.pem
SITE_URL=https://pan.example.com
COOKIE_SECURE=true
```

**B. 交给 Nginx / Caddy**（推荐，证书续期也省事）

```dotenv
SITE_URL=https://pan.example.com
TRUST_PROXY=true
COOKIE_SECURE=true
```

Nginx 最小可用配置：

```nginx
server {
    listen 443 ssl http2;
    server_name pan.example.com;

    ssl_certificate     /etc/letsencrypt/live/pan.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pan.example.com/privkey.pem;

    # 上传上限要 >= MAX_FILE_SIZE_MB
    client_max_body_size 4096m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # AI 对话是 SSE 流式输出，必须关掉缓冲，否则字会攒到最后一次性出现
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
```

Caddy 更省事：

```caddyfile
pan.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

## 跑成服务（systemd）

```ini
# /etc/systemd/system/cloudnetp.service
[Unit]
Description=CloudNETP
After=network.target

[Service]
Type=simple
User=cloudnetp
WorkingDirectory=/opt/CloudNETP
ExecStart=/opt/CloudNETP/.venv/bin/python run.py
Restart=always
RestartSec=3
Environment=CLOUDNETP_RESTART_CMD=systemctl restart cloudnetp

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cloudnetp
sudo journalctl -u cloudnetp -f
```

Windows 上可以用 [NSSM](https://nssm.cc/) 把 `run.py` 注册成服务，并把
`CLOUDNETP_RESTART_CMD` 填成对应的重启命令。

## 用 Docker 跑

仓库自带 `Dockerfile` 与两份 compose，服务器 / NAS 上最省事，宿主机不用装 Python。
装法见[快速开始 → 方式一：Docker 部署](/guide/start.md)，这里只说运维相关的部分。

| 文件 | 用途 |
| --- | --- |
| `Dockerfile` | 基于 `python:3.12-slim`；以 uid `10001` 非 root 运行；healthcheck 打 `/api/version` |
| `docker-compose.yml` | 应用 + SQLite，本地构建镜像，数据落在宿主机 `./data` |
| `docker-compose.mysql.yml` | 应用 + MySQL 8.4（数据在 `mysql-data` 卷），适合多用户 / 大文件 |
| `docker-compose.hub.yml` | 不本地构建，直接拉 Docker Hub 上发布好的 `minecraftmc22/cloudnetp` |

镜像由 `.github/workflows/docker-publish.yml` 发布：打 `v*` tag 推上去就自动构建
（linux/amd64 + linux/arm64）并推送，同时打上 `latest`；预发布 tag（带 `-`）不顶 `latest`。
手动发布到 Actions 里点 Run workflow，可自定义 tag、决定是否更新 `latest`。

### 镜像里做了什么

- 数据库与上传目录通过软链接落到 `/data`（`/app/dev.db` → `/data/dev.db`、
  `/app/storage` → `/data/storage`），所以 `.env` 里的 `sqlite:///./dev.db` 和留空的
  `STORAGE_DIR` 不用为容器改。
- `HOST` 由 compose 固定为 `0.0.0.0`，对外端口跟着 `.env` 的 `PORT`（没写就是 `8123`）。
- `.env` **不在镜像里**（`.dockerignore` 排除了它），靠 compose 的 `env_file` 在创建容器时注入。
  所以改完 `.env` 要 `docker compose up -d` **重建容器**；`docker compose restart` 不会重读它。
- 容器里不是 root。宿主机 `./data` 属主对不上就上传失败，先
  `sudo chown -R 10001:10001 data`，或打开 compose 里 `user: "1000:1000"` 改成 `id -u` / `id -g`。

### 反向代理 / HTTPS

容器只提供 HTTP，TLS 交给 Nginx / Caddy，配置和上面「开 HTTPS → B」完全一样：
`client_max_body_size` 要够大、`proxy_buffering off` 必须有，`.env` 里配 `SITE_URL` 与
`COOKIE_SECURE=true`。`TRUST_PROXY` 默认开，限流与审计能拿到真实 IP。

### 升级

自己构建的：

```bash
docker compose build --pull && docker compose up -d
```

用发布镜像的（`docker-compose.hub.yml`）：

```bash
docker compose -f docker-compose.hub.yml pull && docker compose -f docker-compose.hub.yml up -d
IMAGE_TAG=0.3.67 docker compose -f docker-compose.hub.yml up -d   # 固定到某个版本
```

镜像里没有 `.git`，关于页的「检查更新 / 更新」按钮会失败，这也是 Docker 部署唯一丢掉的便利。
表结构变动由启动时的补列逻辑处理，不需要手动迁移。

### 备份容器里的数据

SQLite 版（先停容器，避免 WAL 写一半）：

```bash
docker compose stop cloudnetp
tar czf cloudnetp-$(date +%F).tar.gz data/
docker compose start cloudnetp
```

MySQL 版（应用可以不停，`--single-transaction` 保证一致性）：

```bash
docker compose -f docker-compose.mysql.yml exec -T mysql \
  mysqldump -ucloudnetp -p'cloudnetp-pass' --single-transaction cloudnetp | gzip > db-$(date +%F).sql.gz
```

密码、库名换成你 `.env` 里的值；文件本体仍在 `./data/storage`，一起备份。

### 常见坑

| 现象 | 原因与处理 |
| --- | --- |
| 上传报权限错误 / 写不进磁盘 | `./data` 属主是 root，容器跑的是 uid `10001`；`sudo chown -R 10001:10001 data` |
| 容器起一下就退出，日志提示证书文件不存在 | 从本机 `.env` 直接拷过来的 `SSL_CERTFILE` / `SSL_KEYFILE` 指向容器里没有的路径，留空即可 |
| 检查更新一直超时 | `UPDATE_PROXY` 还指着 `127.0.0.1`，那在容器里指自己；留空或换成可访问的代理 |
| 改了 `PORT` 没生效 | 只 `restart` 了；端口映射与 `HOST` 在创建容器时确定，要 `docker compose up -d` |
| 想换 Live2D 模型不重建镜像 | 打开 `docker-compose.yml` 里 `./live2d:/app/live2d:ro` 那行再 `up -d` |

## 备份

需要备份的只有两样：

```bash
# 1. 文件本体
rsync -a /opt/CloudNETP/storage/ /backup/cloudnetp/storage/

# 2. 数据库（MySQL）
mysqldump --single-transaction --routines 库名 | gzip > /backup/cloudnetp/db-$(date +%F).sql.gz

# 用 SQLite 的话直接拷
sqlite3 /opt/CloudNETP/dev.db ".backup '/backup/cloudnetp/dev.db'"
```

`storage/blob/` 是按内容哈希分片的，同一个文件只有一份，增量拷贝很快。恢复时把两样放回原位即可。

::: tip Docker 部署
容器里这些路径都在宿主机的 `./data` 下，直接备份这个目录即可，做法见上面的
[用 Docker 跑 → 备份容器里的数据](/guide/deploy.md#备份容器里的数据)。
:::

## 更新

- **后台更新**：管理后台 → 系统设置 → 版本更新 → 点更新。只做 `git pull --ff-only`，然后重启。
  要求 main 分支、工作区干净、`git fetch` 凭据可用。
- **手动更新**：

  ```bash
  cd /opt/CloudNETP
  git pull --ff-only
  .venv/bin/python -m pip install -r requirements.txt   # 依赖有变时
  sudo systemctl restart cloudnetp
  ```

- **Docker 部署**：镜像里没有 `.git`，后台更新用不了；升级走
  `docker compose build --pull && docker compose up -d`（详见上面的「用 Docker 跑」）。
- **压缩包部署**：覆盖文件后重启，数据库结构会在启动时自动补齐（`app/db.py` 里的补列逻辑）。

## 安全清单

- [ ] `SECRET_KEY` 是长随机串，且没有提交进仓库
- [ ] 公网访问走 HTTPS，`COOKIE_SECURE=true`
- [ ] 不对外开放注册时关掉 `ALLOW_REGISTER`（或后台关）
- [ ] 管理员账号用强密码；日常账号不要给管理员角色
- [ ] 反向代理设置了 `client_max_body_size`，否则大文件上传会 413
- [ ] 定期看审计日志，尤其是 `ai_tool` 与 `admin_*` 类动作
