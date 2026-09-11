from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import load_workbook
from pypdf import PdfReader
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import contact_directory, notifications
from app.contact_directory import (
    CONTACT_RECORD_TYPE,
    OUTREACH_CANDIDATE_RECORD_TYPE,
    WEEKLY_EXPORT_RECORD_TYPE,
    build_weekly_contact_export,
    contact_directory_summary,
    upsert_contact_directory,
)
from app.db import Base
from app.lead_scout import persist_public_business_leads
from app.management_companies import import_management_companies
from app.models import BusinessRecord, OutboundMessage, OwnerNotification


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_baseline_import_and_public_discovery_share_one_safe_contact_directory():
    session_factory = _session_factory()
    source = "https://directory.example/uk"
    with session_factory() as db:
        imported = import_management_companies(
            db,
            filename="owner-baseline.csv",
            content=(
                "company,region,email,inn\n"
                "ТСЖ Исходное,Санкт-Петербург,info@baseline-uk.example,7812000000\n"
            ).encode(),
            source_kind="owner_baseline",
            source_url="owner-upload://management-baseline",
            actor="owner-api-key",
        )
        assert imported["contact_directory_created"] == 1
        assert imported["baseline_contacts"] is True
        outbound_before = int(db.scalar(select(func.count()).select_from(OutboundMessage)) or 0)

        discovered = persist_public_business_leads(
            db,
            provider_result={
                "citations": [source],
                "leads": [
                    {
                        "organization_name": "ТСЖ Исходное",
                        "organization_type": "ТСЖ",
                        "region": "Санкт-Петербург",
                        "city": "Санкт-Петербург",
                        "inn": "7812000000",
                        "email": "info@baseline-uk.example",
                        "phone": "",
                        "website": "https://baseline-uk.example/",
                        "source_url": source,
                        "contact_scope": "organization",
                        "contact_person_named": False,
                    },
                    {
                        "organization_name": "УК Новый дом",
                        "organization_type": "УК",
                        "region": "Ленинградская область",
                        "city": "Мурино",
                        "inn": "4703000000",
                        "email": "office@new-uk.example",
                        "phone": "",
                        "website": "https://new-uk.example/",
                        "source_url": source,
                        "contact_scope": "organization",
                        "contact_person_named": False,
                    },
                    {
                        "organization_name": "Бизнес-центр вне сегмента",
                        "organization_type": "БЦ",
                        "region": "Санкт-Петербург",
                        "city": "Санкт-Петербург",
                        "inn": "",
                        "email": "info@office-building.example",
                        "phone": "",
                        "website": "https://office-building.example/",
                        "source_url": source,
                        "contact_scope": "organization",
                        "contact_person_named": False,
                    },
                ],
            },
            allowed_regions={"Санкт-Петербург", "Ленинградская область"},
            segment="management_companies",
        )
        db.commit()

        assert discovered["contact_directory"] == {
            "created": 1,
            "updated": 1,
            "mailing_candidates_created": 1,
            "outbound_messages_created": 0,
        }
        assert discovered["rejected"]["outside_management_company_segment"] == 1
        summary = contact_directory_summary(db)
        assert summary["total_contacts"] == 2
        assert summary["baseline_contacts"] == 1
        assert summary["new_contacts"] == 1
        assert summary["mailing_candidates"] == 1
        assert summary["mailing_candidate_states"] == {"consent_required": 1}
        assert int(db.scalar(select(func.count()).select_from(OutboundMessage)) or 0) == outbound_before
        baseline = db.scalar(
            select(BusinessRecord).where(
                BusinessRecord.record_type == CONTACT_RECORD_TYPE,
                BusinessRecord.status == "baseline",
            )
        )
        assert baseline is not None
        assert baseline.data["source_urls"] == ["https://directory.example/uk", "owner-upload://management-baseline"]


def test_weekly_export_is_idempotent_and_carries_only_never_exported_contacts(monkeypatch, tmp_path):
    session_factory = _session_factory()
    monkeypatch.setattr(contact_directory.settings, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(contact_directory.settings, "contact_export_timezone", "Europe/Moscow")
    monkeypatch.setattr(notifications.settings, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(notifications.settings, "owner_telegram_id", "")
    monkeypatch.setattr(notifications.settings, "telegram_bot_token", "")

    with session_factory() as db:
        upsert_contact_directory(
            db,
            email="info@baseline.example",
            organization_name="ТСЖ Исходное",
            city="Санкт-Петербург",
            source_url="owner-upload://baseline",
            source_kind="owner_baseline",
            baseline=True,
        )
        upsert_contact_directory(
            db,
            email="info@first-new.example",
            organization_name="УК Новая Первая",
            city="Санкт-Петербург",
            inn="7812000001",
            source_url="https://first-new.example/contacts",
            source_kind="public_search",
            baseline=False,
        )
        first = build_weekly_contact_export(
            db,
            current=datetime(2042, 5, 5, 12, tzinfo=timezone.utc),
        )
        db.commit()
        repeated = build_weekly_contact_export(
            db,
            current=datetime(2042, 5, 6, 12, tzinfo=timezone.utc),
        )
        db.commit()

        assert first["new_contacts"] == 1
        assert repeated["reused"] is True
        assert repeated["report_id"] == first["report_id"]
        assert db.scalar(
            select(func.count(BusinessRecord.id)).where(
                BusinessRecord.record_type == WEEKLY_EXPORT_RECORD_TYPE
            )
        ) == 1
        assert db.scalar(select(func.count(OwnerNotification.id))) == 1

        pdf_path = Path(first["artifacts"]["pdf"]["storage_path"])
        xlsx_path = Path(first["artifacts"]["xlsx"]["storage_path"])
        csv_path = Path(first["artifacts"]["csv"]["storage_path"])
        assert pdf_path.is_relative_to(tmp_path)
        pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
        assert "УК Новая Первая" in pdf_text
        assert "ТСЖ Исходное" not in pdf_text
        workbook = load_workbook(xlsx_path, read_only=True, data_only=True)
        try:
            sheet = workbook["Новые контакты"]
            assert sheet["A3"].value == "Организация"
            assert sheet["A4"].value == "УК Новая Первая"
        finally:
            workbook.close()
        with csv_path.open(encoding="utf-8-sig", newline="") as stream:
            csv_rows = list(csv.reader(stream))
        assert csv_rows[1][0] == "УК Новая Первая"

        upsert_contact_directory(
            db,
            email="info@second-new.example",
            organization_name="ТСЖ Новое Второе",
            city="Гатчина",
            source_url="https://second-new.example/contacts",
            source_kind="public_search",
            baseline=False,
        )
        second = build_weekly_contact_export(
            db,
            current=datetime(2042, 5, 12, 12, tzinfo=timezone.utc),
            notify_owner=False,
        )
        db.commit()
        assert second["new_contacts"] == 1
        contacts = db.scalars(
            select(BusinessRecord).where(BusinessRecord.record_type == CONTACT_RECORD_TYPE)
        ).all()
        exported = {row.data["email"]: row.data.get("first_export_key") for row in contacts}
        assert exported["info@baseline.example"] is None
        assert exported["info@first-new.example"] != exported["info@second-new.example"]
        assert db.scalar(
            select(func.count(BusinessRecord.id)).where(
                BusinessRecord.record_type == OUTREACH_CANDIDATE_RECORD_TYPE
            )
        ) == 2
