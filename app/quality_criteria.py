from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


CATALOG_PATH = Path(__file__).with_name("data") / "quality_criteria_100.json"


def quality_criteria() -> list[dict[str, Any]]:
    rows = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or [row.get("id") for row in rows] != list(range(1, 101)):
        raise ValueError("Quality criteria must contain ordered IDs 1..100")
    if len({row.get("test") for row in rows}) != 100:
        raise ValueError("Every criterion must have a distinct regression contract")
    for row in rows:
        if not all(isinstance(row.get(key), str) and row[key].strip() for key in ("category", "criterion", "test")):
            raise ValueError("Every criterion must name its acceptance condition and regression")
    return rows


def assess_junit(path: Path) -> dict[str, Any]:
    root = ElementTree.parse(path).getroot()
    cases: dict[str, list[bool]] = {}
    for case in root.iter("testcase"):
        module = str(case.get("classname", "")).replace(".", "/") + ".py"
        name = str(case.get("name", "")).split("[", 1)[0]
        passed = not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
        cases.setdefault(f"{module}::{name}", []).append(passed)
    results = []
    for row in quality_criteria():
        outcomes = cases.get(row["test"], [])
        status = "regression_passed" if outcomes and all(outcomes) else "failed" if outcomes else "unverified"
        results.append({**row, "status": status, "cases_executed": len(outcomes)})
    passed_count = sum(row["status"] == "regression_passed" for row in results)
    return {
        "total": 100, "regression_passed": passed_count, "not_passed": 100 - passed_count,
        "production_verified": False, "l6_achieved": False,
        "evidence_kind": "isolated_regression_tests_not_live_provider_verification",
        "criteria": results,
    }
