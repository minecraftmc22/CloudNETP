/**
 * 关于视图：版本信息 / 主要能力 / 技术栈 / 更新记录
 */
import { escapeHtml } from "../api.js";
import { store, siteOrigin } from "../store.js";
import { Toast } from "../ui/toast.js";
import { copyText } from "../ui/share.js";
import { checkForUpdates, updateResultHtml } from "../ui/update.js";

const FEATURES = [
  {
    icon: "i-upload",
    tone: "#38bdf8",
    title: "分段上传 · 断点续传",
    desc: "大文件按 5MB 切片，断网或关页面后从已传的块继续",
  },
  {
    icon: "i-shield",
    tone: "#a78bfa",
    title: "内容寻址 · 秒传",
    desc: "以 SHA-256 识别文件，同一份内容只占一份空间",
  },
  {
    icon: "i-music",
    tone: "#f472b6",
    title: "全屏歌词播放器",
    desc: "内嵌 / 在线歌词逐词高亮，对唱自动左右分边",
  },
  {
    icon: "i-database",
    tone: "#2dd4bf",
    title: "WebDAV 挂载",
    desc: "资源管理器、Finder 直接当网络驱动器用",
  },
  {
    icon: "i-share",
    tone: "#fb923c",
    title: "分享与审核",
    desc: "提取码、转存、举报与哈希黑名单一条链路",
  },
  {
    icon: "i-cloud",
    tone: "#94a3b8",
    title: "PWA 离线外壳",
    desc: "装到桌面，断网时也能打开界面看缓存",
  },
];

const STACK = [
  ["Python 3.14", "后端语言"],
  ["FastAPI", "HTTP 框架"],
  ["SQLAlchemy", "ORM"],
  ["MySQL 8", "主库"],
  ["SQLite", "容灾兜底"],
  ["原生 ES Module", "前端，零构建"],
  ["Service Worker", "离线与缓存"],
  ["Pillow", "头像与图标处理"],
];

const AUTHOR = {
  name: "Minecraftmc22",
  initial: "M",
  repo: "https://github.com/minecraftmc22/CloudNETP",
  repoLabel: "github.com/minecraftmc22/CloudNETP",
  issues: "https://github.com/minecraftmc22/CloudNETP/issues",
};

