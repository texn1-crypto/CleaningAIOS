from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import business_policy
from app.db import Base
from app.models import AuditLog, BusinessGoal, CompanyKnowledge, DomainEvent


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_owner_business_policy_is_idempotent_and_keeps_contact_out_of_audit(monkeypatch):
    monkeypatch.setattr(business_policy.settings, "company_phone", "+7 000 000-00-00")
    monkeypatch.setattr(
        business_policy.settings,
        "company_service_area",
        "Санкт-Петербург и Ленинградская область",
    )
    monkeypatch.setattr(
        business_policy.settings,
        "management_contact_regions",
        "Санкт-Петербург|Ленинградская область",
    )
    session_factory = _session_factory()

    with session_factory() as db:
        first = business_policy.sync_owner_business_policy(db)
        second = business_policy.sync_owner_business_policy(db)

        assert first == {"knowledge_updated": 4, "goal_updated": 1}
        assert second == {"knowledge_updated": 0, "goal_updated": 0}
        assert db.scalar(select(func.count(CompanyKnowledge.id))) == 4
        assert db.scalar(select(func.count(BusinessGoal.id))) == 1
        assert db.scalar(select(func.count(DomainEvent.id))) == 5
        audit_rows = db.scalars(select(AuditLog)).all()
        assert len(audit_rows) == 5
        assert all("+7 000" not in str(row.details) for row in audit_rows)


def test_owner_business_policy_requires_handoff_phone(monkeypatch):
    monkeypatch.setattr(business_policy.settings, "company_phone", "")
    session_factory = _session_factory()

    with session_factory() as db:
        try:
            business_policy.sync_owner_business_policy(db)
        except RuntimeError as exc:
            assert str(exc) == "COMPANY_PHONE is required for owner lead handoff"
        else:
            raise AssertionError("missing owner handoff phone must fail closed")
