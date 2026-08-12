from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "CleaningAI OS"
    environment: str = "development"
    database_url: str = "sqlite:///./cleaningai.db"
    api_key: str = "development-only-change-me"
    owner_telegram_id: str = ""
    telegram_bot_token: str = ""
    public_base_url: str = "http://localhost:8000"
    worker_poll_seconds: float = 2.0
    scheduler_interval_seconds: int = 60
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    outreach_per_minute: int = 10
    outreach_per_day: int = 100
    llm_api_key: str = ""
    tender_sources: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
