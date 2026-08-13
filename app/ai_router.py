from __future__ import annotations

from typing import Any

from .config import settings
from .improvements import workspace_agent_configuration_status
from .llm import llm_advisor


def marketing_channel_status(channel: str) -> dict[str, Any]:
    configured = {
        "yandex_direct": bool(settings.yandex_direct_token),
        "yandex_business": bool(settings.yandex_direct_token),
        "vk_ads": bool(settings.vk_ads_token),
        "2gis": bool(settings.twogis_business_token),
        "avito": bool(settings.avito_client_id and settings.avito_client_secret),
        "telegram_ads": bool(settings.telegram_ads_token),
        "seo": True,
        "content": True,
        "other": False,
    }.get(channel, False)
    return {
        "channel": channel,
        "credentials": "configured" if configured else "credentials_required",
        # A token alone never means the system can spend money. Campaign activation
        # remains manual until a channel-specific executor and owner approval exist.
        "automatic_activation": False,
        "activation_mode": "manual_external_campaign_id",
    }


def provider_catalog() -> list[dict[str, Any]]:
    return [
        {
            "capability": "business_reasoning",
            "provider": "openai_compatible_responses",
            "status": llm_advisor.provider_statuses()["openai_responses"],
            "routing": "request_analysis_primary",
            "scopes": ["aggregate_metrics", "redacted_business_context"],
            "forbidden": ["banking_credentials", "unapproved_commitments"],
        },
        {
            "capability": "business_reasoning",
            "provider": "anthropic_messages",
            "status": llm_advisor.provider_statuses()["anthropic_messages"],
            "routing": "business_review_primary",
            "scopes": ["aggregate_metrics", "redacted_business_context"],
            "forbidden": ["raw_personal_data", "banking_credentials", "unapproved_commitments", "application_tools"],
        },
        {
            "capability": "product_improvement",
            "provider": "chatgpt_workspace_agents",
            "status": workspace_agent_configuration_status(),
            "scopes": ["redacted_request", "acceptance_criteria", "test_plan"],
            "forbidden": ["raw_secrets", "production_database_write"],
        },
        {
            "capability": "image_generation",
            "provider": "codex_imagegen_workflow" if not settings.image_generation_api_key else "external_image_provider",
            "status": "codex_workflow_available" if not settings.image_generation_api_key else "credentials_present_adapter_required",
            "scopes": ["approved_brand_brief", "public_content"],
            "forbidden": ["customer_personal_data", "banking_credentials"],
        },
        {
            "capability": "video_generation",
            "provider": "external_video_provider",
            "status": "credentials_present_adapter_required" if settings.video_generation_api_key else "credentials_required",
            "scopes": ["approved_media_assets", "public_content"],
            "forbidden": ["customer_personal_data", "unlicensed_media"],
        },
    ]


def media_provider(kind: str) -> tuple[str, str]:
    if kind == "image":
        return "codex_imagegen_workflow", "queued"
    if settings.video_generation_api_key:
        return "external_video_provider", "adapter_required"
    return "external_video_provider", "credentials_required"
