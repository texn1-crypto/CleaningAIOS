def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


def test_task_and_agent_flow(client):
    created = client.post("/api/tasks", json={"title": "Проверить тендер", "agent_type": "tender"})
    assert created.status_code == 201
    task_id = created.json()["id"]
    result = client.post(f"/api/tasks/{task_id}/run")
    assert result.status_code == 200
    assert result.json()["status"] == "done"
    assert result.json()["result"]["submission_requires_owner_approval"] is True


def test_owner_approval_gate(client):
    task = client.post("/api/tasks", json={"title": "Подать заявку", "agent_type": "tender", "payload": {"action_kind": "tender_submission"}}).json()
    blocked = client.post(f"/api/tasks/{task['id']}/run").json()
    assert blocked["status"] == "blocked"
    assert blocked["result"]["reason"] == "owner_approval_required"


def test_outreach_suppression_and_deduplication(client):
    assert client.post("/api/outreach/suppress", json={"address": "stop@example.com"}).status_code == 201
    blocked = client.post("/api/outreach/messages", json={"campaign_key": "c1", "recipient": "stop@example.com", "subject": "Hi", "body": "Body"})
    assert blocked.status_code == 409
    payload = {"campaign_key": "c1", "recipient": "ok@example.com", "subject": "Hi", "body": "Body"}
    assert client.post("/api/outreach/messages", json=payload).status_code == 201
    assert client.post("/api/outreach/messages", json=payload).status_code == 409


def test_rbac_rejects_viewer_write(client):
    response = client.post("/api/tasks", headers={"X-Role": "viewer"}, json={"title": "Denied"})
    assert response.status_code == 403
