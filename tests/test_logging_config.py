import json
import logging

from app.logging_config import JsonFormatter, request_correlation_id


def test_json_formatter_emits_correlation_and_redacts_credentials(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "api_key", "configured-secret-api-key")
    formatter = JsonFormatter("test-service")
    record = logging.LogRecord(
        name="cleaningai.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="request token: visible-token configured-secret-api-key",
        args=(),
        exc_info=None,
    )
    record.event = "test.failed"
    token = request_correlation_id.set("correlation-42")
    try:
        payload = json.loads(formatter.format(record))
    finally:
        request_correlation_id.reset(token)

    assert payload["service"] == "test-service"
    assert payload["event"] == "test.failed"
    assert payload["correlation_id"] == "correlation-42"
    assert "visible-token" not in payload["message"]
    assert "configured-secret-api-key" not in payload["message"]
    assert "[REDACTED]" in payload["message"]


def test_http_requests_return_safe_correlation_id(client, caplog):
    generated = client.get("/health")
    assert generated.status_code == 200
    assert len(generated.headers["X-Correlation-ID"]) == 32

    supplied = client.get("/health?token=must-not-be-logged", headers={"X-Correlation-ID": "request-123"})
    assert supplied.status_code == 200
    assert supplied.headers["X-Correlation-ID"] == "request-123"

    invalid = client.get("/health", headers={"X-Correlation-ID": "invalid value with spaces"})
    assert invalid.status_code == 200
    assert invalid.headers["X-Correlation-ID"] != "invalid value with spaces"
    request_records = [
        record for record in caplog.records if getattr(record, "event", "") == "http.request.completed"
    ]
    assert request_records
    assert all(getattr(record, "path", "") == "/health" for record in request_records)
    assert all("must-not-be-logged" not in record.getMessage() for record in request_records)
