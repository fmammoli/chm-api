import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from app.services.chm_service import ServiceValidationError
from app.services.job_service import create_job


def _load_main_with_env(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("CHM_API_KEY", "test-key")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("OUTPUTS_DIR", str(tmp_path / "outputs"))

    import app.config as config_module
    import app.main as main_module

    config_module.get_settings.cache_clear()
    main_module = importlib.reload(main_module)
    client = TestClient(main_module.app, base_url="http://localhost")
    return client, main_module


def _valid_payload() -> dict:
    return {
        "geojson": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
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
                }
            ],
        },
        "baselineYear": 1990,
        "comparisonYear": 2024,
    }


def test_create_landcover_job_returns_202(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    monkeypatch.setattr(main_module, "validate_landcover_request_payload", lambda geojson, settings: None)
    monkeypatch.setattr(
        main_module,
        "run_landcover_stats_job",
        lambda settings, job_id, geojson_obj, baseline_year, comparison_year: None,
    )

    response = client.post(
        "/api/v1/landcover/stats/jobs",
        json=_valid_payload(),
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["jobId"]


def test_create_landcover_job_invalid_api_key(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    monkeypatch.setattr(main_module, "validate_landcover_request_payload", lambda geojson, settings: None)

    response = client.post(
        "/api/v1/landcover/stats/jobs",
        json=_valid_payload(),
        headers={"host": "localhost", "X-API-Key": "wrong"},
    )

    assert response.status_code == 401


def test_create_landcover_job_queue_full(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    monkeypatch.setattr(main_module, "validate_landcover_request_payload", lambda geojson, settings: None)
    monkeypatch.setattr(
        main_module,
        "get_queue_snapshot",
        lambda _settings: {"queued": 6, "running": 0, "succeeded": 0, "failed": 0},
    )

    response = client.post(
        "/api/v1/landcover/stats/jobs",
        json=_valid_payload(),
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )

    assert response.status_code == 429
    assert response.json()["detail"] == "Job queue is full. Please retry in a few minutes."


def test_create_landcover_job_validation_422(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    def _raise_validation(_geojson, _settings):
        raise ServiceValidationError("Invalid polygon")

    monkeypatch.setattr(main_module, "validate_landcover_request_payload", _raise_validation)

    response = client.post(
        "/api/v1/landcover/stats/jobs",
        json=_valid_payload(),
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )

    assert response.status_code == 422
    assert response.json()["message"] == "Invalid polygon"


def test_get_landcover_job_not_found(monkeypatch, tmp_path: Path):
    client, _ = _load_main_with_env(monkeypatch, tmp_path)

    response = client.get(
        "/api/v1/landcover/stats/jobs/missing-job",
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )

    assert response.status_code == 404
    assert response.json()["message"] == "Job not found"


def test_get_landcover_job_succeeded_payload(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    created = create_job(main_module.settings, message="Landcover stats job created")

    succeeded_payload = {
        "jobId": created["jobId"],
        "status": "succeeded",
        "createdAt": created["createdAt"],
        "startedAt": created["createdAt"],
        "finishedAt": created["createdAt"],
        "progress": 100,
        "etaSeconds": 0,
        "message": "Landcover stats completed",
        "result": {
            "baselineYear": 1990,
            "comparisonYear": 2024,
            "forestLossHa": 12.34,
            "forestGainHa": 4.56,
            "forestLossPct": 10.2833,
            "forestGainPct": 3.8,
            "netForestChangeHa": -7.78,
            "baselineForestAreaHa": 100.0,
            "comparisonForestAreaHa": 92.22,
            "analyzedAreaHa": 120.0,
            "aoiAreaHa": 120.0,
            "coverageFraction": 1.0,
            "validPixelCount": 900,
            "metadata": {"baselineUrl": "https://example/1990.tif", "comparisonUrl": "https://example/2024.tif"},
        },
        "error": None,
    }

    monkeypatch.setattr(main_module, "get_job", lambda _settings, _job_id: succeeded_payload)

    response = client.get(
        f"/api/v1/landcover/stats/jobs/{created['jobId']}",
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "succeeded"
    assert payload["result"]["baselineYear"] == 1990
    assert payload["result"]["comparisonYear"] == 2024
    assert payload["result"]["forestLossHa"] == 12.34
