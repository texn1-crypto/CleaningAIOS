from __future__ import annotations

from collections import Counter
from functools import lru_cache
import json
from pathlib import Path
from typing import Any


REQUIREMENTS_FILE = Path(__file__).with_name("data") / "tender_l6_requirements.json"
ALLOWED_STATUSES = frozenset({"verified", "partial", "missing", "unverified"})
ALLOWED_PRIORITIES = frozenset({"P0", "P1", "P2"})
TOTAL_REQUIREMENTS = 325


class RequirementsManifestError(RuntimeError):
    """The checked-in L6 traceability manifest is incomplete or inconsistent."""


@lru_cache(maxsize=1)
def _load_manifest() -> dict[str, Any]:
    raw_payload: object = json.loads(REQUIREMENTS_FILE.read_text(encoding="utf-8"))
    if not isinstance(raw_payload, dict):
        raise RequirementsManifestError("manifest must be a JSON object")
    payload: dict[str, Any] = raw_payload
    requirements = payload.get("requirements")
    if not isinstance(requirements, list):
        raise RequirementsManifestError("requirements must be a list")
    identifiers = [item.get("id") for item in requirements if isinstance(item, dict)]
    if identifiers != list(range(1, TOTAL_REQUIREMENTS + 1)):
        raise RequirementsManifestError("requirements must contain ordered IDs 1..325")
    for item in requirements:
        status = item.get("status")
        priority = item.get("priority")
        evidence = item.get("evidence")
        if status not in ALLOWED_STATUSES:
            raise RequirementsManifestError(f"requirement {item['id']} has invalid status")
        if priority not in ALLOWED_PRIORITIES:
            raise RequirementsManifestError(
                f"requirement {item['id']} has invalid priority"
            )
        if not isinstance(evidence, list):
            raise RequirementsManifestError(
                f"requirement {item['id']} evidence must be a list"
            )
        if status == "verified" and not evidence:
            raise RequirementsManifestError(
                f"verified requirement {item['id']} must cite repository evidence"
            )
    return payload


def tender_l6_requirements(
    *,
    status: str | None = None,
    priority: str | None = None,
) -> list[dict[str, Any]]:
    if status is not None and status not in ALLOWED_STATUSES:
        raise ValueError("Unsupported requirement status")
    if priority is not None and priority not in ALLOWED_PRIORITIES:
        raise ValueError("Unsupported requirement priority")
    rows = _load_manifest()["requirements"]
    return [
        {
            **row,
            "evidence": list(row["evidence"]),
        }
        for row in rows
        if (status is None or row["status"] == status)
        and (priority is None or row["priority"] == priority)
    ]


def tender_l6_coverage_summary() -> dict[str, Any]:
    manifest = _load_manifest()
    rows = tender_l6_requirements()
    counts = Counter(str(row["status"]) for row in rows)
    verified = counts["verified"]
    critical_open = [
        row
        for row in rows
        if row["priority"] == "P0" and row["status"] != "verified"
    ]
    release_gate_ids = {313, 314, 325}
    release_gates = [row for row in rows if row["id"] in release_gate_ids]
    return {
        "specification": manifest["specification"],
        "north_star": dict(manifest["north_star"]),
        "total": TOTAL_REQUIREMENTS,
        "by_status": {
            status: counts[status]
            for status in ("verified", "partial", "missing", "unverified")
        },
        "verified_coverage_percent": round(
            verified * 100 / TOTAL_REQUIREMENTS,
            2,
        ),
        "critical_open_count": len(critical_open),
        "next_critical_requirements": [
            {
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
            }
            for row in critical_open[:10]
        ],
        "release_gates": [
            {
                "id": row["id"],
                "status": row["status"],
                "title": row["title"],
            }
            for row in release_gates
        ],
        "first_release_ready": all(
            row["status"] == "verified" for row in release_gates if row["id"] == 313
        ),
        "l6_achieved": all(row["status"] == "verified" for row in release_gates),
        "status_policy": dict(manifest["status_policy"]),
    }
