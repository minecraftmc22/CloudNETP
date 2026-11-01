# -*- coding: utf-8 -*-
"""找歌词库里带 agent（对唱角色）的歌。"""
import sys

sys.path.insert(0, ".")
from app.lyrics_service import fetch_lyrics, search_songs

for q in ["Shallow", "Senorita", "Die With A Smile", "Beauty and a Beat", "Love The Way You Lie"]:
    try:
        items = search_songs(q, limit=3)
    except Exception as e:
        print(q, "搜索失败:", e)
        continue
    if not items:
        print(q, "无结果")
        continue
    for it in items[:2]:
        try:
            data = fetch_lyrics(it["id"])
        except Exception as e:
            print(q, it.get("title"), "取词失败:", e)
            continue
        lines = data.get("lines") or []
        agents = sorted({l.get("agent") for l in lines if l.get("agent")})
        print(q, "|", it.get("title"), "-", it.get("artist"), "| id:", it.get("id"), "| agent:", agents if agents else "无")
