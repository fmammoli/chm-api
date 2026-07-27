import importlib
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_logs_endpoint_returns_ndjson(monkeypatch):
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    monkeypatch.setenv("REQUIRE_API_KEY", "false")

    import app.main as main_module

    main_module = importlib.reload(main_module)
    client = TestClient(main_module.app, base_url="http://localhost")

    response = client.get("/api/v1/logs", params={"tail": 3}, headers={"host": "localhost"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")

    lines = [line for line in response.text.splitlines() if line.strip()]
    assert lines, "expected at least one logged event"

    payload = json.loads(lines[-1])
    assert "seq" in payload
    assert "message" in payload
    assert isinstance(payload["message"], str)


def test_logs_ui_page_renders_form(monkeypatch):
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    monkeypatch.setenv("REQUIRE_API_KEY", "false")

    import app.main as main_module

    main_module = importlib.reload(main_module)
    client = TestClient(main_module.app, base_url="http://localhost")

    response = client.get("/logs-ui", headers={"host": "localhost"})

    assert response.status_code == 200
    assert "Follow logs" in response.text
    assert "/api/v1/logs" in response.text
