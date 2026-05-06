from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    PROJECT_NAME: str = "Project Helper"
    DATABASE_URL: str = "sqlite:///./project_helper.db"
    DEEPSEEK_API_KEY: Optional[str] = None
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    REPO_STORAGE_PATH: str = "./repos"
    
    # Firecrawl & Context7 (Mocked or placeholders for now)
    FIRECRAWL_API_KEY: Optional[str] = None
    CONTEXT7_API_KEY: Optional[str] = None

    class Config:
        env_file = ".env"

settings = Settings()
