import io
import json
import ssl
import traceback
import urllib.error

import pytest

from app import deployment_probes as probes


def _transport(monkeypatch, outcomes):
    calls, sleeps = [], []
    responses = iter(outcomes)
    def open_url(url, timeout):
        calls.append((url, timeout))
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(result)
    monkeypatch.setattr(probes.urllib.request, "urlopen", open_url)
    monkeypatch.setattr(probes.time, "sleep", sleeps.append)
    return calls, sleeps


OK = json.dumps({"ok": True, "result": {"id": 123, "is_bot": True}}).encode()


def test_probe_first_success_does_not_retry(monkeypatch):
    calls, sleeps = _transport(monkeypatch, [OK])
    assert probes.verify_telegram_identity("test-secret") == 1
    assert calls == [("https://api.telegram.org/bottest-secret/getMe", 10)]
    assert sleeps == []


def test_probe_retries_transient_transport_and_server_error(monkeypatch):
    calls, sleeps = _transport(monkeypatch, [
        urllib.error.URLError(TimeoutError("test-secret")),
        urllib.error.HTTPError("secret-url", 503, "temporary", {}, None), OK,
    ])
    assert probes.verify_telegram_identity("test-secret") == 3
    assert len(calls) == 3 and sleeps == [2, 4]


def test_probe_exhaustion_is_bounded_and_secret_safe(monkeypatch):
    token = "test-secret"
    calls, sleeps = _transport(monkeypatch, [TimeoutError(token)] * 3)
    with pytest.raises(probes.TelegramProbeFailed) as caught:
        probes.verify_telegram_identity(token)
    assert token not in "".join(traceback.format_exception(caught.value))
    assert len(calls) == 3 and sleeps == [2, 4]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
def test_probe_never_retries_auth_client_or_rate_limit_rejection(monkeypatch, status):
    token = "test-secret"
    calls, sleeps = _transport(monkeypatch, [urllib.error.HTTPError("secret-url", status, token, {}, None)])
    with pytest.raises(probes.TelegramProbeFailed) as caught:
        probes.verify_telegram_identity(token)
    assert token not in "".join(traceback.format_exception(caught.value))
    assert len(calls) == 1 and sleeps == []


@pytest.mark.parametrize("body", [b"not-json", b"{}", b"[]", b'{"ok":false}',
    b'{"ok":true,"result":{"id":123,"is_bot":false}}', b"x" * 65537])
def test_probe_invalid_identity_never_passes_or_retries(monkeypatch, body):
    calls, sleeps = _transport(monkeypatch, [body])
    with pytest.raises(probes.TelegramProbeFailed):
        probes.verify_telegram_identity("test-secret")
    assert len(calls) == 1 and sleeps == []


@pytest.mark.parametrize("wrapped", [False, True])
def test_probe_does_not_retry_certificate_failure(monkeypatch, wrapped):
    token = "test-secret"
    error = ssl.SSLCertVerificationError(token)
    calls, sleeps = _transport(monkeypatch, [urllib.error.URLError(error) if wrapped else error])
    with pytest.raises(probes.TelegramProbeFailed, match="TLS") as caught:
        probes.verify_telegram_identity(token)
    assert token not in "".join(traceback.format_exception(caught.value))
    assert len(calls) == 1 and sleeps == []


def test_probe_missing_token_makes_no_request(monkeypatch):
    calls, sleeps = _transport(monkeypatch, [])
    with pytest.raises(probes.TelegramProbeFailed, match="missing"):
        probes.verify_telegram_identity(" ")
    assert calls == sleeps == []
