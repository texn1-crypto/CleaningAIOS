from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "CleaningAI OS"
    environment: str = "development"
    database_url: str = "sqlite:///./cleaningai.db"
    log_format: str = "json"
    log_level: str = "INFO"
    release_sha: str = "development"
    build_time: str = "unknown"
    api_key: str = "development-only-change-me"
    manager_api_key: str = ""
    operator_api_key: str = ""
    viewer_api_key: str = ""
    owner_telegram_id: str = ""
    owner_telegram_chat_id: str = ""
    telegram_bot_token: str = ""
    telegram_bot_api_base_url: str = ""
    telegram_cloud_download_limit_bytes: int = 20_000_000
    telegram_startup_max_attempts: int = 4
    telegram_startup_retry_seconds: float = 5.0
    telegram_callback_secret: str = ""
    owner_notification_max_attempts: int = 10
    owner_notification_retry_max_seconds: int = 15 * 60
    approval_ttl_hours: int = 24
    public_base_url: str = "http://localhost:8000"
    crm_public_url: str = ""
    twenty_enabled: bool = False
    twenty_base_url: str = "http://host.docker.internal:3020"
    twenty_api_key: str = ""
    twenty_sync_interval_minutes: int = 15
    twenty_sync_batch_size: int = 25
    twenty_timeout_seconds: float = 15.0
    twenty_max_response_bytes: int = 128_000
    internal_api_url: str = ""
    worker_poll_seconds: float = 2.0
    agent_worker_replicas: int = 4
    scheduler_interval_seconds: int = 60
    system_admin_interval_minutes: int = 5
    system_admin_report_interval_minutes: int = 2 * 60
    system_admin_stale_task_minutes: int = 15
    agent_slo_window_hours: int = 24
    agent_slo_success_rate_percent: float = 95.0
    agent_slo_p95_duration_seconds: float = 300.0
    agent_stale_run_minutes: int = 15
    agent_read_tool_max_calls_per_run: int = 4
    agent_read_tool_timeout_seconds: float = 35.0
    agent_read_tool_total_timeout_seconds: float = 45.0
    agent_read_tool_max_result_bytes: int = 64_000
    agent_mcp_read_servers_json: str = "[]"
    agent_mcp_protocol_version: str = "2026-07-28"
    crawl4ai_enabled: bool = False
    crawl4ai_base_url: str = "http://crawl4ai:11235"
    crawl4ai_api_token: str = ""
    crawl4ai_timeout_seconds: float = 30.0
    crawl4ai_max_response_bytes: int = 2_000_000
    crawl4ai_default_content_chars: int = 12_000
    crawl4ai_max_content_chars: int = 24_000
    openjarvis_enabled: bool = False
    openjarvis_base_url: str = "http://host.docker.internal:8011"
    openjarvis_api_key: str = ""
    openjarvis_model: str = "qwen3:0.6b"
    openjarvis_timeout_seconds: float = 120.0
    openjarvis_max_prompt_chars: int = 4_000
    openjarvis_max_response_chars: int = 3_500
    openjarvis_max_response_bytes: int = 128_000
    telephony_enabled: bool = False
    telephony_gateway_url: str = ""
    telephony_api_token: str = ""
    telephony_webhook_secret: str = ""
    telephony_timeout_seconds: float = 20.0
    telephony_max_response_bytes: int = 64_000
    telephony_calls_per_minute: int = 1
    telephony_calls_per_day: int = 20
    telephony_timezone: str = "Europe/Moscow"
    telephony_daily_start_hour: int = 10
    telephony_daily_end_hour: int = 18
    ceo_review_interval_hours: int = 24
    ceo_weekly_brief_timezone: str = "Europe/Moscow"
    ceo_weekly_brief_weekday: int = 0
    ceo_weekly_brief_hour: int = 9
    owner_activity_report_interval_minutes: int = 60
    daily_owner_pack_timezone: str = "Europe/Moscow"
    daily_owner_pack_hour: int = 18
    marketing_budget_advisor_timezone: str = "Europe/Moscow"
    marketing_budget_advisor_daily_hour: int = 17
    marketing_budget_daily_limit_rub: float = 2_000
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
    outreach_daily_end_hour: int = 18
    outreach_min_interval_minutes: int = 30
    outreach_rate_limit_cooldown_hours: int = 24
    inbound_mail_poll_seconds: int = 60
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-5.6-terra"
    llm_provider: str = "auto"
    llm_reasoning_effort: str = "low"
    llm_timeout_seconds: int = 60
    llm_max_output_tokens: int = 1200
    llm_council_enabled: bool = True
    llm_council_min_members: int = 2
    llm_council_max_members: int = 3
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_version: str = "2023-06-01"
    anthropic_timeout_seconds: int = 60
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = "gemini-3.7-flash"
    gemini_thinking_level: str = "low"
    gemini_timeout_seconds: int = 60
    perplexity_api_key: str = ""
    perplexity_base_url: str = "https://api.perplexity.ai"
    perplexity_model: str = "sonar-pro"
    perplexity_timeout_seconds: int = 60
    perplexity_coach_interval_minutes: int = 30
    perplexity_max_improvements_per_cycle: int = 3
    perplexity_max_queued_improvements: int = 30
    management_contact_scout_interval_minutes: int = 6 * 60
    management_contact_scout_max_results: int = 20
    management_contact_regions: str = "Санкт-Петербург|Ленинградская область|Москва|Московская область"
    lead_monthly_handoff_target: int = 20
    lead_verification_batch_size: int = 24
    lead_research_batch_size: int = 8
    lead_report_timezone: str = "Europe/Moscow"
    contact_export_timezone: str = "Europe/Moscow"
    contact_export_weekday: int = 0
    contact_export_hour: int = 18
    prompt_candidate_rollout_percent: int = 0
    prompt_rollout_seed: str = "cleaningaios-v1"
    github_research_token: str = ""
    evolution_research_queries: str = (
        "multi agent orchestration stars:>=500|"
        "ai agent evaluation observability stars:>=200|"
        "sales crm automation stars:>=200|"
        "marketing automation analytics stars:>=200|"
        "business forecasting mlops stars:>=200|"
        "continuous delivery rollback stars:>=500|"
        "retrieval augmented generation evaluation stars:>=500"
    )
    evolution_research_timezone: str = "Europe/Moscow"
    evolution_research_daily_hour: int = 18
    evolution_research_max_sources_per_cycle: int = 20
    evolution_research_max_improvements_per_cycle: int = 3
    evolution_research_timeout_seconds: int = 30
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
    tender_source_freshness_slo_minutes: int = 120
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
