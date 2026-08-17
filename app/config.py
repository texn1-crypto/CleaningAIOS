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
    owner_telegram_chat_id: str = ""
    telegram_bot_token: str = ""
    telegram_bot_api_base_url: str = ""
    telegram_cloud_download_limit_bytes: int = 20_000_000
    telegram_callback_secret: str = ""
    approval_ttl_hours: int = 24
    public_base_url: str = "http://localhost:8000"
    internal_api_url: str = ""
    worker_poll_seconds: float = 2.0
    scheduler_interval_seconds: int = 60
    system_admin_interval_minutes: int = 5
    system_admin_stale_task_minutes: int = 15
    ceo_review_interval_hours: int = 24
    owner_activity_report_interval_minutes: int = 30
    ceo_development_cadence_hours: int = 24
    growth_review_interval_hours: int = 24
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    unsubscribe_secret: str = ""
    outreach_per_minute: int = 10
    outreach_per_day: int = 7
    outreach_timezone: str = "Europe/Moscow"
    outreach_daily_start_hour: int = 9
    outreach_rate_limit_cooldown_hours: int = 24
    inbound_mail_poll_seconds: int = 60
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-5.6-terra"
    llm_provider: str = "auto"
    llm_reasoning_effort: str = "low"
    llm_timeout_seconds: int = 60
    llm_max_output_tokens: int = 1200
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_version: str = "2023-06-01"
    anthropic_timeout_seconds: int = 60
    workspace_agent_trigger_id: str = ""
    workspace_agent_access_token: str = ""
    workspace_agent_timeout_seconds: int = 20
    company_name: str = "CleaningAIOS"
    company_legal_name: str = ""
    company_inn: str = ""
    company_phone: str = ""
    company_email: str = ""
    company_address: str = ""
    company_service_area: str = "Москва и Московская область"
    privacy_contact_email: str = ""
    owner_notification_email: str = ""
    hot_lead_score: int = 70
    public_lead_rate_limit_per_hour: int = 5
    public_lead_rate_secret: str = ""
    yandex_direct_token: str = ""
    yandex_search_api_key: str = ""
    yandex_cloud_folder_id: str = ""
    vk_ads_token: str = ""
    twogis_business_token: str = ""
    avito_client_id: str = ""
    avito_client_secret: str = ""
    telegram_ads_token: str = ""
    telegram_social_chat_id: str = ""
    cleaning_news_feeds: str = "https://www.cleanlink.com/rss/cleanlink-rss.asp,https://www.cleanlink.com/rss/newsofinterest.asp"
    cleaning_news_max_age_days: int = 14
    cleaning_news_timeout_seconds: int = 20
    vk_community_id: str = ""
    vk_community_token: str = ""
    vk_api_version: str = "5.199"
    odnoklassniki_group_id: str = ""
    odnoklassniki_application_key: str = ""
    odnoklassniki_access_token: str = ""
    odnoklassniki_session_secret: str = ""
    instagram_business_account_id: str = ""
    instagram_access_token: str = ""
    social_telegram_url: str = ""
    social_vk_url: str = ""
    social_odnoklassniki_url: str = ""
    social_instagram_url: str = ""
    image_generation_api_key: str = ""
    image_generation_base_url: str = "https://api.openai.com/v1"
    image_generation_model: str = "gpt-image-2"
    image_generation_quality: str = "low"
    image_generation_size: str = "1024x1024"
    image_generation_timeout_seconds: int = 120
    image_generation_enabled: bool = False
    social_image_generation_enabled: bool = False
    video_generation_api_key: str = ""
    tender_sources: str = ""
    tender_source_token: str = ""
    tender_request_timeout_seconds: int = 30
    tender_monitor_interval_minutes: int = 60
    document_storage_path: str = "/data/documents"
    proposal_font_path: str = ""
    max_document_bytes: int = 50_000_000
    max_attachment_bytes: int = 10_000_000
    max_import_bytes: int = 10_000_000
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def public_leads_enabled(self) -> bool:
        """Production forms require an identified personal-data controller."""
        if not self.production:
            return True
        return bool(self.company_legal_name and (self.privacy_contact_email or self.company_email))

    @property
    def image_generation_configured(self) -> bool:
        """Keep the original social-only switch backward compatible."""
        return bool(
            self.image_generation_api_key
            and (self.image_generation_enabled or self.social_image_generation_enabled)
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
