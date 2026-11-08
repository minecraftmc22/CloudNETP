---
home: true
title: 首页
heroText: CloudNETP
tagline: 自托管的二次元个人网盘 · 文件只放在你自己的机器上
heroImage: /images/logo.png
actions:
  - text: 快速开始
    link: /guide/start.html
    type: primary
  - text: 功能一览
    link: /guide/
    type: default
features:
  - title: 文件管理
    details: 目录树、网格 / 列表双视图、多选、右键菜单、搜索、重命名、移动、回收站
  - title: 秒传与续传
    details: 拖文件夹上传保留目录结构，5 MB 分片断点续传，SHA-256 去重秒传
  - title: 在线预览
    details: 图片缩放拖动、音视频播放器与全屏歌词、PDF、Markdown、Office / ODF 文档
  - title: 分享链接
    details: 有效期 / 提取码 / 次数上限，独立公开页，合集可勾选转存
  - title: WebDAV
    details: 挂到 Windows 资源管理器、Finder 或 RaiDrive，写入同样受配额与审计约束
  - title: 看板娘 AI
    details: CDP娘 可拖动可对话，SSE 流式输出、工具调用，读写权限分开授权
  - title: 多用户与配额
    details: 独立空间与配额、角色与封禁、审计日志、孤儿文件清理
  - title: 数据库容灾
    details: 主库断连自动降级本地 SQLite，恢复后自动回迁，后台可手动切换
footer: PolyForm Noncommercial License 1.0.0 · 数据放在你自己的机器上
---

## 这是什么

CloudNETP 是一个**自己部署、自己用**的云盘：Python 3.14 + FastAPI 后端，零构建的原生
ES Module 前端，界面按 WinUI 3 / Fluent Design 的风格做，右下角常驻一位叫「CDP娘」的看板娘。

它不依赖任何云服务：文件落在你指定的磁盘目录，元数据放在 MySQL 或本地 SQLite 里。
装完之后，局域网内的手机、平板、另一台电脑都能通过浏览器访问，也可以挂成 WebDAV 盘符。

![文件管理界面](/images/files.jpg)

## 从这里开始

| 你想做的事 | 看这一篇 |
| --- | --- |
| 第一次把项目跑起来 | [快速开始](/guide/start.md) |
| 注册账号、登录、找回密码 | [注册与登录](/guide/account.md) |
| 传文件、整理目录、找回删除的文件 | [文件管理](/guide/files.md) |
| 在浏览器里看图片、听歌、看文档 | [在线预览](/guide/preview.md) |
| 把文件分享给同学 | [分享链接](/guide/share.md) |
| 把网盘挂成本地盘符 | [WebDAV 挂载](/guide/webdav.md) |
| 和看板娘聊天、让它帮忙找文件 | [看板娘与 AI 对话](/guide/mascot.md) |
| 换主题、壁纸、界面语言 | [外观与个性化](/guide/appearance.md) |
| 管理用户、看审计日志 | [管理后台](/guide/admin.md) |
| 换成 MySQL、做主备容灾 | [数据库容灾](/guide/database.md) |
| 绑域名、开 HTTPS、跑在服务器上 | [部署与运维](/guide/deploy.md) |

## 本 Wiki 怎么跑起来

在线阅读：<https://minecraftmc22.github.io/CloudNETP/>

文档用 [VuePress 2](https://v2.vuepress.vuejs.org/) + [vuepress-theme-hope](https://theme-hope.vuejs.press/)
编写，源文件就在仓库的 `Wiki/` 目录：

```bash
cd Wiki
pnpm install        # 或 npm install
pnpm docs:dev       # 本地预览：http://localhost:8080/CloudNETP/
pnpm docs:build     # 产出静态站点到 .vuepress/dist
```

本地预览地址带 `/CloudNETP/` 前缀，是因为 `base` 必须和 GitHub Pages 的仓库路径一致 ——
这样本地看到的样子就是线上看到的样子，不会出现「本地好、线上图片 404」。

::: important 文档站怎么发布
改了 `Wiki/` 下的任何文件，push 到 `main` 就会自动重新构建并发布，不需要手动跑 `docs:build`。
工作流在 `.github/workflows/deploy-wiki.yml`，也可以到 Actions 页面点 `Run workflow` 手动触发。
:::

::: tip 截图怎么来的
文档里的界面图由 `scripts/make_wiki_shots.py` 生成：脚本会建一套临时数据库与临时存储，
写入演示用户、目录树、分享记录，再起一个独立实例用 Playwright 截图，跑完自动清理 ——
不会碰你的真实数据。
:::
