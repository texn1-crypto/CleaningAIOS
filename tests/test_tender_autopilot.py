from __future__ import annotations

from copy import deepcopy


MANAGER = {"X-Role": "manager"}


def _create_tender_with_evidence(client, suffix: str) -> tuple[int, int, int]:
    tender = client.post(
        "/api/records",
        headers=MANAGER,
        json={
            "record_type": "tender",
            "external_id": f"autopilot-{suffix}",
            "title": "Поставка материалов и клининг объекта",
            "deadline_at": "2040-01-10T12:00:00Z",
            "data": {
                "source_url": f"https://procurement.example.invalid/{suffix}",
                "platform": "fixture",
                "region": "Санкт-Петербург",
            },
        },
    ).json()
    specification = client.post(
        f"/api/tenders/{tender['id']}/documents",
        headers=MANAGER,
        json={
            "name": "specification.pdf",
            "source_url": f"https://procurement.example.invalid/{suffix}/specification.pdf",
            "checksum": "a" * 64,
            "analysis": {"kind": "requirements", "extraction_mode": "fixture"},
        },
    ).json()
    quote = client.post(
        f"/api/tenders/{tender['id']}/documents",
        headers=MANAGER,
        json={
            "name": "supplier-quote.pdf",
            "source_url": f"https://supplier.example.invalid/{suffix}/quote.pdf",
            "checksum": "b" * 64,
            "analysis": {"kind": "supplier_quote", "extraction_mode": "fixture"},
        },
    ).json()
    return tender["id"], specification["id"], quote["id"]


def _assessment_payload(specification_id: int, quote_id: int) -> dict:
    return {
        "requirements": [
            {
                "code": "service.scope",
                "description": "Выполнить полный объём услуг из технического задания",
                "mandatory": True,
                "status": "satisfied",
                "evidence": [
                    {
                        "document_id": specification_id,
                        "document_checksum": "a" * 64,
                        "locator": "page 2, section 1",
                        "excerpt": "Объём и периодичность услуг определены",
                    }
                ],
            }
        ],
        "qualification_checks": [
            {
                "code": "company.experience",
                "description": "Опыт компании соответствует требованию",
                "status": "satisfied",
                "evidence": [
                    {
                        "document_id": specification_id,
                        "document_checksum": "a" * 64,
                        "locator": "page 5, section 4",
                        "excerpt": "Требуется один исполненный договор",
                    }
                ],
            }
        ],
        "supplier_quote": {
            "supplier_name": "ООО Поставщик",
            "quote_reference": "QUOTE-001",
            "total_cost": "500000.00",
            "currency": "RUB",
            "vat_included": True,
            "stock_status": "confirmed",
            "valid_until": "2040-01-05T12:00:00Z",
            "evidence": [
                {
                    "document_id": quote_id,
                    "document_checksum": "b" * 64,
                    "locator": "page 1",
                    "excerpt": "Итого 500 000 рублей, товар в наличии",
                }
            ],
        },
        "contract_value": "1000000.00",
        "contract_months": 1,
        "payroll_cost": "100000.00",
        "logistics_cost": "20000.00",
        "other_direct_cost": "10000.00",
        "onboarding_cost": "20000.00",
        "available_working_capital": "800000.00",
        "payment_delay_days": 30,
        "tax_percent": "6.00",
        "contingency_percent": "5.00",
        "minimum_margin_percent": "10.00",
        "conservative_cost_increase_percent": "15.00",
        "maximum_risk_score": 35,
    }


def test_tender_decision_snapshot_is_decimal_evidence_bound_and_idempotent(client):
    tender_id, specification_id, quote_id = _create_tender_with_evidence(
        client, "ready"
    )
    payload = _assessment_payload(specification_id, quote_id)

    first = client.post(
        f"/api/tenders/{tender_id}/decision-snapshots",
        headers=MANAGER,
        json=payload,
    )

    assert first.status_code == 201
    body = first.json()
    assert body["created"] is True
    assert body["status"] == "ready_for_owner_review"
    assert body["recommendation"] == "consider_participation"
    assert body["result"]["economics"]["valid"] is True
    assert body["result"]["economics"]["base"] == {
        "revenue": "1000000.00",
        "direct_cost": "650000.00",
        "tax": "60000.00",
        "contingency": "32500.00",
        "total_cost": "742500.00",
        "net_profit": "257500.00",
        "margin_percent": "25.75",
    }
    assert body["result"]["economics"]["conservative"]["net_profit"] == "155125.00"
    assert body["result"]["economics"]["stop_price"] == "812500.00"
    assert body["result"]["automatic_submission_allowed"] is False
    assert body["participation_review_task_id"] is not None

    repeated = client.post(
        f"/api/tenders/{tender_id}/decision-snapshots",
        headers=MANAGER,
        json=payload,
    ).json()
    assert repeated["created"] is False
    assert repeated["id"] == body["id"]
    assert repeated["participation_review_task_id"] == body["participation_review_task_id"]

    snapshots = client.get(
        f"/api/tenders/{tender_id}/decision-snapshots",
        headers=MANAGER,
    ).json()
    assert len(snapshots) == 1
    task = next(
        row
        for row in client.get("/api/tasks", headers=MANAGER).json()
        if row["id"] == body["participation_review_task_id"]
    )
    assert task["payload"]["assessment_snapshot_id"] == body["id"]
    assert task["payload"]["assessment_input_hash"] == body["input_hash"]


def test_unknown_mandatory_requirement_fails_closed(client):
    tender_id, specification_id, quote_id = _create_tender_with_evidence(
        client, "unknown"
    )
    payload = _assessment_payload(specification_id, quote_id)
    payload["requirements"][0]["status"] = "unknown"
    payload["requirements"][0]["evidence"] = []

    body = client.post(
        f"/api/tenders/{tender_id}/decision-snapshots",
        headers=MANAGER,
        json=payload,
    ).json()

    assert body["status"] == "needs_verification"
    assert body["recommendation"] == "collect_data"
    assert body["result"]["economics"]["valid"] is False
    assert "requirement:service.scope:unknown" in body["result"]["verification_gaps"]
    assert body["result"]["participation_review_available"] is False
    assert body["participation_review_task_id"] is None
    assert body["result"]["automatic_submission_allowed"] is False


def test_tender_snapshot_rejects_document_substitution(client):
    tender_id, specification_id, quote_id = _create_tender_with_evidence(
        client, "tamper"
    )
    payload = _assessment_payload(specification_id, quote_id)
    tampered = deepcopy(payload)
    tampered["requirements"][0]["evidence"][0]["document_checksum"] = "c" * 64

    response = client.post(
        f"/api/tenders/{tender_id}/decision-snapshots",
        headers=MANAGER,
        json=tampered,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        f"Checksum mismatch for tender document {specification_id}"
    )
    assert client.get(
        f"/api/tenders/{tender_id}/decision-snapshots",
        headers=MANAGER,
    ).json() == []


def test_expired_supplier_quote_cannot_become_ready(client):
    tender_id, specification_id, quote_id = _create_tender_with_evidence(
        client, "expired-quote"
    )
    payload = _assessment_payload(specification_id, quote_id)
    payload["supplier_quote"]["valid_until"] = "2020-01-01T00:00:00Z"

    body = client.post(
        f"/api/tenders/{tender_id}/decision-snapshots",
        headers=MANAGER,
        json=payload,
    ).json()

    assert body["status"] == "needs_verification"
    assert "supplier_quote_expired" in body["result"]["verification_gaps"]
    assert body["result"]["economics"]["valid"] is False
    assert body["participation_review_task_id"] is None
