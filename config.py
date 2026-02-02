from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application configuration"""
    database_url: str = "postgresql+asyncpg://user:password@localhost:5432/articence_db"
    log_level: str = "INFO"
    max_ai_retries: int = 5
    ai_failure_rate: float = 0.25
    
    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
