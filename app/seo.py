"""站点 SEO：给首页注入 <head> 元信息，并生成 robots.txt / sitemap.xml。

为什么要在服务端拼：爬虫和社交平台的抓取器大多不执行 JS，
只认首屏 HTML 里的 title / description / og:*。所以这些标签必须在返回
index.html 时就写好，不能等前端渲染完再补。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import escape as html_escape
from xml.sax.saxutils import escape as xml_escape

from sqlalchemy.orm import Session

from . import settings_store
from .config import STATIC_DIR, settings

# 后台输入框与 API 同一套上限（描述太长会被搜索结果截断）
TITLE_MAX = 120
DESCRIPTION_MAX = 300
KEYWORDS_MAX = 200
IMAGE_MAX = 500
BASE_URL_MAX = 200

# 首页源码读一次就够，只有文件被替换（升级）时才重读
_source_cache: dict[str, object] = {"mtime": None, "text": ""}


def index_source() -> str:
    path = STATIC_DIR / "index.html"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _source_cache.get("text") or ""
    if _source_cache["mtime"] != mtime or not _source_cache["text"]:
        _source_cache["text"] = path.read_text(encoding="utf-8")
        _source_cache["mtime"] = mtime
    return str(_source_cache["text"])


def _normalize_base(raw: str) -> str:
    base = (raw or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    return base


def values(db: Session) -> dict:
    def text(key: str, fallback: str = "") -> str:
        raw = settings_store.get_value(db, key).strip()
        return raw or fallback

    site = text("site_name", settings.APP_NAME)
    base = _normalize_base(text("seo_base_url", settings.public_base))
    return {
        "site": site,
        "base": base,
        # 空 = 用 index.html 里原有的标题（站名 · 樱色云盘）
        "title": text("seo_title")[:TITLE_MAX],
        "description": text("seo_description")[:DESCRIPTION_MAX],
        "keywords": text("seo_keywords")[:KEYWORDS_MAX],
        "image": text("seo_image")[:IMAGE_MAX],
        "indexable": settings_store.get_flag(db, "seo_index"),
    }


def _meta(name: str, content: str, *, prop: bool = False) -> str:
    key = "property" if prop else "name"
    return f'<meta {key}="{name}" content="{html_escape(content, quote=True)}">'


def head_html(db: Session) -> str:
    """插在 </head> 前的那一段。首页与公开页都用它。"""
    cfg = values(db)
    title = cfg["title"] or cfg["site"]
    canonical = f"{cfg['base']}/" if cfg["base"] else ""
    parts: list[str] = [
        _meta("robots", "index,follow" if cfg["indexable"] else "noindex,nofollow"),
    ]
    if cfg["description"]:
        parts.append(_meta("description", cfg["description"]))
    if cfg["keywords"]:
        parts.append(_meta("keywords", cfg["keywords"]))
    if canonical:
        parts.append(f'<link rel="canonical" href="{html_escape(canonical, quote=True)}">')

    # 社交平台 / IM 的链接预览只读 og:*，跟 SEO 一起配最省事
    parts.append(_meta("og:type", "website", prop=True))
    parts.append(_meta("og:site_name", cfg["site"], prop=True))
    parts.append(_meta("og:title", title, prop=True))
    if cfg["description"]:
        parts.append(_meta("og:description", cfg["description"], prop=True))
    if canonical:
        parts.append(_meta("og:url", canonical, prop=True))
    if cfg["image"]:
        parts.append(_meta("og:image", cfg["image"], prop=True))
        parts.append(_meta("twitter:card", "summary_large_image"))
    else:
        parts.append(_meta("twitter:card", "summary"))
    parts.append(_meta("twitter:title", title))
    if cfg["description"]:
        parts.append(_meta("twitter:description", cfg["description"]))
    if cfg["image"]:
        parts.append(_meta("twitter:image", cfg["image"]))

    if cfg["indexable"]:
        data: dict = {
            "@context": "https://schema.org",
            "@type": "WebApplication",
            "name": title,
            "applicationCategory": "CloudStorageApplication",
            "operatingSystem": "Any",
        }
        if cfg["description"]:
            data["description"] = cfg["description"]
        if canonical:
            data["url"] = canonical
        payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
        parts.append(f'<script type="application/ld+json">{payload}</script>')
    return "\n  ".join(parts)


def render_index(db: Session) -> str:
    """把 SEO 块注入首页源码；后台填了 SEO 标题就顺手把 <title> 换掉。"""
    html = index_source()
    if not html:
        return ""
    cfg = values(db)
    if cfg["title"]:
        # 用 lambda 做替换：标题里出现反斜杠时不会被 re 当成转义序列
        html = re.sub(
            r"<title>.*?</title>",
            lambda _m: f"<title>{html_escape(cfg['title'])}</title>",
            html,
            count=1,
            flags=re.S,
        )
    block = head_html(db)
    if "<!--seo-->" in html:
        return html.replace("<!--seo-->", block, 1)
    return html.replace("</head>", f"  {block}\n</head>", 1)


def robots_txt(db: Session) -> str:
    """不收录时整站 Disallow（分享页 / 取件页无论如何都不收录）。"""
    cfg = values(db)
    if not cfg["indexable"]:
        return "User-agent: *\nDisallow: /\n"
    lines = [
        "User-agent: *",
        "Allow: /$",
        "Disallow: /api/",
        "Disallow: /static/",
        "Disallow: /assets/",
        "Disallow: /dav/",
        "Disallow: /s/",
        "Disallow: /pickup",
        "Disallow: /drop/",
        "Disallow: /settings",
        "Disallow: /admin",
    ]
    if cfg["base"]:
        lines += ["", f"Sitemap: {cfg['base']}/sitemap.xml"]
    return "\n".join(lines) + "\n"


def sitemap_xml(db: Session) -> str | None:
    """只有开了收录、且知道公开地址时才给 sitemap（前端是 hash 路由，能列的只有静态页）。"""
    cfg = values(db)
    if not cfg["indexable"] or not cfg["base"]:
        return None
    pages = [
        ("/", STATIC_DIR / "index.html"),
        ("/terms", STATIC_DIR / "terms.html"),
        ("/privacy", STATIC_DIR / "privacy.html"),
    ]
    rows: list[str] = []
    for path, source in pages:
        try:
            lastmod = datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).strftime(
                "%Y-%m-%d"
            )
        except OSError:
            lastmod = ""
        changefreq = "daily" if path == "/" else "monthly"
        rows.append(
            "  <url>"
            f"<loc>{xml_escape(cfg['base'] + path)}</loc>"
            + (f"<lastmod>{lastmod}</lastmod>" if lastmod else "")
            + f"<changefreq>{changefreq}</changefreq>"
            + "</url>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(rows)
        + "\n</urlset>\n"
    )
