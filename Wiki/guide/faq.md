---
title: 常见问题
order: 13
---

# 常见问题

## 启动相关

### 浏览器打不开 `0.0.0.0:8000`

`0.0.0.0` 只是「监听所有网卡」的写法，不是可访问的地址，Chrome 124+ 会直接拦掉。
本机开 <http://localhost:8000>，别的设备用 `http://<本机局域网 IP>:8000`。
启动日志第一行会打印实际可用地址。

### 提示「在此系统上禁止运行脚本」

PowerShell 的默认执行策略。三种解法任选：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
# 或
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# 或直接双击 start.bat（它已经带了 Bypass）
```

### `ModuleNotFoundError: No module named 'fastapi'`

用了系统 Python，而不是虚拟环境里的解释器。用脚本启动，或显式写
`.venv\Scripts\python.exe run.py` / `.venv/bin/python run.py`。

### 端口被占用（`WinError 10048` / `Address already in use`）

改 `.env` 的 `PORT`，或者先找出占用进程：

```powershell
netstat -ano | findstr :8000      # Windows，最后一列是 PID
```

```bash
lsof -i :8000                     # Linux / macOS
```

### 改代码后上传文件会触发重启

用 `uvicorn main:app --reload` 起的服会监视整个项目根目录，`storage/` 里的写入也算变更。
开发请用 `main.py`（`start.sh --dev` / `start.ps1 -Dev`），它只监视 `app/` 与 `static/`。

## Docker

### 容器起来了，但上传报权限错误

宿主机 `./data` 是 root 建的，而容器里进程是 uid `10001`：

```bash
sudo chown -R 10001:10001 data
docker compose up -d
```

不想 chown 就打开 `docker-compose.yml` 里的 `user: "1000:1000"`，改成 `id -u` / `id -g` 的结果。

### 改了 `.env` 但容器里没变

`env_file` 只在**创建容器**时注入，`docker compose restart` 不会重读。改完用
`docker compose up -d`（镜像没变时不会重新构建）。

### 关于页点「检查更新」一直失败

镜像里没有 `.git`，自动更新对 Docker 部署不可用。升级请
`docker compose build --pull && docker compose up -d`。

### 端口怎么是 8123 而不是 8000

`8123` 是镜像自己的默认值。compose 的端口映射读 `.env` 的 `PORT`，所以 `.env` 里留着
`PORT=8000` 就还是 8000；删掉那行才会退回 8123。

### 拉镜像报 `manifest unknown` / `not found`

说明这个 tag 还没发布过。镜像由仓库打 tag 后自动构建，先确认 Docker Hub 上有对应标签
（`latest` 与版本号两种），或者干脆用 `docker-compose.yml` 在本机构建。
`IMAGE_TAG` 写错版本号也是这个报错。

## 上传与预览

### 上传到一半失败

- 看单文件是否超过 `MAX_FILE_SIZE_MB`（默认 4096 MB）。
- 看配额是否满了（顶栏会显示用量）。
- 断网导致的中断可以直接重传同一个文件，已传的块会被复用。
- 反向代理后面部署时，Nginx 的 `client_max_body_size` 要放开。

### 大文件预览提示「文件过大」

预览有上限，超过就只给下载。压缩包、可执行文件本来就没有预览器，双击即下载。

### 视频拖进度条卡顿

视频走 Range 请求，正常可以即时跳转。如果经过反向代理，确认没有开启响应缓冲
（`proxy_buffering off`），否则会退化成整段下载。

## WebDAV

### Windows 报 `0x80070043`

先确认地址带了 `/dav`、账号密码正确。本站的 `401` 会先给 Digest 再给 Basic，正常情况下
**不需要**改注册表 `BasicAuthLevel`。如果系统被策略强制只允许 Basic，才需要打开：

```powershell
reg add HKLM\SYSTEM\CurrentControlSet\Services\WebClient\Parameters /v BasicAuthLevel /t REG_DWORD /d 2 /f
net stop webclient && net start webclient
```

### 一直弹凭据框

用户名是你的**用户名**（不是昵称），密码是当前登录密码。刚改过密码的话要重新输入一次。

### WebDAV 里删除的文件能恢复吗

能，和网页端一样进回收站，保留期相同。

## 分享

### 链接打不开 / 提示不存在

可能已过期、被创建者取消，或下载次数用完。让创建者重新建一条。

### 提取码忘了

提取码只存哈希，谁都查不到明文。取消这条分享重建一个即可。

## 数据库

### 界面显示在用 SQLite，但我配的是 MySQL

主库连不上时看门狗自动降级了。到「管理后台 → 系统设置 → 数据库」看探活状态，
修好主库后点「重新连接 MySQL」，会先回迁本地变更再切回。

### 能同时跑多个实例吗

不建议。容灾的回迁逻辑按单实例设计，多实例请把 `DB_FALLBACK_ENABLED` 关掉，
并且不要共用同一个 `STORAGE_DIR`。

## 账号

### 忘了管理员密码

用另一个管理员在后台重置（普通管理员重置不了超管的密码，得由另一个超管来）。
整个站只剩一个超管且密码丢了时，可以临时用 SQL 改密码哈希（**先备份数据库**），
或者新建一个空库重新注册第一个账号（会得到新的超级管理员），再把数据迁过去。

### 关掉注册后还能建号吗

后台「系统设置 → 站点」关掉开放注册后，普通访客看不到注册页签，接口也会拒绝。
管理员仍可在用户管理里手动创建。

### 登录后直接跳到「账号申诉」页

说明这个账号正被封禁。登录接口不会返回「密码错误」，而是带上封禁原因、封禁时间与解封时间把你
带到申诉页；在那里可以填说明提交给管理员。临时封禁到期会**自动解封**，不需要手动操作。

如果你确信是误判，提交申诉后等管理员在后台「申诉审核」里处理即可；处理结果会显示在申诉页，
也可以自己刷新查看。详见[注册与登录 → 被管理员封禁了怎么办](/guide/account.md#被管理员封禁了怎么办)。

### 注销账号后还能恢复吗

不能。注销是**立即物理删除**：账号、文件、分享、快传、看板娘对话与记忆都会清掉，
管理员也无法还原。想留后路的话，先把重要文件分享或转存到别的账号，再注销。

## 其他

### 125% 缩放下注册页看不全 / 协议页滚不动

两个都已修：注册表单比屏幕高时，登录页自己就是滚动容器（滚轮或方向键都能往下走），
视口矮的时候还会收紧间距、缩小左侧插画；用户协议与隐私政策页恢复了整页滚动。

如果仍然滚不动，先确认不是浏览器扩展（例如强制暗色、阅读模式）加了 `overflow: hidden`。

### 界面里有几句中文没翻译

可以先切换一次语言（登录页右上角，或「设置 → 外观 → 界面语言」）再切回来，让界面整页重绘。
仍有残留的话多半是这两类，都不算 bug：

- **用户数据**：文件名、用户名、审计详情里的路径，本来就按原样显示；
- **更新记录**：正文只提供中文原文，英文界面下卡片顶部会写明这一点；
- **服务端返回的原因**：例如「至少需要保留一名管理员」这类接口错误，目前由后端直接给出中文说明，
  界面上原样展示。

看板娘的名字固定叫 **CDP娘**，英文界面里也不改名。

### 缩略图不显示

`.env` 里 `THUMB_ENABLED=true` 才会生成缩略图；刚上传的文件需要几秒。大图首次生成会慢一点。

### 手机能装成 App 吗

支持 PWA：手机浏览器打开后选「添加到主屏幕」，图标与启动画面都用站点自带的。

### 更新提示 404 / 401

私有仓库必须配 `UPDATE_TOKEN`（fine-grained PAT，Contents: Read），否则匿名 API 返回 404。
国内直连 GitHub 被重置时，配 `UPDATE_PROXY` 指向本地代理。
