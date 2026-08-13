from __future__ import annotations

from typing import Any

from .config import settings
from .improvements import workspace_agent_configuration_status
from .llm import llm_advisor


def integration_status() -> dict[str, Any]:
    """Return capability configuration without exposing credential values."""
    smtp_ready = all(
        [settings.smtp_host, settings.smtp_username, settings.smtp_password, settings.smtp_from_email]
    )
    return {
        "postgresql": {
            "status": "connected"
            if not settings.database_url.startswith("sqlite")
            else "development_sqlite"
        },
        "telegram": {
            "status": "configured"
            if settings.telegram_bot_token and settings.owner_telegram_id
            else "credentials_required"
        },
        "smtp_default": {"status": "configured" if smtp_ready else "credentials_required"},
        "tender_sources": {
            "status": "configured"
            if settings.tender_sources.strip()
            else "source_configuration_required",
            "sources": [x.strip() for x in settings.tender_sources.split(",") if x.strip()],
        },
        "llm": {
            "status": llm_advisor.configuration_status(),
            "provider": "openai_compatible_responses",
            "model": settings.llm_model or None,
        },
        "workspace_agent_handoff": {
            "status": workspace_agent_configuration_status(),
            "provider": "chatgpt_workspace_agents",
        },
        "owner_hot_lead_email": {
            "status": "configured"
            if smtp_ready and settings.owner_notification_email
            else "credentials_required"
        },
        "public_website": {
            "status": "ready" if settings.public_leads_enabled else "legal_profile_required",
            "lead_form_enabled": settings.public_leads_enabled,
        },
        "marketing_channels": {
            "yandex": "credentials_present_adapter_manual"
            if settings.yandex_direct_token
            else "credentials_required",
            "vk_ads": "credentials_present_adapter_manual"
            if settings.vk_ads_token
            else "credentials_required",
            "2gis": "credentials_present_adapter_manual"
            if settings.twogis_business_token
            else "credentials_required",
            "avito": "credentials_present_adapter_manual"
            if settings.avito_client_id and settings.avito_client_secret
            else "credentials_required",
            "telegram_ads": "credentials_present_adapter_manual"
            if settings.telegram_ads_token
                else "credentials_required",
        },
        "social_publishing": {
            "telegram": "credentials_present_adapter_required"
            if settings.telegram_bot_token and settings.telegram_social_chat_id
            else "credentials_required",
            "vk": "credentials_present_adapter_required"
            if settings.vk_community_id and settings.vk_community_token
            else "credentials_required",
            "odnoklassniki": "credentials_present_adapter_required"
            if settings.odnoklassniki_group_id and settings.odnoklassniki_application_key and settings.odnoklassniki_session_secret
            else "credentials_required",
            "instagram": "credentials_present_adapter_required"
            if settings.instagram_business_account_id and settings.instagram_access_token
            else "credentials_required",
            "publication_mode": "owner_approved_scheduled_content_only",
        },
        "media_generation": {
            "image": "codex_workflow_available"
            if not settings.image_generation_api_key
            else "credentials_present_adapter_required",
            "video": "credentials_present_adapter_required"
            if settings.video_generation_api_key
            else "credentials_required",
        },
    }
