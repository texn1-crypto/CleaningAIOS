from __future__ import annotations

import ast
from pathlib import Path

from app.quality_criteria import assess_junit, quality_criteria


def test_quality_catalog_has_100_distinct_executable_contracts():
    rows = quality_criteria()
    assert len(rows) == 100
    for row in rows:
        path, name = row["test"].split("::")
        tree = ast.parse(Path(path).read_text())
        assert any(isinstance(node, ast.FunctionDef) and node.name == name for node in tree.body)


def test_missing_skipped_and_failed_cases_never_count_as_pass(tmp_path):
    path = tmp_path / "results.xml"
    path.write_text('''<testsuites><testsuite>
      <testcase classname="tests.test_public_research" name="test_research_follows_real_links_and_keeps_per_page_hashes"/>
      <testcase classname="tests.test_public_research" name="test_research_skips_external_query_active_and_download_links"><skipped/></testcase>
      <testcase classname="tests.test_public_research" name="test_research_rechecks_dns_for_every_page"><failure/></testcase>
      <testcase classname="tests.test_public_research" name="test_research_stops_on_root_access_failure_without_retry[401]"/>
      <testcase classname="tests.test_public_research" name="test_research_stops_on_root_access_failure_without_retry[403]"><failure/></testcase>
    </testsuite></testsuites>''')
    result = assess_junit(path)
    assert result["regression_passed"] == 1
    assert result["not_passed"] == 99
    assert result["production_verified"] is False
    assert result["l6_achieved"] is False
    assert result["criteria"][3]["status"] == "failed"
    assert result["criteria"][-1]["status"] == "unverified"


def test_quality_catalog_endpoint_requires_manager_and_does_not_claim_runtime_success(client):
    assert client.get("/api/agents/quality-criteria", headers={"X-Role": "operator"}).status_code == 403
    response = client.get("/api/agents/quality-criteria", headers={"X-Role": "manager"})
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == 100
    assert len(result["registered_agents"]) == 21
    assert result["assessment"] == "acceptance_catalog_not_a_production_score"
    assert result["l6_achieved"] is False
