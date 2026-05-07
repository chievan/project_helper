from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    PROJECT_NAME: str = "Project Helper"
    DATABASE_URL: str = "sqlite:///./project_helper.db"
    DEEPSEEK_API_KEY: Optional[str] = None
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    DEEPSEEK_MODEL: str = "deepseek-v4-flash"  # 默认用于报告撰写
    DEEPSEEK_ANALYSIS_MODEL: str = "deepseek-v4-flash"  # 默认用于工具分析
    REPO_STORAGE_PATH: str = "./repos"
    
    # Firecrawl & Context7 (Mocked or placeholders for now)
    FIRECRAWL_API_KEY: Optional[str] = None
    CONTEXT7_API_KEY: Optional[str] = None

    class Config:
        env_file = ".env"

settings = Settings()
