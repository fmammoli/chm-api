import importlib
import os

from fastapi.testclient import TestClient


def test_health_accepts_public_host_header(monkeypatch):
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    monkeypatch.setenv("REQUIRE_API_KEY", "false")
    monkeypatch.setenv("CHM_API_KEY", "test-key")

    import app.main as main_module

    main_module = importlib.reload(main_module)
    client = TestClient(main_module.app)

    response = client.get("/health", headers={"host": "178.104.153.106"})

    assert response.status_code == 200
