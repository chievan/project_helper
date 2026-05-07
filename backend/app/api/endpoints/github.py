from fastapi import APIRouter, HTTPException
import httpx
from bs4 import BeautifulSoup
from typing import List, Dict, Any
import time

router = APIRouter()

# 简单的内存缓存
cache = {
    "data": [],
    "last_updated": 0
}
CACHE_TTL = 3600  # 1小时缓存

from app.core.config import settings

@router.get("/trending")
async def get_github_trending():
    now = time.time()
    if cache["data"] and (now - cache["last_updated"] < CACHE_TTL):
        return cache["data"]

    # 支持通过配置指定加速域名或代理
    github_base = getattr(settings, "GITHUB_DOMAIN", "https://github.com")
    url = f"{github_base}/trending"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        # 尝试通过系统代理抓取
        async with httpx.AsyncClient(trust_env=True) as client:
            try:
                response = await client.get(url, headers=headers, timeout=10.0)
            except (httpx.ConnectError, httpx.TimeoutException) as ce:
                print(f"Direct fetch failed: {ce}. Trying fallback...")
                # 如果直连失败，尝试使用一个通用的公共 API 镜像 (如果可用)
                fallback_url = "https://gtrend.yapie.me/repositories"
                response = await client.get(fallback_url, timeout=10.0)
            
        if response.status_code != 200:
            if cache["data"]: return cache["data"]
            raise HTTPException(status_code=502, detail=f"GitHub reachable but returned {response.status_code}")
            
        # 如果是第三方 API 返回的是 JSON
        if "application/json" in response.headers.get("Content-Type", ""):
            data = response.json()
            results = []
            for item in data[:15]:
                results.append({
                    "owner": item.get("author"),
                    "name": item.get("name"),
                    "full_name": f"{item.get('author')}/{item.get('name')}",
                    "description": item.get("description"),
                    "language": item.get("language"),
                    "stars": str(item.get("stars")),
                    "url": item.get("url")
                })
        else:
            # 否则按原逻辑解析 HTML
            soup = BeautifulSoup(response.text, 'html.parser')
            repo_rows = soup.select('article.Box-row')
            results = []
            for row in repo_rows:
                try:
                    title_tag = row.select_one('h2 a')
                    if not title_tag: continue
                    full_name = title_tag.text.strip().replace(' ', '').replace('\n', '')
                    owner, name = full_name.split('/')
                    desc_tag = row.select_one('p')
                    description = desc_tag.text.strip() if desc_tag else ""
                    meta_div = row.select_one('div.f6.color-fg-muted.mt-2')
                    language = "Unknown"
                    if meta_div:
                        lang_tag = meta_div.select_one('span[itemprop="programmingLanguage"]')
                        if lang_tag: language = lang_tag.text.strip()
                    stars = "0"
                    if meta_div:
                        stars_tag = meta_div.select_one('a[href$="/stargazers"]')
                        if stars_tag: stars = stars_tag.text.strip()
                    results.append({
                        "owner": owner, "name": name, "full_name": full_name,
                        "description": description, "language": language,
                        "stars": stars, "url": f"https://github.com/{full_name}"
                    })
                except Exception: continue
        
        if results:
            cache["data"] = results
            cache["last_updated"] = now
        return results
        
    except Exception as e:
        import traceback
        print(f"Scraper critical error:\n{traceback.format_exc()}")
        if cache["data"]: return cache["data"]
        raise HTTPException(status_code=500, detail=str(e))