const CHANGELOG = [
  {
    version: "0.3.32",
    title: "检查更新与自动提示",
    items: [
      "关于页新增「检查更新」：读取 GitHub main 的版本号与最新提交，有新版本时给出提交链接",
      "管理后台「系统设置 → 版本更新」可开启自动检查：管理员每天最多查一次，登录时提示新版本",
      "只读检查，不会自动拉代码或重启服务；结果缓存 10 分钟，手动刷新最快 30 秒一次",
      "私有仓库需在 .env 配置只读 UPDATE_TOKEN（Contents: Read）；国内网络可配 UPDATE_PROXY",
    ],
  },
  {
    version: "0.3.31",
    title: "看板娘可拖动、可调大小",
    items: [
      "看板娘可以直接拖着走，位置会记住，刷新后仍在原处；拖到屏幕边缘会保留一小块，不会找不回来",
      "设置 → 装饰新增「看板娘大小」，60%–180% 实时缩放，画布同步重排不模糊",
      "单击（不拖动）仍然是戳一戳，会播动作和气泡；拖动过程不会误触",
      "新增《CDP娘版权声明》：角色与模型所有权归 Minecraftmc22，未经授权禁止商用，关于页可直接打开",
    ],
  },
  {
    version: "0.3.30",
    title: "看板娘的眼睛跟着鼠标走",
    items: [
      "修复鼠标移动时看板娘视线不跟随的问题：之前传给运行库的是画布像素坐标，被内部归一化后一直夹在边缘",
      "现在直接驱动注视控制器，眼睛、头部与身体会一起朝鼠标方向转，鼠标在屏幕任何位置都有效",
      "带平滑过渡，不会瞬移；每 40ms 采样一次，几乎不占性能",
    ],
  },
  {
    version: "0.3.29",
    title: "内容面板 0% 现在是真的透明",
    items: [
      "修复模块不透明度拉到 0% 仍像一块磨砂板的问题：背景模糊与饱和度现在跟着滑块缩放，0% 完全透出壁纸",
      "面板边框也随不透明度淡出，不再留下圆角轮廓",
      "弹窗同步处理：不透明度调到很低时不会再有背景模糊",
    ],
  },
  {
    version: "0.3.28",
    title: "拖入文件夹也能上传了",
    items: [
      "把文件夹直接拖进文件页，会按原来的目录层级建好文件夹再上传，路径不再被拍平",
      "同名文件夹自动合并，重复拖同一个文件夹不会出现「项目 (1)」",
      "空文件夹也会创建；.DS_Store、Thumbs.db 这类系统文件自动跳过",
      "单次最多处理 2000 个文件、递归 12 层，超出会提示；普通文件拖拽行为不变",
    ],
  },
  {
    version: "0.3.27",
    title: "分享页双击文件直接预览",
    items: [
      "合集/文件夹分享列表里，双击文件就在当前页弹出预览，不用再跳新标签页",
      "图片、视频、音频、PDF、文本、Markdown、Office 文档都能直接看，文件夹双击则进入下一级",
      "每行新增眼睛按钮，单击也能预览；弹层内可直接下载、按 Esc 或点背景关闭",
      "手机端弹层全屏显示，预览时页面不会跟着滚动",
    ],
  },
  {
    version: "0.3.26",
    title: "每个模块单独调不透明度",
    items: [
      "设置 → 外观新增「模块不透明度」：侧栏、标题栏、内容面板、弹窗四块各自一根滑块",
      "0% 完全透明、100% 完全不透明，拖动即时预览，松手才同步到服务器",
      "侧栏与标题栏在自定义图片壁纸下默认加浓，防止亮照片把文字糊掉；不想要可以拉回 0",
      "「恢复默认」一键回到当前主题与壁纸的推荐浓度",
    ],
  },
  {
    version: "0.3.25",
    title: "自定义背景图与图片取色",
    items: [
      "外观设置里可以把壁纸换成自己上传的图片：长边缩到 2560 存 webp，透明通道保留",
      "上传后自动从图片里取出现最多的颜色作为主题色，压暗/提亮到适合当按钮色的区间",
      "主题色新增取色器，任意 HEX 都能选；另有「从背景取色」按钮可随时重取一次",
      "自定义背景叠了一层随主题变化的蒙层，亮色暗色下文字都看得清",
    ],
  },
  {
    version: "0.3.24",
    title: "举报审核能看到被举报的内容",
    items: [
      "后台举报列表改为文件类型图标 + 图片缩略图，图片能直接看出是什么，点缩略图看原图",
      "举报详情新增「被举报内容」清单：合集/文件夹举报会列出里面所有文件，逐个可打开预览",
      "新增管理员专用的举报预览接口，与分享是否已失效无关；支持缩略图、内容流和文本预览",
      "合集举报的体积改为按所有根的实际内容求和，不再只显示第一项的大小",
    ],
  },
  {
    version: "0.3.23",
    title: "分享页转存与弹窗修复",
    items: [
      "修复创建分享后结果弹窗自动消失、页面被一层看不见的遮罩挡住点不动的问题",
      "分享页恢复转存入口：列表里的项目可以勾选（支持全选），选中后一键转存到自己的网盘；文件夹内页仍可整夹转存",
      "转存接口支持一次提交多项，先统一校验配额再落盘，不会出现存了一半才报空间不足",
    ],
  },
  {
    version: "0.3.22",
    title: "批量分享（合集链接）",
    items: [
      "多选文件或文件夹后可一键分享，多项合并成一条链接，打开是列表页，可逐个预览、下载、进入文件夹",
      "分享列表新增「合集 N 项」标识；合集分享的举报按整条链接处理，不会误拉黑其中某个文件的哈希",
    ],
  },
  {
    version: "0.3.21",
    title: "后台用户列表显示头像与昵称",
    items: [
      "管理后台「用户」页改为展示真实头像与昵称，登录名在昵称不同时以 @ 形式附在下一行",
      "头像接口放行管理员读取其他账号的头像（普通用户仍只能读自己的）",
    ],
  },
  {
    version: "0.3.20",
    title: "修复幻灯片图片渲染",
    items: [
      "修复含图片的 pptx / ppsx 预览报「This method has been removed in JSZip 3.0」：JSZip 3 移除了 asUint8Array 等同步方法，幻灯片图片改走 async() 读取",
    ],
  },
  {
    version: "0.3.19",
    title: "放映格式与预览体积上限",
    items: [
      "支持 .ppsx / .pps / .potx 等放映与模板格式的在线预览（与 .pptx 同一套解析）",
      "文档类文件超过 50 MB 不再在线预览，改为提示下载到本地打开，避免浏览器解析大文件时卡死",
    ],
  },
  {
    version: "0.3.18",
    title: "账号安全与更多预览格式",
    items: [
      "注册加服务端图形验证码（点阵几何图形，取不到明文），登录连错两次自动要求验证码",
      "注册校验加强：用户名至少 4 位、密码必须含数字与小写字母，下方实时显示密码强度；用户名可用性即时检测",
      "支持邮箱注册与「邮箱 / 用户名」双通道登录，右上角会对未绑定邮箱的账号做提醒",
      "新增忘记密码：用户名 + 绑定邮箱即可重置，重置令牌一次性且 15 分钟过期；找不回时可联系管理员",
      "在线预览扩展到 odt / ods / odp / rtf / txt / csv，并支持旧版 doc / ppt 的文字级预览",
      "数据库支持在 .env 里用 DB_USER / DB_PASSWORD 组合配置，密码里的特殊字符自动转义",
    ],
  },
  {
    version: "0.3.17",
    title: "预览增强与品牌统一",
    items: [
      "新增 Word / Excel / PowerPoint / Markdown 在线预览，全部在浏览器本地解析",
      "登录注册页、分享页的品牌图标统一换成角色形象，并重新生成整套站点图标",
      "接口文档 /api/docs 与 /api/openapi.json 改为仅管理员可访问",
    ],
  },
  {
    version: "0.3.16",
    title: "Live2D 看板娘",
    items: [
      "看板娘换成 Live2D 模型，模型放 live2d/model/ 即自动发现，无需改代码",
      "有多个模型时悬停出现「换一个」；目录为空或加载失败仍退回云朵",
    ],
  },
  {
    version: "0.3.15",
    title: "管理后台标签栏",
    items: ["修复窄屏下后台标签文字折行、撑出药丸的问题", "手机端隐藏标签图标，五个标签尽量排成一行"],
  },
  {
    version: "0.3.14",
    title: "关于页作者与仓库",
    items: ["关于页新增「作者与仓库」：作者名与 GitHub 仓库入口", "页脚补上作者署名与仓库链接"],
  },
  {
    version: "0.3.13",
    title: "关于页",
    items: ["新增关于页：版本号、能力清单、技术栈与更新记录", "设置页「关于」一行改为可跳转"],
  },
  {
    version: "0.3.12",
    title: "文件类型图标",
    items: ["图标细分为图片 / 视频 / 音乐 / 文本 / 代码 / Word / PDF / 表格 / 幻灯片 / 压缩包十类", "侧栏「设置」换成真正的齿轮"],
  },
  {
    version: "0.3.11",
    title: "侧栏品牌头像",
    items: ["标题栏左上角换成角色头像特写（原为云朵图标）"],
  },
  {
    version: "0.3.10",
    title: "站点图标",
    items: ["重做整套 favicon：抠出角色、加白色贴纸边、深紫粉光底", "补齐 ico 与 16/32/maskable/apple-touch"],
  },
  {
    version: "0.3.9",
    title: "分享举报与转存",
    items: [
      "公开分享页可一键转存到自己的网盘（复用 blob，不复制物理数据）",
      "匿名举报，后台审核通过后按哈希拉黑：禁止再分享与转存，文件仍归物主",
    ],
  },
  {
    version: "0.3.8",
    title: "行级歌词高亮",
    items: ["只有行级时间轴的歌词按整行做卡拉 OK 填充"],
  },
  {
    version: "0.3.7",
    title: "对唱与翻译开关",
    items: ["多行同时高亮，对唱按演唱者左右分边", "修复歌词翻译开关点不动的问题"],
  },
  {
    version: "0.3.6",
    title: "系统媒体控制",
    items: ["修复播放/暂停图标只显示一个", "接入 MediaSession，Windows 媒体浮窗可控制播放"],
  },
  {
    version: "0.3.5",
    title: "歌词来源与样式",
    items: ["每首歌可选内嵌 / 在线 / 无歌词，默认音频自带优先", "字号、高亮色、翻译开关随账号保存"],
  },
  {
    version: "0.3.4",
    title: "音频元数据",
    items: ["支持 14 种音频容器与 ASF / APEv2 标签、内嵌封面"],
  },
  {
    version: "0.3.3",
    title: "全屏歌词播放器",
    items: ["逐词渐变高亮、点行跳播、封面与元数据"],
  },
  {
    version: "0.3.2",
    title: "移动端适配",
    items: ["底部标签栏、对话框底部抽屉、安全区内边距"],
  },
  {
    version: "0.3.1",
    title: "数据库容灾",
    items: ["主库探测与降级、周期镜像、恢复后自动回迁"],
  },
  {
    version: "0.2.8",
    title: "PWA",
    items: ["可安装到桌面，导航与静态资源网络优先，API 不缓存"],
  },
  {
    version: "0.2.7",
    title: "WebDAV Digest 认证",
    items: ["解决 Windows 明文 HTTP 只认 Digest 的 0x80070043"],
  },
  {
    version: "0.2.6",
    title: "任务面板与个人资料",
    items: ["移动任务进度面板；昵称、简介与头像"],
  },
];

