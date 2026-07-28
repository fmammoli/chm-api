import importlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.services.chm_service import ServiceValidationError
from app.services.job_service import create_job, job_output_path, mark_job_failed, mark_job_running, mark_job_succeeded


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
        }
    }


def test_create_job_returns_202(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    monkeypatch.setattr(main_module, "validate_chm_request_payload", lambda geojson, settings: None)
    monkeypatch.setattr(main_module, "run_chm_job", lambda settings, job_id, geojson_obj: None)

    response = client.post(
        "/api/v1/chm/jobs",
        json=_valid_payload(),
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["jobId"]


def test_create_job_invalid_api_key(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    monkeypatch.setattr(main_module, "validate_chm_request_payload", lambda geojson, settings: None)

    response = client.post(
        "/api/v1/chm/jobs",
        json=_valid_payload(),
        headers={"host": "localhost", "X-API-Key": "wrong"},
    )

    assert response.status_code == 401


def test_create_job_rejects_when_queue_is_full(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    monkeypatch.setattr(main_module, "validate_chm_request_payload", lambda geojson, settings: None)
    monkeypatch.setattr(
        main_module,
        "get_queue_snapshot",
        lambda _settings: {"queued": 6, "running": 0, "succeeded": 0, "failed": 0},
    )

    response = client.post(
        "/api/v1/chm/jobs",
        json=_valid_payload(),
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )

    assert response.status_code == 429
    payload = response.json()
    assert payload["detail"] == "Job queue is full. Please retry in a few minutes."


def test_legacy_crop_rejects_when_queue_is_full(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    monkeypatch.setattr(main_module, "validate_chm_request_payload", lambda geojson, settings: None)
    monkeypatch.setattr(
        main_module,
        "get_queue_snapshot",
        lambda _settings: {"queued": 6, "running": 0, "succeeded": 0, "failed": 0},
    )

    response = client.post(
        "/api/v1/chm/crop",
        json=_valid_payload(),
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )

    assert response.status_code == 429
    payload = response.json()
    assert payload["detail"] == "Job queue is full. Please retry in a few minutes."


def test_get_job_status_invalid_api_key(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)
    created = create_job(main_module.settings)

    response = client.get(
        f"/api/v1/chm/jobs/{created['jobId']}",
        headers={"host": "localhost", "X-API-Key": "wrong"},
    )

    assert response.status_code == 401


def test_create_job_oversized_aoi_returns_422(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    def _raise_validation(_geojson, _settings):
        raise ServiceValidationError(
            "AOI square side is 72.50 km (width=72.50 km, height=71.90 km). Maximum allowed is 30.0 km."
        )

    monkeypatch.setattr(main_module, "validate_chm_request_payload", _raise_validation)

    response = client.post(
        "/api/v1/chm/jobs",
        json=_valid_payload(),
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )

    assert response.status_code == 422
    payload = response.json()
    assert "72.50 km" in payload["message"]
    assert "30.0 km" in payload["message"]


def test_get_job_status_states(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    created = create_job(main_module.settings)

    queued_response = client.get(
        f"/api/v1/chm/jobs/{created['jobId']}",
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )
    assert queued_response.status_code == 200
    assert queued_response.json()["status"] == "queued"

    mark_job_running(main_module.settings, created["jobId"])
    running_response = client.get(
        f"/api/v1/chm/jobs/{created['jobId']}",
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )
    assert running_response.status_code == 200
    assert running_response.json()["status"] == "running"

    mark_job_failed(main_module.settings, created["jobId"], code="generation_failed", message="boom")
    failed_response = client.get(
        f"/api/v1/chm/jobs/{created['jobId']}",
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )
    assert failed_response.status_code == 200
    failed_payload = failed_response.json()
    assert failed_payload["status"] == "failed"
    assert failed_payload["error"]["code"] == "generation_failed"


def test_download_only_after_success(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    created = create_job(main_module.settings)

    not_ready = client.get(
        f"/api/v1/chm/jobs/{created['jobId']}/download",
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )
    assert not_ready.status_code == 409

    output_path = job_output_path(main_module.settings, created["jobId"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    geotiff_stub = b"II*\x00fake-geotiff-content"
    output_path.write_bytes(geotiff_stub)
    mark_job_succeeded(main_module.settings, created["jobId"], output_file_size=len(geotiff_stub))

    status_after_success = client.get(
        f"/api/v1/chm/jobs/{created['jobId']}",
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )
    assert status_after_success.status_code == 200
    status_payload = status_after_success.json()
    assert status_payload["status"] == "succeeded"
    assert status_payload["result"]["downloadUrl"].endswith(f"/api/v1/chm/jobs/{created['jobId']}/download")
    assert status_payload["result"]["contentType"] == "image/tiff"

    ready = client.get(
        f"/api/v1/chm/jobs/{created['jobId']}/download",
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )
    assert ready.status_code == 200
    assert ready.headers["content-type"].startswith("image/tiff")
    assert ready.content == geotiff_stub


def test_failed_job_has_structured_error(monkeypatch, tmp_path: Path):
    client, main_module = _load_main_with_env(monkeypatch, tmp_path)

    created = create_job(main_module.settings)
    mark_job_failed(
        main_module.settings,
        created["jobId"],
        code="generation_failed",
        message="Detailed backend error message",
    )

    response = client.get(
        f"/api/v1/chm/jobs/{created['jobId']}",
        headers={"host": "localhost", "X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["error"] == {
        "code": "generation_failed",
        "message": "Detailed backend error message",
    }
    assert payload["finishedAt"] is not None
