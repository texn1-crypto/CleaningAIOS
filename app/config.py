from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "CleaningAI OS"
    environment: str = "development"
    database_url: str = "sqlite:///./cleaningai.db"
    api_key: str = "development-only-change-me"
    manager_api_key: str = ""
    operator_api_key: str = ""
    viewer_api_key: str = ""
    owner_telegram_id: str = ""
    telegram_bot_token: str = ""
    public_base_url: str = "http://localhost:8000"
    internal_api_url: str = ""
    worker_poll_seconds: float = 2.0
    scheduler_interval_seconds: int = 60
    ceo_review_interval_hours: int = 24
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    unsubscribe_secret: str = ""
    outreach_per_minute: int = 10
    outreach_per_day: int = 100
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-5.6-terra"
    llm_reasoning_effort: str = "low"
    llm_timeout_seconds: int = 60
    llm_max_output_tokens: int = 1200
    workspace_agent_trigger_id: str = ""
    workspace_agent_access_token: str = ""
    workspace_agent_timeout_seconds: int = 20
    tender_sources: str = ""
    tender_source_token: str = ""
    tender_request_timeout_seconds: int = 30
    document_storage_path: str = "/data/documents"
    max_document_bytes: int = 25_000_000
    max_attachment_bytes: int = 10_000_000
    max_import_bytes: int = 10_000_000
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
