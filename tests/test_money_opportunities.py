from decimal import Decimal

from app.db import SessionLocal
from app.models import BusinessRecord, OperatingEntity, TenderAssessmentSnapshot


def test_money_opportunities_are_database_facts_with_evidence(client):
    with SessionLocal() as db:
        experiment = BusinessRecord(
            record_type="marketing_experiment",
            external_id="audit-roi-campaign",
            title="Audited ROI campaign",
            status="running",
            source="yandex_direct",
            data={"budget_limit": 2000, "spent": 1000},
        )
        lead = BusinessRecord(
            record_type="lead",
            external_id="audit-roi-lead",
            title="Audited won lead",
            status="won",
            score=91,
            source="public_site",
            data={
                "utm_campaign": "audit-roi-campaign",
                "recognized_revenue": 5000,
                "next_action": "contract_recorded",
            },
        )
        tender = BusinessRecord(
            record_type="tender",
            external_id="audit-profitable-tender",
            title="Audited profitable tender",
            status="awaiting_approval",
            source="https://provider.example/tenders",
            data={},
        )
        contract = OperatingEntity(
            entity_type="contract",
            external_id="audit-active-contract",
            name="Audited active contract",
            status="active",
            data={"monthly_revenue": 10000},
        )
        db.add_all([experiment, lead, tender, contract])
        db.flush()
        snapshot = TenderAssessmentSnapshot(
            record_id=tender.id,
            input_hash="a" * 64,
            rules_version="test-v1",
            status="ready_for_owner_review",
            recommendation="consider_participation",
            input_snapshot={},
            result_snapshot={
                "economics": {
                    "base": {"net_profit": "250000.00", "margin_percent": "25.00"},
                    "conservative": {"net_profit": "150000.00"},
                },
                "risk": {"score": 20},
                "verification_gaps": [],
                "hard_stops": [],
            },
            created_by="test",
        )
        db.add(snapshot)
        db.commit()
        ids = {
            "experiment": experiment.id,
            "lead": lead.id,
            "tender": tender.id,
            "snapshot": snapshot.id,
            "contract": contract.id,
        }

    try:
        response = client.get("/api/money-opportunities")
        assert response.status_code == 200
        body = response.json()
        assert body["facts_only"] is True
        tender_card = next(
            card
            for card in body["tenders"]["cards"]
            if card["record_id"] == ids["tender"]
        )
        assert tender_card["expected_net_profit"] == "250000.00"
        assert tender_card["conservative_net_profit"] == "150000.00"
        assert tender_card["next_action"] == "owner_participation_review"
        assert tender_card["evidence"]["assessment_snapshot_id"] == ids["snapshot"]

        marketing_card = next(
            card
            for card in body["marketing"]["cards"]
            if card["experiment_id"] == ids["experiment"]
        )
        assert marketing_card["actual_spend"] == "1000.00"
        assert marketing_card["recognized_revenue"] == "5000.00"
        assert marketing_card["roi_percent"] == "400.00"
        assert marketing_card["evidence"]["won_lead_ids"] == [ids["lead"]]
        assert Decimal(
            body["sales"]["summary"]["active_monthly_revenue"]
        ) >= Decimal("10000.00")
        assert body["sales"]["source"]["contracts"].startswith(
            "operating_entities"
        )
    finally:
        with SessionLocal() as db:
            db.query(TenderAssessmentSnapshot).filter(
                TenderAssessmentSnapshot.id == ids["snapshot"]
            ).delete()
            db.query(BusinessRecord).filter(
                BusinessRecord.id.in_([ids["experiment"], ids["lead"], ids["tender"]])
            ).delete(synchronize_session=False)
            db.query(OperatingEntity).filter(
                OperatingEntity.id == ids["contract"]
            ).delete()
            db.commit()


def test_money_opportunities_require_manager_role(client):
    response = client.get(
        "/api/money-opportunities",
        headers={"X-Role": "operator"},
    )
    assert response.status_code == 403
