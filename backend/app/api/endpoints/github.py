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
        # httpx 会自动识别系统环境变量 HTTP_PROXY / HTTPS_PROXY
        async with httpx.AsyncClient(trust_env=True) as client:
            response = await client.get(url, headers=headers, timeout=15.0)
            
        if response.status_code != 200:
            # 如果抓取失败但有旧数据，先返回旧数据
            if cache["data"]:
                return cache["data"]
            raise HTTPException(status_code=502, detail="Failed to fetch from GitHub")
            
        soup = BeautifulSoup(response.text, 'html.parser')
        repo_rows = soup.select('article.Box-row')
        
        results = []
        for row in repo_rows:
            try:
                # Name and Owner
                title_tag = row.select_one('h2 a')
                if not title_tag: continue
                
                full_name = title_tag.text.strip().replace(' ', '').replace('\n', '')
                owner, name = full_name.split('/')
                
                # Description
                desc_tag = row.select_one('p')
                description = desc_tag.text.strip() if desc_tag else ""
                
                # Meta info (Stars, Language, etc.)
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
                    "owner": owner,
                    "name": name,
                    "full_name": full_name,
                    "description": description,
                    "language": language,
                    "stars": stars,
                    "url": f"https://github.com/{full_name}"
                })
            except Exception as e:
                print(f"Error parsing row: {e}")
                continue
        
        # 更新缓存
        if results:
            cache["data"] = results
            cache["last_updated"] = now
            
        return results
        
    except Exception as e:
        print(f"Scraper exception: {e}")
        if cache["data"]:
            return cache["data"]
        raise HTTPException(status_code=500, detail=str(e))
