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

- **Docker / 压缩包部署**：重新拉镜像或覆盖文件，数据库结构会在启动时自动补齐
  （`app/db.py` 里的补列逻辑）。

## 安全清单

- [ ] `SECRET_KEY` 是长随机串，且没有提交进仓库
- [ ] 公网访问走 HTTPS，`COOKIE_SECURE=true`
- [ ] 不对外开放注册时关掉 `ALLOW_REGISTER`（或后台关）
- [ ] 管理员账号用强密码；日常账号不要给管理员角色
- [ ] 反向代理设置了 `client_max_body_size`，否则大文件上传会 413
- [ ] 定期看审计日志，尤其是 `ai_tool` 与 `admin_*` 类动作
