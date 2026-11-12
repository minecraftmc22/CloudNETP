/**
 * 关于视图：版本信息 / 主要能力 / 技术栈 / 更新记录
 */
import { escapeHtml } from "../api.js";
import { H, getLang, t } from "../i18n.js";
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
    desc: "内嵌 / 网易云歌词逐词高亮，对唱自动左右分边",
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
    version: "0.3.74",
    title: "主库、次库都能自己挑",
    items: [
      "管理后台「数据库」卡片多了两个下拉：主库选 MySQL 或 SQLite，次库还能选「不使用」，四种组合随你搭",
      "换主库不会丢数据：先连新库探活，通了就把当前库的数据整体复制过去再切，旧库原样留着；连不上直接报错，当前库一动不动",
      "选择记在 storage/db/roles.json，重启照样生效；不想用后台也能照旧改 .env 的 DB_PRIMARY / DB_SECONDARY",
      "次库选「不使用」时，镜像与自动回切一起停掉，「切到次库」按钮变灰，不会再出现「关了但还在偷偷切」的情况",
      "当前服务库的标签会写明是主库还是次库、跑的是 MySQL 还是 SQLite，换完库一眼能对上",
      "每个版本号变化都会自动构建镜像：改了 VERSION 推到 main 就触发，Docker Hub 上同一个版本只会构建一次，不会再漏发版",
    ],
  },
  {
    version: "0.3.73",
    title: "取件码能自己上传文件，还能让 CDP娘 帮你取件",
    items: [
      "快传 → 取件码：直接把文件拖进去或选本机文件，支持一次多个，传完就能生成 6 位码；这些文件单独存放，不占你的网盘空间，也不会出现在「我的文件」里",
      "每个文件可以单独设有效期和下载次数，一个文件用完不影响同一条码里的其它文件；管理员能设有效期与次数上限",
      "登录后也能输入别人的取件码：看清单、一键转存到指定目录，或直接下载",
      "管理后台新增「快传取件码」卡片：最长有效期、单文件下载次数上限、过期后文件保留天数，以及「立即清理过期文件」；清理只删快传自己存的文件，不动你网盘里的东西",
      "我的分享支持分页，默认每页 10 条，可切 20 / 50；已取消 / 已过期的记录可以「重新分享」或「删除记录」",
      "CDP娘 新增「快传取件码」权限：可以先帮你找到文件、确认后再生成取件码；也能用取件码帮你把文件转存到指定目录或给出下载链接",
      "工具调用现在统一收在回答下方的「使用工具 N」里，点一下展开，能看到调用了什么、结果是什么",
    ],
  },
  {
    version: "0.3.72",
    title: "上传可以在后台跑了",
    items: [
      "上传时切到别的页面（我的分享、快传、设置…）不会再中断，右下角队列跟着你走，传完回到「我的文件」直接能看到",
      "队列里的每个任务都能暂停 / 继续：暂停时已传的分片留在服务端，继续时接着传，不重新读一遍文件；标题栏也能一键「全部暂停 / 全部继续」",
      "进入文件夹、搜索、刷新时会盖一层带旋转图标的加载遮罩，不用盯着没变化的旧列表猜是不是没点上",
      "选目录的弹窗（移动文件、快传选文件）加载时也换成旋转图标，不再只有一行「加载中…」",
      "失败条目仍然可以点刷新重试，成功或取消的条目点 ✕ 只是从列表里移除，不会影响其它任务",
    ],
  },
  {
    version: "0.3.71",
    title: "上传列表能收起来了",
    items: [
      "「我的文件」右下角的上传列表多了一个开关按钮（只有图标）：点一下收起成一条标题栏，再点展开；收起状态按设备记住，下次进来还是收起的",
      "上传失败的条目右侧出现刷新图标，点一下就从断点重传，成功后自动从列表里收走，不用重新选文件",
      "失败时列表会自动展开一次，免得收起来以后不知道哪一条挂了；标题栏仍会显示「N 个失败」",
      "已经结束（完成 / 失败 / 取消）的条目，点 ✕ 现在是把它从列表里去掉，不再什么都不做",
      "顺手修了个层级问题：上传面板原来被看板娘盖住，右下角的按钮点不动；现在面板浮在最上层，手机端也抬到标签栏之上",
    ],
  },
  {
    version: "0.3.70",
    title: "快传加了使用规范与举报",
    items: [
      "第一次用快传要先读并勾选同意《快传使用规范》：禁止上传法律法规禁止的文件，违者封号并依法追究法律责任；同意状态记在账号上，换设备不用再勾",
      "快传页顶部常驻警示条，手机扫码上传页也写明禁止上传违法内容",
      "上传时会比对已被举报确认的文件哈希：命中就直接拒绝，并提示「此文件包含法律禁止的内容，无法上传」，服务端按合并后的真实哈希判定，改文件名或声明假的哈希都不管用",
      "取件页每个文件都加了「举报」按钮，不用登录就能提交；管理员在后台审核通过后，该文件立即禁止分享、取件与下载，同时撤销它所在的取件码",
      "后台举报列表能区分「分享」与「快传取件码」两种来源，取件码举报会显示对应的 6 位码",
    ],
  },
  {
    version: "0.3.69",
    title: "快传收紧了上传口子",
    items: [
      "扫码直传改为必须登录，而且只有生成链接的人能往上上传：链接就算传出去，别人也塞不进你的网盘、吃不到你的配额",
      "未登录扫码时页面只给「登录 / 注册」和「去取件」两个按钮，登录完自动回到原来的上传页，不用再扫一次",
      "取件码保持匿名：不登录照样能输码看文件、下载，只有往网盘里传才要账号",
      "登录页底部新增「有取件码？去取件」入口，没账号的人也知道去哪儿输码",
    ],
  },
  {
    version: "0.3.68",
    title: "加了个「快传」",
    items: [
      "侧栏新增「快传」：扫码直传、发给用户、取件码三个标签页，都在一处管理",
      "扫码直传：电脑上生成二维码与一次性链接，手机扫码打开就能选照片和文件，直接落进你指定的目录，手机上不用登录",
      "发给用户：选中文件或文件夹填上对方用户名，直接复制进对方网盘的根目录（同一份内容只占一份磁盘），对方在「快传 → 收到」里能看到来源和附言，未读会提示",
      "取件码：给文件生成 6 位码，谁拿到都能在 /pickup 输码下载，可设有效期与下载次数，过期、用完或撤销即失效；只支持文件，文件夹请用分享链接",
      "新增两个免登录页面：/pickup 取件、/drop/<token> 手机上传；上传页自带二维码、分片续传与已传列表，375–390px 窄屏可用",
      "扫码直传与取件码都能撤销；文件去向、撤销、发送都记审计日志；二维码用内置的 vendor 库生成，不依赖外网",
    ],
  },
  {
    version: "0.3.67",
    title: "镜像自动发布了",
    items: [
      "新增 GitHub Actions 工作流：打 tag 推上去就自动构建镜像并推到 Docker Hub 的 minecraftmc22/cloudnetp，不用再自己 build",
      "一次构建同时出 linux/amd64 与 linux/arm64，NAS 和树莓派也能直接拉；预发布 tag（带 - 的）不会顶掉 latest",
      "手动运行也支持：在 Actions 里点 Run workflow，自己填镜像 tag（留空取版本号），并决定要不要更新 latest",
      "新增 docker-compose.hub.yml：不改任何配置，docker compose -f docker-compose.hub.yml up -d 就能拉起发布好的镜像，固定版本用 IMAGE_TAG=0.3.67",
      "没配 Docker Hub 密钥时会跳过推送、只构建镜像，方便先验证 Dockerfile；README 与文档站同步补上镜像地址、tag 规则和发布步骤",
    ],
  },
  {
    version: "0.3.66",
    title: "可以用 Docker 装了",
    items: [
      "新增 Dockerfile 与 docker-compose.yml：写好 .env 后 docker compose up -d --build 就能起服务，机器上不用装 Python",
      "数据统一落在 /data：SQLite 库与上传内容只要把宿主机 ./data 挂进去就持久化，镜像里已把 /app/dev.db、/app/storage 指过去",
      "另附 docker-compose.mysql.yml：应用 + MySQL 8.4，数据库存在 mysql-data 卷里，等 MySQL 健康后自动启动",
      "容器以非 root（uid 10001）运行并自带 healthcheck；构建默认走阿里云 PyPI 镜像，海外可用构建参数换源",
      "README 补齐 Docker 部署一节：配置准备、端口、数据备份，以及权限、证书路径、反向代理这些常见坑",
    ],
  },
  {
    version: "0.3.65",
    title: "分享页能选转存位置了",
    items: [
      "分享页转存时不再只能存到根目录：弹窗里可以直接浏览自己的文件夹，点进去选目标目录，面包屑随时回退",
      "文件与文件夹目录分开：选择器里只列文件夹，不会把文件名混进来；空目录会给一句提示，确认按钮是「保存到这里」",
      "存完的提示会写清楚存到了哪：例如「已保存到 我的文件 / 接收文件夹 / 子目录」",
      "分享页右上角新增账号入口：未登录时给出登录与注册按钮，登录后换成自己的头像与昵称（点击回到网盘）",
      "从分享页点登录 / 注册，登录成功后自动跳回原来那条分享，提取码不会丢；登录页也支持 ?mode=register 直接落在注册页签",
    ],
  },
  {
    version: "0.3.64",
    title: "更新记录补全了",
    items: [
      "补上 v0.3.57 ~ v0.3.63 的更新记录：看图权限、AI 默认接口与总开关、WebDAV 开关、对话重发/编辑/删除、站内公告、滚轮缩放，以及公告留空压扁页面的修复",
      "新增 scripts/changelog_check.py：发版时若不补记录会直接报错（最新一条必须等于 config.VERSION，版本号不重复、从新到旧、每条都要有说明）",
      "关于页的更新记录默认展示最近 4 条，点「展开全部」可以看到全部 67 个版本",
    ],
  },
  {
    version: "0.3.63",
    title: "公告留空不再压扁页面",
    items: [
      "修：后台把公告清空后，主界面的导航与文件区只画到屏幕上半截，下面露出一大片壁纸",
      "原因是外壳的三行栅格靠自动排布，公告不占位时内容区掉进了「按内容撑高」的那一行，撑满屏幕的那行反而空着",
      "现在标题栏 / 公告 / 内容各自点名行号，公告在不在都不影响内容区吃满整屏",
    ],
  },
  {
    version: "0.3.62",
    title: "公告看得见了，滚轮也能调大小",
    items: [
      "后台填的公告现在显示在两处：登录卡片里（标题下方）与主界面标题栏下方一整条，切到任何视图都在",
      "超过三行折叠成「展开 / 收起」；点叉关掉后按内容记在本机，管理员改了文字会重新出现一次",
      "后台点保存后公告条立刻更新，不用刷新；清空公告整条收起、不占高度",
      "看板娘：鼠标停在她身上滚轮上下即可缩放（一格约 5%），左侧弹一个「104%」提示；设置 → 装饰 里可以关掉这个开关",
      "顺手修：设置页的看板娘大小滑块会跟着滚轮变化同步",
    ],
  },
  {
    version: "0.3.61",
    title: "重发按钮只留图标",
    items: [
      "对话失败时提问左边的「重新发送」改成 26px 的圆形图标按钮，不再显示文字",
      "文字只留在无障碍标签里（读屏仍能读到）；触屏设备上放大到 32px 方便点",
    ],
  },
  {
    version: "0.3.60",
    title: "对话可以重发、改错、删掉",
    items: [
      "对话失败（接口报错，或手动停止且一个字都没收到）时，那条提问左边亮出重发按钮，空回答的泡泡直接收掉",
      "每条气泡下方有「编辑 / 删除」：编辑把内容与图片放回输入框，改完发送即替换原来那一轮；点「取消」不动历史",
      "删除要点两下确认（第二下 3 秒内有效），删提问时连带删掉它的回答",
      "删掉某条回答后再发消息，会自动合并相邻的同角色内容——部分接口（如 Anthropic）不接受连续同角色",
    ],
  },
  {
    version: "0.3.59",
    title: "WebDAV 可以按人开关",
    items: [
      ".env 新增 WEBDAV_ENABLED 总开关：管理后台「站点」里可改，后台的值优先；关闭后所有账号的 /dav 一律 404",
      "每个用户也能在「设置 → WebDAV」里单独关掉：关闭后只有本人 403，别人不受影响，网盘文件不动",
      "「连接方法」弹窗补了一条排障提示：资源管理器报「找不到网络名」时，先确认 Windows 的 WebClient 服务在运行",
    ],
  },
  {
    version: "0.3.58",
    title: "AI 总开关与管理员默认接口",
    items: [
      ".env 新增 AI_ENABLED 与 AI_DEFAULT_*（地址、密钥、模型、协议、图片、思考、工具、输入输出上限）",
      "管理员提供默认接口后，没自己配接口的用户打开就能聊；前端只显示默认模型名，地址与密钥不下发",
      "默认接口只提供凭据与能力，读内容 / 写文件 / 移动等权限与工具轮次始终听用户自己的",
      "管理后台新增「看板娘 AI 默认接口」卡片，可改可清空，密钥只回掩码；关掉 AI 总开关后对话入口隐藏、相关接口 403",
      "设置 → 装饰 新增「重置看板娘位置」：一键回到右下角，大小与模型不变",
    ],
  },
  {
    version: "0.3.57",
    title: "「查看文件内容」也能看图",
    items: [
      "勾选「查看文件内容」+「图片输入」后，CDP娘 能直接看网盘里的 png / jpg / webp / gif",
      "小图原样发送（GIF 动画不丢帧），超限自动缩到长边 1568 转 JPEG；透明底先铺白，避免客户端渲成黑底",
      "上限：文件 20MB、像素 8000 万；SVG 会提示先转成 PNG 再看",
      "勾了看内容但没开图片输入时执行前直接拒绝，不会把图塞给不支持视觉的接口",
      "读正文与看图同样会记审计日志",
    ],
  },
  {
    version: "0.3.56",
    title: "路径式链接不再 404",
    items: [
      "前端是 hash 路由（/#/admin），但地址栏可能被写成 /admin 或 /admin#/admin（旧书签、外部链接、手敲）",
      "现在 /admin、/files、/settings、/shares、/trash、/about、/feedback 都会 302 到对应的 /#/视图",
      "跳转是 302（临时），不会被浏览器长期缓存；未知路径仍然是 404，/api、/terms、/s/<token> 不受影响",
    ],
  },
  {
    version: "0.3.55",
    title: "读文件内容也要授权了",
    items: [
      "设置 → 看板娘 · 对话设置 → 数据操作权限：新增「查看文件内容」开关，默认关闭",
      "不勾选时 CDP娘 只能看目录结构、文件名、大小和修改时间——模型手里根本没有读正文的工具",
      "勾选后她才能读取 txt / md / 代码等纯文本正文（最多 4000 字），服务端执行前还会再校验一次权限",
      "「工具调用」的说明同步改成「不读正文」，避免和新的读内容权限混淆",
      "系统提示词补了一句：没有权限时不要假装读过文件内容",
    ],
  },
  {
    version: "0.3.54",
    title: "歌词库配置搬进后台了",
    items: [
      "管理后台 → 系统设置新增「歌词来源」卡片：网易云 / 歌词库两个开关，以及歌词库地址、API 密钥、站点 Cookie",
      "密钥与 Cookie 加密存进 system_settings（purpose=lyrics），接口只回掩码；「测试连接」用当前填写的值试搜一首，测试不保存",
      "优先级：后台保存的 > .env > 内置默认。每个字段会标出来源，留空表示继续跟随 .env，清空用「清除」勾选",
      "配置保存后立刻生效，不用重启；来源顺序改变时播放器「搜索指定」的分类会跟着变",
      "修：.env 里只填了歌词库密钥、没写 LYRICS_PROVIDERS 时，之前只启用网易云，搜出来的分类就只剩一个；现在配了密钥就默认两个都启用（写 LYRICS_PROVIDERS 仍以显式配置为准）",
      "改歌词配置会清掉搜索/歌词缓存，并记审计（修改歌词来源）",
    ],
  },
  {
    version: "0.3.53",
    title: "网易云可以在后台扫码登录了",
    items: [
      "管理后台 → 系统设置新增「网易云歌词」卡片：点扫码登录，用网易云音乐 App 扫一下并确认，服务端自动取回登录票据",
      "Cookie 全程不进浏览器：接口只回连接状态、昵称、头像与更新时间，不返回 Cookie 原文或名字；歌词请求由后端代发",
      "扫码拿到的 Cookie 用 SECRET_KEY 派生的 Fernet 加密存进 system_settings，重启后自动读回内存，取歌词不用查库",
      "优先级：后台登录的 > .env 的 NETEASE_COOKIE；退出登录只清后台那份，会自动回退到 .env 并在卡片上写明来源",
      "二维码由服务端画成 SVG（新增 qrcode 依赖），3 分钟有效；过期会提示重新获取，轮询状态：等待扫码 / 已扫码待确认 / 成功",
      "登录与退出都记审计（网易云扫码登录 / 退出网易云账号）",
      "加密逻辑抽到 app/secret_box.py，AI 密钥沿用原盐值（purpose=ai），已存的密钥照常可解",
    ],
  },
  {
    version: "0.3.52",
    title: "歌词来源可以同时开两个",
    items: [
      "新增 LYRICS_PROVIDERS：逗号分隔列出要启用的来源（如 netease,lyriclib），顺序就是自动匹配的优先级；留空仍按老的 LYRICS_PROVIDER 走，all 表示全开",
      "歌词设置 → 搜索指定：搜索框上方多了来源分类（网易云 / 歌词库），点哪个搜哪个；只启用一个来源时不显示分类，选过的分类记在浏览器本地",
      "自动匹配改成按顺序尝试多个来源：某个来源报错或没结果就继续试下一个，全部失败才提示",
      "每首歌的偏好记住来源；把某个来源从配置里去掉后，老歌单里指向它的歌仍能取到歌词",
      "歌词库新增 LYRICS_API_COOKIE（站点登录态），与网易云的 NETEASE_COOKIE 各自独立；密钥与 Cookie 都只留在服务端",
      "修：点来源分类时歌词面板会自己关掉（重绘把被点按钮摘出 DOM，导致「点在外面」的误判）",
      "修：上游直接掐断连接时歌词接口漏成 500，现在统一按「服务暂时不可用」返回",
    ],
  },
  {
    version: "0.3.51",
    title: "网易云歌词（逐字 / 逐行）",
    items: [
      "在线歌词默认改走网易云接口：免密钥即可搜索与拿逐行歌词（lrc），原来的自建歌词库改为 LYRICS_PROVIDER=lyriclib 可选",
      "配置 NETEASE_COOKIE（网页版 Cookie 里的 MUSIC_U=...）后升级为逐字歌词（yrc）并带翻译；不配置时自动退回逐行，Cookie 只存服务端",
      "yrc 逐字时间轴完整解析（含间奏行与元数据行），翻译按行起始时间对齐到逐字行上",
      "「搜索指定」的每首歌偏好记录来源（netease / lyriclib），老库自动补列，旧偏好仍按歌词库解析",
      "播放器底部信息栏显示来源标签（内嵌 / 自定义 / 网易云·逐字 / 网易云·逐行 / 歌词库），搜索结果带来源角标",
    ],
  },
  {
    version: "0.3.50",
    title: "协议与隐私政策",
    items: [
      "新增 /terms（用户协议）与 /privacy（隐私政策）：独立页面，未登录也能打开，正文按本站真实实现写（自托管、scrypt 密码、审计日志、AI 对话会把内容发给所配置的服务商）",
      "登录与注册表单加「已阅读并同意《用户协议》和《隐私政策》」勾选：未勾选提交会抖动提示并中断，链接新窗口打开、不影响已填内容",
      "注册接口增加 agree 字段校验：绕过前端直接调接口也会被 400 挡下",
      "两页带自动生成的小节目录与序号，站点名、联系邮箱取 /api/meta；界面语言为英文时在顶部给一行「以中文版本为准」的说明，正文按既定策略只保留中文",
    ],
  },
  {
    version: "0.3.49",
    title: "内置提示词收回服务端",
    items: [
      "设置页去掉「填入默认」按钮：内置系统提示词不再通过接口下发，管理员在页面上只能看到并编辑自己写的那一版",
      "提示词留空即用内置默认（中英各一版，跟着界面语言换），这一步在服务端完成，前端拿不到正文",
    ],
  },
  {
    version: "0.3.48",
    title: "README 有图了",
    items: [
      "README 顶部换成图标 + 徽章，新增「界面预览」：文件管理、预览、设置、后台、分享页、登录页与手机端共八张截图",
      "截图由 scripts/make_readme_shots.py 生成：临时库 + 临时存储里铺一套演示数据，Playwright 逐个场景抓图后自动清理",
      "修掉审计日志在中文界面显示「审计动作:登录」这种前缀的问题（key 只用于查字典，不再漏到界面上）",
    ],
  },
  {
    version: "0.3.47",
    title: "一条命令启动，提示词跟着语言走",
    items: [
      "新增 start.sh（Linux / Ubuntu / macOS）与 start.ps1 / start.bat（Windows）：首次运行自动建 .venv、装依赖、从 .env.example 生成 .env 并补一个随机 SECRET_KEY，之后一条命令启动",
      "依赖只在 requirements.txt 变化时重装（按 sha256 打标记）；默认走阿里云 PyPI 镜像，可用 PIP_INDEX_URL 覆盖",
      "start.sh --dev 与 start.ps1 -Dev 走开发入口：热重载，只监视 app/ 与 static/，上传文件不会触发重启",
      "内置系统提示词拆成中英两版，跟着界面语言切换：实际发给模型的人设与运行时信息都会切",
      "提示词留空即用内置默认；管理员自己改过的提示词属于用户内容，保持原样不翻译",
      "README 的快速开始改成「脚本」+「手动」两条路径，修正原来 .venv/Scripts/python 在 Linux 下不可用、以及建了 venv 却仍用系统 python 的问题",
    ],
  },
  {
    version: "0.3.46",
    title: "分享页也会说英文了",
    items: [
      "公开分享页（/s/…）接入 i18n：提取码门槛、单文件、合集、文件夹浏览、举报与转存弹窗、下载剩余次数全部跟着语言走",
      "分享页是独立入口，不依赖登录态：未登录按本机 → 浏览器语言兜底；登录用户打开别人的分享时，语言以账号里存的为准",
      "合集分享的「N 个项目」是后端拼的标题，改成前端用 itemCount 按当前语言重拼，不再出现中文标题配英文列表",
      "后端返回的 detail 也过一遍 t()：字典里有对应中文 msgid 就翻（如「提取码不正确」），没有就原样显示，不会把错误吞掉",
      "补掉预览器、Office/ODF 表格提示、上传面板、歌词面板里被插值劈开、之前扫不到的句子",
      "新增 scripts/i18n_html_check.py（扫 H 模板与 innerHTML 里漏译的静态文案）、i18n_add.py（追加译文时自动跳过已有 key、写完校验 JSON）、i18n_dedupe.py，并把重复 key 检查并进 i18n_check.py",
      "关于页的更新记录正文仍以中文原文为准，英文界面在卡片顶部给出一行说明",
    ],
  },
  {
    version: "0.3.45",
    title: "管理后台也翻成英文了",
    items: [
      "管理后台 6 个页签（概览 / 用户 / 举报 / 反馈 / 审计 / 设置）全部接入 i18n：卡片标题、表格列头、按钮、确认框、Toast 一起翻",
      "弹窗、右键菜单、Toast 改成入口统一过 t()：各处只写中文原文，字典里有就翻、没有就原样显示，不用每个调用点自己包一层",
      "新增 scripts/i18n_check.py：扫源码里 t(\"...\") 用到的 key，缺哪条直接报行号；scripts/i18n_strings.py 列出还没被 t()/H 包住的中文字符串",
      "补掉页脚文案、WebDAV 帮助弹窗、审计动作名（46 条）、举报预览类型名、看板娘对话、预览器与歌词设置的残留中文，字典从 296 条扩到 750 条",
      "审计动作改用「审计动作:原名」作为 key，避免「重命名」「取消分享」这类词和按钮文案撞车翻错",
      "分享页（/s/…，独立入口）与关于页的历史更新记录仍是中文，属于下一批",
    ],
  },
  {
    version: "0.3.44",
    title: "界面可以切英文了",
    items: [
      "新增 简体中文 / English 两套界面：设置 → 外观 → 界面语言，登录页右上角也能直接切",
      "语言存在账号里（user_settings.language），换设备自动跟随；未登录时按本机记忆 → 浏览器语言 → 中文的顺序兜底",
      "翻译只作用于界面文案：模板里的静态文字走 H 标记，JS 里的句子走 t()，插值进来的文件名、昵称等用户数据永远原样保留",
      "已覆盖：导航与顶栏、文件列表、设置页（含看板娘 · 对话设置）、回收站、分享、关于、登录/注册/找回密码",
      "管理后台与部分二级弹窗仍是中文，补字典即可（scripts/i18n_harvest.py 会列出还没翻的条目）",
    ],
  },
  {
    version: "0.3.43",
    title: "管理页能直接看库、换库了",
    items: [
      "管理后台 → 系统设置新增「数据库」卡片：当前服务库、主库连接串（密码已脱敏）、探活延迟、容灾副本大小与镜像状态、看门狗参数一目了然",
      "「刷新配置」重读 .env 里的 DATABASE_URL 与容灾参数，重建连接池并重新探活，不用重启服务",
      "「重新连接 MySQL」丢弃旧连接池重新握手；当前跑在 SQLite 时会先把本地数据回迁，成功才切回",
      "「重新连接 SQLite」切到本地容灾库（切前先镜像一份最新副本）；切过来后暂停自动回切，避免刚点完就被看门狗改回去",
      "设置 → 对话设置底部新增「保存配置」按钮，可一次性提交整块 AI 配置",
    ],
  },
  {
    version: "0.3.42",
    title: "工具轮次上限可以在设置里调",
    items: [
      "设置 → 对话设置新增「工具轮次上限」：3 / 5 / 8 / 12 / 20 轮快捷项，或自己填 1–50",
      "上限跟着账号走（存在 ai_config 里），改完立即生效；到上限仍会收回工具让模型收尾",
      "工具胶囊按调用 id 区分，多轮调用同一个工具会分别列出来",
      "冒烟脚本改用系统分配的随机端口，避免本机已有 mock 占着 8899 时断言莫名全红",
    ],
  },
  {
    version: "0.3.41",
    title: "工具调用不再聊到一半断掉",
    items: [
      "工具往返上限从 3 轮提到 8 轮（一轮里模型可以同时调多个工具）",
      "到达上限时不再直接结束：最后一轮收回工具，让模型基于已有结果给出答复",
      "同时对用户说明「工具调用已达上限，先按已有结果回答」，不会莫名其妙停住",
    ],
  },
  {
    version: "0.3.40",
    title: "对话框跟着看板娘走",
    items: [
      "修操作条小图标：之前云朵的上下浮动动画套在了 #mascot 里所有 svg 上，锁/对话/关闭三个图标会一起飘，现在动画只作用于云朵",
      "按钮改成 28px 高、左右各 12px、图标与文字 6px，图标严格垂直居中",
      "打开对话框后拖动看板娘，对话框会跟着一起移动；左边放不下自动翻到她右侧，始终留在视口内",
      "名字统一回 CDP娘（默认系统提示词里的自称也改回来了）",
    ],
  },
  {
    version: "0.3.39",
    title: "CDP娘上岗：能动手整理文件了",
    items: [
      "默认系统提示词换成「CDP娘」人设：元气数据管家、整理强迫症、碰到安全问题秒变硬核保镖",
      "新增写工具：新建文件夹、写/改文本文件、改名、复制、移动；不勾权限时模型手里根本没有这些工具",
      "设置 → 对话设置新增「数据操作权限」两个开关，默认关闭；每次写操作都会在对话里标成琥珀色并记进审计日志",
      "覆盖同名文本文件会先说明，二进制文件不会被覆盖；复制与移动沿用现有的配额、层级与重名处理",
    ],
  },
  {
    version: "0.3.38",
    title: "设置换成 WinUI 3 观感，手机底栏瘦身",
    items: [
      "设置页改成 Win11 的分组样式：分组标题放在卡片外，卡身 8px 圆角、64px 行高、发丝分隔线，输入框与按钮统一 4px 圆角",
      "手机底部标签栏只留图标，高度从 62px 收到 52px，选中项用顶部小横条提示，文字留给读屏",
      "「意见反馈」从侧栏移到关于页，侧栏更清爽；关于页补了「提交 Issue」入口",
      "看板娘正式定名 CDP娘，对话面板、系统提示词与各处文案同步",
    ],
  },
  {
    version: "0.3.37",
    title: "看板娘会聊天了",
    items: [
      "悬停看板娘出现「锁定 / 对话 / 关闭」：锁定后鼠标穿透，关闭会记进用户设置",
      "对话面板走 SSE 流式输出，思考过程单独折叠，工具调用显示成可展开的小胶囊",
      "设置 → 看板娘 → 对话设置：自填 OpenAI 兼容 / Anthropic 接口，密钥加密落库、接口只回掩码",
      "填完密钥自动探测模型列表，可选输入/输出上限（32K–256K）、图片输入与思考模式",
      "系统提示词由管理员在设置里维护，全站生效",
    ],
  },
  {
    version: "0.3.36",
    title: "重置密码也要过强度校验",
    items: [
      "找回密码第 3 步新增密码强度条与四条规则勾选，和注册页保持同一套标准",
      "新增「确认新密码」输入框，两次不一致直接拦下，不会白跑一次接口",
      "密码规则抽成共用模块（static/js/password.js），注册 / 找回 / 修改密码三处统一，弱密码提示与后端逐字一致",
      "修复步骤切换后提交按钮文案被还原成上一步的问题",
    ],
  },
  {
    version: "0.3.35",
    title: "自动更新与手动更新",
    items: [
      "后台「版本更新」新增自动更新开关：每 6 小时检查一次，发现新版本自动拉取并重启（默认关闭）",
      "新增「立即更新」：确认后执行 git fetch + fast-forward 合并，依赖有变化才 pip install，然后重启服务",
      "安全限制：只支持 main 分支、工作区必须干净；不会覆盖本地提交，失败会保留原代码并提示原因",
      "开发热重载模式交给 uvicorn 自动重启；生产模式用独立 helper 等端口释放后拉起 run.py",
    ],
  },
  {
    version: "0.3.34",
    title: "意见反馈页",
    items: [
      "新增「意见反馈」页：可选建议 / 问题 / 其他，填写标题、内容与联系方式，提交后能看到处理状态",
      "同账号 45 秒内只能提交一条，避免误触和刷屏；页面会显示管理员配置的邮箱 / QQ 作为备用联系方式",
      "管理后台新增「意见反馈」标签：待处理 / 已处理 / 全部筛选，可标记已处理、重新打开或删除",
      "顺手修复后台概览请求晚到会覆盖刚切换标签页的竞态",
    ],
  },
  {
    version: "0.3.33",
    title: "空白处右键新建文件夹",
    items: [
      "文件列表的空白区域右键，弹出「新建文件夹 / 上传文件 / 刷新」菜单，菜单顶部显示当前目录",
      "右键文件或文件夹本身仍是原来的文件菜单，不会误触；搜索状态下会提示新建位置在根目录",
      "新建流程沿用原来的名称校验与提示，创建后自动刷新当前目录",
    ],
  },
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

  root.innerHTML = H`
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
              ${fallback ? t("降级运行 · SQLite") : t("主库 · MySQL")}
            </span>
          </div>
        </section>

        <section class="about-card">
          <div class="about-card__head"><svg class="icon icon--s"><use href="#i-star"/></svg>主要能力</div>
          <div class="about-feats">
            ${FEATURES.map(
              (f) => H`
              <div class="about-feat" style="--tone:${f.tone}">
                <span class="about-feat__icon"><svg class="icon"><use href="#${f.icon}"/></svg></span>
                <div>
                  <div class="about-feat__title">${t(f.title)}</div>
                  <div class="about-feat__desc">${t(f.desc)}</div>
                </div>
              </div>`
            ).join("")}
          </div>
        </section>

        <section class="about-card">
          <div class="about-card__head"><svg class="icon icon--s"><use href="#i-code"/></svg>技术栈</div>
          <div class="about-stack">
            ${STACK.map(
              ([name, role]) => H`
              <div class="about-stack__item">
                <span class="about-stack__name">${t(name)}</span>
                <span class="about-stack__role">${t(role)}</span>
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
            <a class="about-lrow" href="#/feedback">
              <span class="about-lrow__icon"><svg class="icon"><use href="#i-mail"/></svg></span>
              <span class="about-lrow__body">
                <span class="about-lrow__label">意见反馈</span>
                <span class="about-lrow__value">遇到问题或有想法，站内提一条给管理员</span>
              </span>
              <svg class="icon icon--s about-lrow__go"><use href="#i-chevron"/></svg>
            </a>
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
                <span class="about-lrow__label">提交 Issue</span>
                <span class="about-lrow__value">Bug 或改进建议也可以直接发到仓库</span>
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
            <span class="about-card__count">${t("{n} 个版本", { n: CHANGELOG.length })}</span>
          </div>
          ${getLang() === "zh-CN" ? "" : `<div class="about-card__note">${t("更新记录的正文只提供中文原文。")}</div>`}
          <ol class="about-log">
            ${CHANGELOG.map(
              (c, i) => H`
              <li class="about-log__item ${i >= VISIBLE ? "is-extra" : ""} ${i === 0 ? "is-latest" : ""}">
                <span class="about-log__dot"></span>
                <div class="about-log__body">
                  <div class="about-log__head">
                    <span class="about-log__ver">v${c.version}</span>
                    <span class="about-log__title">${c.title}</span>
                  </div>
                  <ul class="about-log__items">
                    ${c.items.map((t) => H`<li>${t}</li>`).join("")}
                  </ul>
                </div>
              </li>`
            ).join("")}
          </ol>
          <button class="about-more" type="button" data-act="more">
            <span data-more-text>${t("展开全部 {n} 个版本", { n: CHANGELOG.length })}</span>
            <svg class="icon icon--s"><use href="#i-chevron-down"/></svg>
          </button>
        </section>

        <footer class="about-foot">
          <span>${t("© 2026 {app} · 数据存放在你自己的服务器上", { app: escapeHtml(appName) })}</span>
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
      updateStatus.innerHTML = H`<span class="update-state">正在检查 GitHub…</span>`;
    }
    try {
      const data = await checkForUpdates(true);
      if (updateStatus) updateStatus.innerHTML = updateResultHtml(data);
    } catch (err) {
      if (updateStatus) {
        updateStatus.innerHTML = `<span class="update-state is-error">${t("检查失败：{msg}", {
          msg: escapeHtml(err.message || t("网络不可达")),
        })}</span>`;
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
    moreText.textContent = open ? t("收起") : t("展开全部 {n} 个版本", { n: CHANGELOG.length });
    if (!open) more.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
}
