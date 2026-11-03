/**
 * 关于视图：版本信息 / 主要能力 / 技术栈 / 更新记录
 */
import { escapeHtml } from "../api.js";
import { store, siteOrigin } from "../store.js";
import { Toast } from "../ui/toast.js";
import { copyText } from "../ui/share.js";

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
          </div>
          <div class="about-hero__side">
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
