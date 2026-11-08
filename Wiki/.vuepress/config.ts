import { viteBundler } from "@vuepress/bundler-vite";
import { defineUserConfig } from "vuepress";
import { hopeTheme } from "vuepress-theme-hope";

export default defineUserConfig({
  lang: "zh-CN",
  title: "CloudNETP",
  description: "自托管的二次元个人网盘：文件管理、在线预览、分享链接、WebDAV 与看板娘 AI",

  bundler: viteBundler(),

  theme: hopeTheme({
    hostname: "https://pan.example.com",
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
