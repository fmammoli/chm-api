from __future__ import annotations

import importlib
from pathlib import Path
import tarfile
import time
import zipfile

from fastapi.testclient import TestClient

from app.services.threat_map_service import JobProgressUpdate


def _load_main_with_env(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("CHM_API_KEY", "test-key")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.setenv("THREAT_MAP_ENABLED", "true")
    monkeypatch.setenv("THREAT_MAP_MAX_QUEUE_LENGTH", "4")

    import app.config as config_module
    import app.main as main_module
    import app.services.threat_map_queue as threat_map_queue_module

    config_module.get_settings.cache_clear()
    main_module = importlib.reload(main_module)
    threat_map_queue_module = importlib.reload(threat_map_queue_module)
    return main_module, threat_map_queue_module


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
        "geojsonCrs": "EPSG:4326",
        "preset": "balanced",
        "outputFormat": "mp4",
    }


def _poll_until_done(client: TestClient, job_id: str, headers: dict[str, str], timeout_seconds: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/threat-map/jobs/{job_id}", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"succeeded", "partial_success", "failed", "cancelled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for threat-map terminal state")


def test_create_poll_download_mp4(monkeypatch, tmp_path: Path):
    main_module, queue_module = _load_main_with_env(monkeypatch, tmp_path)

    def _mock_process_job(*, settings, job_id, payload, progress_callback, is_cancelled):
        progress_callback(JobProgressUpdate(progress=25, message="Encoded year 1990", current_year=1990))
        progress_callback(JobProgressUpdate(progress=95, message="Encoded year 2024", current_year=2024))

        out = settings.outputs_dir / f"threat_map_{job_id}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake-mp4")
        return {
            "status": "succeeded",
            "artifactType": "mp4",
            "contentType": "video/mp4",
            "sizeBytes": out.stat().st_size,
            "yearsRendered": 35,
            "yearsExpected": 35,
            "warnings": [],
        }

    monkeypatch.setattr(queue_module, "process_threat_map_job", _mock_process_job)

    headers = {"host": "localhost", "X-API-Key": "test-key"}
    with TestClient(main_module.app, base_url="http://localhost") as client:
        create_resp = client.post("/api/v1/threat-map/jobs", json=_valid_payload(), headers=headers)
        assert create_resp.status_code == 202
        job_id = create_resp.json()["jobId"]

        terminal = _poll_until_done(client, job_id, headers)
        assert terminal["status"] == "succeeded"
        assert terminal["currentYear"] == 2024

        download = client.get(f"/api/v1/threat-map/jobs/{job_id}/download", headers=headers)
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("video/mp4")
        assert download.content == b"fake-mp4"


def test_create_poll_download_zip_partial_success(monkeypatch, tmp_path: Path):
    main_module, queue_module = _load_main_with_env(monkeypatch, tmp_path)

    def _mock_process_job(*, settings, job_id, payload, progress_callback, is_cancelled):
        progress_callback(JobProgressUpdate(progress=30, message="Fallback frame year 1990", current_year=1990))
        progress_callback(JobProgressUpdate(progress=95, message="Fallback frame year 2024", current_year=2024))

        out = settings.outputs_dir / f"threat_map_{job_id}.zip"
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("frame_1990.png", b"a")
            archive.writestr("frame_2024.png", b"b")

        return {
            "status": "partial_success",
            "artifactType": "zip",
            "contentType": "application/zip",
            "sizeBytes": out.stat().st_size,
            "yearsRendered": 35,
            "yearsExpected": 35,
            "warnings": ["mp4 encode failed, fallback to zip"],
            "fallbackReasonCode": "resource_limit",
        }

    monkeypatch.setattr(queue_module, "process_threat_map_job", _mock_process_job)

    headers = {"host": "localhost", "X-API-Key": "test-key"}
    with TestClient(main_module.app, base_url="http://localhost") as client:
        create_resp = client.post("/api/v1/threat-map/jobs", json=_valid_payload(), headers=headers)
        assert create_resp.status_code == 202
        job_id = create_resp.json()["jobId"]

        terminal = _poll_until_done(client, job_id, headers)
        assert terminal["status"] == "partial_success"
        assert terminal["currentYear"] == 2024

        download = client.get(f"/api/v1/threat-map/jobs/{job_id}/download", headers=headers)
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("application/zip")


