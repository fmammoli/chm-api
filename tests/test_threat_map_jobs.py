from __future__ import annotations

import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from app.services.threat_map_queue import stop_threat_map_worker


def _load_main_with_env(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("CHM_API_KEY", "test-key")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.setenv("THREAT_MAP_ENABLED", "false")
    monkeypatch.setenv("THREAT_MAP_MAX_QUEUE_LENGTH", "2")

    import app.config as config_module
    import app.main as main_module

    config_module.get_settings.cache_clear()
    main_module = importlib.reload(main_module)
    stop_threat_map_worker()
    client = TestClient(main_module.app, base_url="http://localhost")
    return client


def _valid_payload() -> dict:
    return {
        "geojson": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [106.7, -6.4],
                                [107.0, -6.4],
                                [107.0, -6.1],
                                [106.7, -6.1],
                                [106.7, -6.4],
                            ]
                        ],
                    },
                    "properties": {},
                }
            ],
        },
        "preset": "balanced",
    }


def test_threat_map_queue_deferred_and_full(monkeypatch, tmp_path: Path):
    client = _load_main_with_env(monkeypatch, tmp_path)
    headers = {"host": "localhost", "X-API-Key": "test-key"}

    first = client.post("/api/v1/threat-map/jobs", json=_valid_payload(), headers=headers)
    assert first.status_code == 202
    assert first.json()["status"] == "queued"

    second = client.post("/api/v1/threat-map/jobs", json=_valid_payload(), headers=headers)
    assert second.status_code == 202
    assert second.json()["status"] == "deferred"

    third = client.post("/api/v1/threat-map/jobs", json=_valid_payload(), headers=headers)
    assert third.status_code == 429


def test_threat_map_get_returns_not_found(monkeypatch, tmp_path: Path):
    client = _load_main_with_env(monkeypatch, tmp_path)
    headers = {"host": "localhost", "X-API-Key": "test-key"}

    response = client.get("/api/v1/threat-map/jobs/not-real", headers=headers)
    assert response.status_code == 404
    assert response.json()["message"] == "Job not found"


def test_threat_map_download_preflight_allows_localhost_origin(monkeypatch, tmp_path: Path):
    client = _load_main_with_env(monkeypatch, tmp_path)
    headers = {
        "host": "127.0.0.1",
        "origin": "http://localhost:3000",
        "access-control-request-method": "GET",
        "access-control-request-headers": "x-api-key",
    }

    response = client.options("/api/v1/threat-map/jobs/not-real/download", headers=headers)

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"
