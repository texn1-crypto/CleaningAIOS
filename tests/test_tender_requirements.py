from pathlib import Path

from app.tender_requirements import (
    TOTAL_REQUIREMENTS,
    tender_l6_coverage_summary,
    tender_l6_requirements,
)


def test_master_requirements_manifest_tracks_every_number_and_real_evidence():
    rows = tender_l6_requirements()

    assert TOTAL_REQUIREMENTS == 325
    assert [row["id"] for row in rows] == list(range(1, 326))
    assert all(row["status"] != "verified" or row["evidence"] for row in rows)
    for row in rows:
        for reference in row["evidence"]:
            assert Path(reference.split(":", 1)[0]).exists(), reference


def test_master_requirements_summary_never_claims_l6_from_partial_components():
    summary = tender_l6_coverage_summary()

    assert sum(summary["by_status"].values()) == 325
    assert summary["by_status"]["verified"] < 325
    assert summary["critical_open_count"] > 0
    assert summary["first_release_ready"] is False
    assert summary["l6_achieved"] is False
    gates = {row["id"]: row["status"] for row in summary["release_gates"]}
    assert gates == {313: "partial", 314: "missing", 325: "missing"}


def test_master_requirements_api_is_manager_only_and_filterable(client):
    denied = client.get(
        "/api/tender-autopilot/master-requirements/summary",
        headers={"X-Role": "viewer"},
    )
    assert denied.status_code == 403

    summary = client.get("/api/tender-autopilot/master-requirements/summary")
    assert summary.status_code == 200
    assert summary.json()["total"] == 325

    missing = client.get(
        "/api/tender-autopilot/master-requirements?status=missing&priority=P0"
    )
    assert missing.status_code == 200
    assert missing.json()
    assert all(
        row["status"] == "missing" and row["priority"] == "P0"
        for row in missing.json()
    )

    invalid = client.get(
        "/api/tender-autopilot/master-requirements?status=implemented"
    )
    assert invalid.status_code == 422
