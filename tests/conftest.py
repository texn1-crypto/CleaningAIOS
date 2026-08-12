import os
import sys
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite:///./test_cleaningai.db"
Path("test_cleaningai.db").unlink(missing_ok=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
from app.main import app

@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
