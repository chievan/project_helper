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
    
    # 终极兜底数据 (如果网络完全不通)
    mock_data = [
        {"owner": "deepseek-ai", "name": "DeepSeek-V3", "full_name": "deepseek-ai/DeepSeek-V3", "description": "The strongest open-source model.", "language": "Python", "stars": "120k+", "url": "https://github.com/deepseek-ai/DeepSeek-V3"},
        {"owner": "open-webui", "name": "open-webui", "full_name": "open-webui/open-webui", "description": "User-friendly WebUI for LLMs.", "language": "TypeScript", "stars": "45k+", "url": "https://github.com/open-webui/open-webui"},
        {"owner": "langchain-ai", "name": "langchain", "full_name": "langchain-ai/langchain", "description": "Building applications with LLMs through composability.", "language": "Python", "stars": "92k+", "url": "https://github.com/langchain-ai/langchain"}
    ]

    try:
        results = []
        async with httpx.AsyncClient(trust_env=True) as client:
            # 尝试列表 (GitHub 直连 -> 镜像1 -> 镜像2)
            targets = [
                (url, "html"),
                ("https://gtrend.yapie.me/repositories", "json"),
                ("https://api.gitterapp.com/repositories", "json")
            ]
            
            for target_url, mode in targets:
                try:
                    print(f"Trying to fetch trending from: {target_url}")
                    response = await client.get(target_url, headers=headers, timeout=5.0)
                    if response.status_code == 200:
                        if mode == "json":
                            data = response.json()
                            for item in data[:12]:
                                results.append({
                                    "owner": item.get("author") or item.get("username"),
                                    "name": item.get("name"),
                                    "full_name": f"{item.get('author')}/{item.get('name')}",
                                    "description": item.get("description"),
                                    "language": item.get("language") or "Unknown",
                                    "stars": str(item.get("stars") or "0"),
                                    "url": item.get("url") or f"https://github.com/{item.get('author')}/{item.get('name')}"
                                })
                        else:
                            soup = BeautifulSoup(response.text, 'html.parser')
                            repo_rows = soup.select('article.Box-row')
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
                        if results: break # 成功获取则跳出循环
                except Exception as e:
                    print(f"Fetch from {target_url} failed: {e}")
                    continue
        
        # 如果所有尝试都失败，使用 Mock 数据
        if not results:
            print("All fetch attempts failed. Using mock fallback data.")
            results = mock_data
            
        cache["data"] = results
        cache["last_updated"] = now
        return results
        
    except Exception as e:
        import traceback
        print(f"Scraper critical error:\n{traceback.format_exc()}")
        return cache["data"] if cache["data"] else mock_data
