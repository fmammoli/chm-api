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
        "canopyThresholdsM": [5, 10, 20],
    }


def test_create_chm_stats_job_returns_202(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    monkeypatch.setattr(main_module, "validate_chm_stats_request_payload", lambda geojson, settings: None)
    monkeypatch.setattr(main_module, "run_chm_stats_job", lambda settings, job_id, geojson_obj, canopy_thresholds_m: None)

    response = client.post(
        "/api/v1/chm/stats/jobs",
        json=_valid_payload(),
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["jobId"]


def test_create_chm_stats_job_queue_full(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    monkeypatch.setattr(main_module, "validate_chm_stats_request_payload", lambda geojson, settings: None)
    monkeypatch.setattr(
        main_module,
        "get_queue_snapshot",
        lambda _settings: {"queued": 6, "running": 0, "succeeded": 0, "failed": 0},
    )

    response = client.post(
        "/api/v1/chm/stats/jobs",
        json=_valid_payload(),
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )

    assert response.status_code == 429
    assert response.json()["detail"] == "Job queue is full. Please retry in a few minutes."


def test_create_chm_stats_job_validation_422(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    def _raise_validation(_geojson, _settings):
        raise ServiceValidationError("Invalid polygon")

    monkeypatch.setattr(main_module, "validate_chm_stats_request_payload", _raise_validation)

    response = client.post(
        "/api/v1/chm/stats/jobs",
        json=_valid_payload(),
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )

    assert response.status_code == 422
    assert response.json()["message"] == "Invalid polygon"


def test_get_chm_stats_job_not_found(monkeypatch, tmp_path: Path):
    client, _ = _load_main_with_env(monkeypatch, tmp_path)

    response = client.get(
        "/api/v1/chm/stats/jobs/missing-job",
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )

    assert response.status_code == 404
    assert response.json()["message"] == "Job not found"


def test_get_chm_stats_job_succeeded_payload(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    created = create_job(main_module.settings, message="CHM stats job created")

    succeeded_payload = {
        "jobId": created["jobId"],
        "status": "succeeded",
        "createdAt": created["createdAt"],
        "startedAt": created["createdAt"],
        "finishedAt": created["createdAt"],
        "progress": 100,
        "etaSeconds": 0,
        "message": "CHM stats completed",
        "result": {
            "minCanopyHeightM": 1.0,
            "maxCanopyHeightM": 30.0,
            "meanCanopyHeightM": 12.25,
            "medianCanopyHeightM": 11.5,
            "stdDevCanopyHeightM": 6.5,
            "varianceCanopyHeightM2": 42.25,
            "p10CanopyHeightM": 2.0,
            "p25CanopyHeightM": 6.0,
            "p75CanopyHeightM": 16.0,
            "p90CanopyHeightM": 25.0,
            "p95CanopyHeightM": 28.0,
            "interquartileRangeM": 10.0,
            "coefficientOfVariation": 0.53,
            "totalCanopyVolumeProxyM3": 490000.0,
            "analyzedAreaHa": 4.0,
            "aoiAreaHa": 4.0,
            "coverageFraction": 1.0,
            "validPixelCount": 4,
            "canopyCoverByThreshold": [
                {"thresholdM": 5.0, "coverRatio": 0.75, "coverPercent": 75.0, "coverAreaHa": 3.0},
                {"thresholdM": 10.0, "coverRatio": 0.5, "coverPercent": 50.0, "coverAreaHa": 2.0},
            ],
            "metadata": {"sourceFormat": "pmtiles_png", "zoom": 10},
        },
        "error": None,
    }

    monkeypatch.setattr(main_module, "get_job", lambda _settings, _job_id: succeeded_payload)

    response = client.get(
        f"/api/v1/chm/stats/jobs/{created['jobId']}",
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "succeeded"
    assert payload["result"]["meanCanopyHeightM"] == 12.25