def test_create_poll_download_frames_tar_gz(monkeypatch, tmp_path: Path):
    main_module, queue_module = _load_main_with_env(monkeypatch, tmp_path)

    def _mock_process_job(*, settings, job_id, payload, progress_callback, is_cancelled):
        progress_callback(JobProgressUpdate(progress=40, message="Frame packaged for year 1990", current_year=1990))
        progress_callback(JobProgressUpdate(progress=95, message="Frame packaged for year 2024", current_year=2024))

        out = settings.outputs_dir / f"threat_map_{job_id}_frames.tar.gz"
        out.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out, mode="w:gz") as archive:
            manifest = tmp_path / "manifest.json"
            manifest.write_text('{"artifactType":"frames_tar_gz"}', encoding="utf-8")
            frame = tmp_path / "frame_1990.png"
            frame.write_bytes(b"fake-png")
            archive.add(manifest, arcname="manifest.json")
            archive.add(frame, arcname="frames/frame_1990.png")

        return {
            "status": "succeeded",
            "artifactType": "frames_tar_gz",
            "contentType": "application/gzip",
            "sizeBytes": out.stat().st_size,
            "yearsRendered": 35,
            "yearsExpected": 35,
            "warnings": [],
        }

    monkeypatch.setattr(queue_module, "process_threat_map_job", _mock_process_job)

    headers = {"host": "localhost", "X-API-Key": "test-key"}
    payload = _valid_payload() | {"outputFormat": "frames_tar_gz"}
    with TestClient(main_module.app, base_url="http://localhost") as client:
        create_resp = client.post("/api/v1/threat-map/jobs", json=payload, headers=headers)
        assert create_resp.status_code == 202
        job_id = create_resp.json()["jobId"]

        terminal = _poll_until_done(client, job_id, headers)
        assert terminal["status"] == "succeeded"
        assert terminal["result"]["artifactType"] == "frames_tar_gz"

        download = client.get(f"/api/v1/threat-map/jobs/{job_id}/download", headers=headers)
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("application/gzip")


def test_create_accepts_overlay_geojson(monkeypatch, tmp_path: Path):
    main_module, queue_module = _load_main_with_env(monkeypatch, tmp_path)

    def _mock_process_job(*, settings, job_id, payload, progress_callback, is_cancelled):
        assert payload.overlayGeojson is not None
        progress_callback(JobProgressUpdate(progress=95, message="Frame packaged for year 2024", current_year=2024))

        out = settings.outputs_dir / f"threat_map_{job_id}_frames.tar.gz"
        out.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out, mode="w:gz") as archive:
            manifest = tmp_path / "manifest_overlay.json"
            manifest.write_text('{"artifactType":"frames_tar_gz"}', encoding="utf-8")
            archive.add(manifest, arcname="manifest.json")

        return {
            "status": "succeeded",
            "artifactType": "frames_tar_gz",
            "contentType": "application/gzip",
            "sizeBytes": out.stat().st_size,
            "yearsRendered": 35,
            "yearsExpected": 35,
            "warnings": [],
        }

    monkeypatch.setattr(queue_module, "process_threat_map_job", _mock_process_job)

    headers = {"host": "localhost", "X-API-Key": "test-key"}
    payload = _valid_payload() | {
        "outputFormat": "frames_tar_gz",
        "overlayGeojson": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [106.78, -6.33],
                                [106.92, -6.33],
                                [106.92, -6.19],
                                [106.78, -6.19],
                                [106.78, -6.33],
                            ]
                        ],
                    },
                    "properties": {},
                }
            ],
        },
        "overlayGeojsonCrs": "EPSG:4326",
    }

    with TestClient(main_module.app, base_url="http://localhost") as client:
        create_resp = client.post("/api/v1/threat-map/jobs", json=payload, headers=headers)
        assert create_resp.status_code == 202
        job_id = create_resp.json()["jobId"]

        terminal = _poll_until_done(client, job_id, headers)
        assert terminal["status"] == "succeeded"
