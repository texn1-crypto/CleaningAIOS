from __future__ import annotations

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import BusinessRecord, OutreachConsent, OutboundMessage


def test_public_lead_scout_filters_personal_uncited_and_out_of_region_contacts(client, monkeypatch):
    from app import lead_scout

    with SessionLocal() as db:
        consent_count_before = db.scalar(select(func.count(OutreachConsent.address)))
        outbound_count_before = db.scalar(select(func.count(OutboundMessage.id)))

    cited = "https://business-center.example/contacts"
    provider_result = {
        "status": "succeeded",
        "provider": "perplexity_sonar",
        "model": "sonar-test",
        "prompt": {"name": "public_lead_discovery", "version": "1.0.0"},
        "citations": [cited, "https://directory.example/company"],
        "leads": [
            {
                "organization_name": "Бизнес-центр Север",
                "region": "СПб",
                "email": "info@business-center.example",
                "phone": "+7 (812) 555-01-01",
                "website": "https://business-center.example/",
                "source_url": cited,
                "contact_scope": "organization",
                "contact_person_named": False,
            },
            {
                "organization_name": "Бизнес-центр Север",
                "region": "Санкт-Петербург",
                "email": "sales@business-center.example",
                "phone": "+7 (812) 555-01-01",
                "website": "https://business-center.example/",
                "source_url": cited,
                "contact_scope": "organization",
                "contact_person_named": False,
            },
            {
                "organization_name": "Личный контакт",
                "region": "Москва",
                "email": "ivan.petrov@gmail.com",
                "phone": "",
                "website": "",
                "source_url": "https://directory.example/company",
                "contact_scope": "person",
                "contact_person_named": True,
            },
            {
                "organization_name": "Казанский объект",
                "region": "Казань",
                "email": "info@kazan-object.example",
                "phone": "",
                "website": "https://kazan-object.example/",
                "source_url": "https://directory.example/company",
                "contact_scope": "organization",
                "contact_person_named": False,
            },
            {
                "organization_name": "Московский офис",
                "region": "Москва",
                "email": "office@moscow-office.example",
                "phone": "",
                "website": "https://moscow-office.example/",
                "source_url": "https://directory.example/company",
                "contact_scope": "organization",
                "contact_person_named": False,
            },
            {
                "organization_name": "Источник не подтвержден",
                "region": "Москва",
                "email": "info@uncited.example",
                "phone": "",
                "website": "https://uncited.example/",
                "source_url": "https://uncited.example/contacts",
                "contact_scope": "organization",
                "contact_person_named": False,
            },
        ],
    }
    monkeypatch.setattr(
        lead_scout.llm_advisor,
        "discover_public_business_leads",
        lambda brief: provider_result,
    )

    task = client.post(
        "/api/tasks",
        json={
            "title": "Найти публичные контакты потенциальных заказчиков",
            "agent_type": "lead_scout",
            "payload": {"regions": ["СПб", "Москва"], "max_results": 20},
        },
    ).json()
    first = client.post(f"/api/tasks/{task['id']}/run").json()
    assert first["status"] == "done"
    assert first["result"]["created"] == 2
    assert first["result"]["updated"] == 1
    assert first["result"]["external_messages_sent"] is False
    assert first["result"]["consent"]["marketing_contact_allowed"] is False
    assert first["result"]["rejected"] == {
        "outside_target_regions": 1,
        "personal_contact": 1,
        "source_not_cited": 1,
    }

    with SessionLocal() as db:
        leads = db.scalars(
            select(BusinessRecord).where(
                BusinessRecord.source == "perplexity_public_business_search"
            )
        ).all()
        assert len(leads) == 2
        northern = next(row for row in leads if row.title == "Бизнес-центр Север")
        assert northern.data["public_emails"] == [
            "info@business-center.example",
            "sales@business-center.example",
        ]
        assert northern.data["public_phones"] == ["+78125550101"]
        assert northern.data["source_urls"] == [cited]
        assert northern.data["outreach_consent"] == "not_verified"
        assert db.scalar(select(func.count(OutreachConsent.address))) == consent_count_before
        assert db.scalar(select(func.count(OutboundMessage.id))) == outbound_count_before

    repeated_task = client.post(
        "/api/tasks",
        json={
            "title": "Повторно проверить публичные контакты",
            "agent_type": "lead_scout",
            "payload": {"regions": ["Санкт-Петербург"], "max_results": 20},
        },
    ).json()
    repeated = client.post(f"/api/tasks/{repeated_task['id']}/run").json()
    assert repeated["result"]["created"] == 0
    assert repeated["result"]["updated"] == 2
    assert repeated["result"]["rejected"]["outside_requested_regions"] == 3
    with SessionLocal() as db:
        assert db.scalar(
            select(func.count(BusinessRecord.id)).where(
                BusinessRecord.source == "perplexity_public_business_search"
            )
        ) == 2


def test_chat_routes_public_customer_search_to_lead_scout():
    from app.chat import understand_russian_message

    intent = understand_russian_message(
        "Найди потенциальных заказчиков клининга по публичным контактам в Москве и СПб"
    )
    assert intent["kind"] == "task"
    assert intent["agent_type"] == "lead_scout"
    assert intent["payload"]["action"] == "discover_public_business_leads"
    assert intent["payload"]["automatic_outreach"] is False