const VISIBLE = 4;

export function renderAbout(root) {
  const meta = store.get().meta || {};
  const { user } = store.get();
  const appName = meta.appName || "CloudNETP";
  const version = meta.version || "0.1.0";
  const origin = siteOrigin();
  const host = origin.replace(/^https?:\/\//, "");
  const fallback = meta.dbMode === "fallback";

  root.innerHTML = `
    <div class="view-head">
      <div class="view-title"><svg class="icon"><use href="#i-info"/></svg>关于</div>
      <span class="view-sub">版本、能力与更新记录</span>
    </div>
    <div class="view-body">
      <div class="about">

        <section class="about-hero">
          <div class="about-hero__avatar"><img src="/assets/brand-avatar.png" alt=""></div>
          <div class="about-hero__main">
            <div class="about-hero__title">
              <h2>${escapeHtml(appName)}</h2>
              <span class="chip chip--accent">v${escapeHtml(version)}</span>
            </div>
            <p class="about-hero__desc">自托管的多用户网盘：文件上传、在线预览、分享与 WebDAV 都跑在自己的一台机器上，数据不经过第三方；界面按 WinUI3 / Fluent 的语法做成粉紫系。</p>
            <div class="about-hero__links">
              <a class="about-link" href="${escapeHtml(origin)}/" target="_blank" rel="noopener">
                <svg class="icon icon--s"><use href="#i-link"/></svg>${escapeHtml(host)}
              </a>
              <span class="about-dot">·</span>
              <span class="about-hero__who">当前登录 <b>${escapeHtml(user?.displayName || user?.username || "—")}</b></span>
            </div>
            <p class="about-update" data-update-status hidden></p>
          </div>
          <div class="about-hero__side">
            <button class="btn" type="button" data-act="check-update">
              <svg class="icon icon--s"><use href="#i-refresh"/></svg>检查更新
            </button>
            <button class="btn" type="button" data-act="copy">
              <svg class="icon icon--s"><use href="#i-link"/></svg>复制地址
            </button>
            <span class="about-badge ${fallback ? "is-warn" : ""}">
              <svg class="icon icon--s"><use href="#i-database"/></svg>
              ${fallback ? "降级运行 · SQLite" : "主库 · MySQL"}
            </span>
          </div>
        </section>

        <section class="about-card">
          <div class="about-card__head"><svg class="icon icon--s"><use href="#i-star"/></svg>主要能力</div>
          <div class="about-feats">
            ${FEATURES.map(
              (f) => `
              <div class="about-feat" style="--tone:${f.tone}">
                <span class="about-feat__icon"><svg class="icon"><use href="#${f.icon}"/></svg></span>
                <div>
                  <div class="about-feat__title">${f.title}</div>
                  <div class="about-feat__desc">${f.desc}</div>
                </div>
              </div>`
            ).join("")}
          </div>
        </section>

        <section class="about-card">
          <div class="about-card__head"><svg class="icon icon--s"><use href="#i-code"/></svg>技术栈</div>
          <div class="about-stack">
            ${STACK.map(
              ([name, role]) => `
              <div class="about-stack__item">
                <span class="about-stack__name">${name}</span>
                <span class="about-stack__role">${role}</span>
              </div>`
            ).join("")}
          </div>
          <div class="about-card__note">
            后端单体 FastAPI，前端不打包：改完文件刷新即生效，适合手动上传部署。
          </div>
        </section>

        <section class="about-card">
          <div class="about-card__head"><svg class="icon icon--s"><use href="#i-user"/></svg>作者与仓库</div>
          <div class="about-author">
            <span class="about-author__mark">${escapeHtml(AUTHOR.initial)}</span>
            <span class="about-author__body">
              <span class="about-author__name">${escapeHtml(AUTHOR.name)}</span>
              <span class="about-author__role">作者 / 维护者</span>
            </span>
          </div>
          <div class="about-links">
            <a class="about-lrow" href="${AUTHOR.repo}" target="_blank" rel="noopener">
              <span class="about-lrow__icon"><svg class="icon"><use href="#i-github"/></svg></span>
              <span class="about-lrow__body">
                <span class="about-lrow__label">GitHub 仓库</span>
                <span class="about-lrow__value">${escapeHtml(AUTHOR.repoLabel)}</span>
              </span>
              <svg class="icon icon--s about-lrow__go"><use href="#i-arrow-up-right"/></svg>
            </a>
            <a class="about-lrow" href="${AUTHOR.issues}" target="_blank" rel="noopener">
              <span class="about-lrow__icon"><svg class="icon"><use href="#i-info"/></svg></span>
              <span class="about-lrow__body">
                <span class="about-lrow__label">问题反馈</span>
                <span class="about-lrow__value">提交 Issue 或 Pull Request</span>
              </span>
              <svg class="icon icon--s about-lrow__go"><use href="#i-arrow-up-right"/></svg>
            </a>
            <a class="about-lrow" href="/live2d/model/CDP/COPYRIGHT.md" target="_blank" rel="noopener">
              <span class="about-lrow__icon"><svg class="icon"><use href="#i-shield"/></svg></span>
              <span class="about-lrow__body">
                <span class="about-lrow__label">CDP娘版权声明</span>
                <span class="about-lrow__value">所有权归 Minecraftmc22，未经授权禁止商用</span>
              </span>
              <svg class="icon icon--s about-lrow__go"><use href="#i-arrow-up-right"/></svg>
            </a>
          </div>
        </section>

        <section class="about-card">
          <div class="about-card__head">
            <svg class="icon icon--s"><use href="#i-clock"/></svg>更新记录
            <span class="about-card__count">${CHANGELOG.length} 个版本</span>
          </div>
          <ol class="about-log">
            ${CHANGELOG.map(
              (c, i) => `
              <li class="about-log__item ${i >= VISIBLE ? "is-extra" : ""} ${i === 0 ? "is-latest" : ""}">
                <span class="about-log__dot"></span>
                <div class="about-log__body">
                  <div class="about-log__head">
                    <span class="about-log__ver">v${c.version}</span>
                    <span class="about-log__title">${c.title}</span>
                  </div>
                  <ul class="about-log__items">
                    ${c.items.map((t) => `<li>${t}</li>`).join("")}
                  </ul>
                </div>
              </li>`
            ).join("")}
          </ol>
          <button class="about-more" type="button" data-act="more">
            <span data-more-text>展开全部 ${CHANGELOG.length} 个版本</span>
            <svg class="icon icon--s"><use href="#i-chevron-down"/></svg>
          </button>
        </section>

        <footer class="about-foot">
          <span>© 2026 ${escapeHtml(appName)} · 数据存放在你自己的服务器上</span>
          <span class="about-foot__sep">·</span>
          <span>作者 <a class="about-foot__link" href="${AUTHOR.repo}" target="_blank" rel="noopener">${escapeHtml(AUTHOR.name)}</a></span>
          <span class="about-foot__sep">·</span>
          <span>版本号随每次改动递增 0.0.1</span>
        </footer>
      </div>
    </div>
  `;

  root.querySelector('[data-act="copy"]')?.addEventListener("click", async () => {
    if (await copyText(`${origin}/`)) Toast.success("地址已复制");
    else Toast.error("复制失败，请手动选中地址");
  });

  const checkBtn = root.querySelector('[data-act="check-update"]');
  const updateStatus = root.querySelector("[data-update-status]");
  checkBtn?.addEventListener("click", async () => {
    checkBtn.disabled = true;
    if (updateStatus) {
      updateStatus.hidden = false;
      updateStatus.innerHTML = `<span class="update-state">正在检查 GitHub…</span>`;
    }
    try {
      const data = await checkForUpdates(true);
      if (updateStatus) updateStatus.innerHTML = updateResultHtml(data);
    } catch (err) {
      if (updateStatus) {
        updateStatus.innerHTML = `<span class="update-state is-error">检查失败：${escapeHtml(
          err.message || "网络不可达"
        )}</span>`;
      }
    } finally {
      checkBtn.disabled = false;
    }
  });

  const more = root.querySelector('[data-act="more"]');
  const moreText = root.querySelector("[data-more-text]");
  more?.addEventListener("click", () => {
    const list = root.querySelector(".about-log");
    const open = !list.classList.contains("is-open");
    list.classList.toggle("is-open", open);
    more.classList.toggle("is-open", open);
    moreText.textContent = open ? "收起" : `展开全部 ${CHANGELOG.length} 个版本`;
    if (!open) more.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
}
