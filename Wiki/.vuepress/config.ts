import { viteBundler } from "@vuepress/bundler-vite";
import { defineUserConfig } from "vuepress";
import { hopeTheme } from "vuepress-theme-hope";

// GitHub Pages 的仓库站点固定挂在 https://<user>.github.io/<repo>/ 下，
// 所以 base 必须是仓库名，而不是 "/"。本地 `docs:dev` 也走同一份配置，
// 预览地址会变成 http://localhost:8080/CloudNETP/，这是正常的。
const BASE = (process.env.WIKI_BASE || "/CloudNETP/") as `/${string}/`;

export default defineUserConfig({
  base: BASE,
  lang: "zh-CN",
  title: "CloudNETP",
  description: "自托管的二次元个人网盘：文件管理、在线预览、分享链接、WebDAV 与看板娘 AI",

  bundler: viteBundler(),

  theme: hopeTheme({
    hostname: "https://minecraftmc22.github.io",
    logo: "/images/logo.png",
    logoDark: "/images/logo.png",
    repo: "minecraftmc22/CloudNETP",
    docsDir: ".",
    // 文档在仓库的 Wiki/ 子目录，主题默认拼出来的编辑链接会指错，直接关掉
    editLink: false,
    navbar: [
      { text: "指南", link: "/guide/" },
      { text: "部署运维", link: "/guide/deploy.html" },
      { text: "常见问题", link: "/guide/faq.html" },
    ],
    sidebar: {
      "/guide/": "structure",
    },
    footer: "PolyForm Noncommercial License 1.0.0 · 数据放在你自己的机器上",
    displayFooter: true,
    pageInfo: ["Date", "ReadingTime", "Word"],
    markdown: {
      align: true,
      attrs: true,
      codeTabs: true,
      hint: true,
      tasklist: true,
      tabs: true,
    },
  }),
});
