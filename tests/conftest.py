import os
import sys
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite:///./test_cleaningai.db"
os.environ["ENVIRONMENT"] = "test"
# Unit and integration tests must never inherit live model credentials from a
# developer's local .env. Tests that exercise adapters opt in with monkeypatch.
os.environ["LLM_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["PERPLEXITY_API_KEY"] = ""
os.environ["TENDER_SEARCH_ENABLED"] = "false"
Path("test_cleaningai.db").unlink(missing_ok=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
from app.main import app

@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
