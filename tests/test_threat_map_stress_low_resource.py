from __future__ import annotations

import importlib
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient

from app.services.threat_map_service import JobProgressUpdate


def _load_main_with_env(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("CHM_API_KEY", "test-key")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.setenv("THREAT_MAP_ENABLED", "true")
    monkeypatch.setenv("THREAT_MAP_MAX_QUEUE_LENGTH", "3")
    monkeypatch.setenv("THREAT_MAP_TILE_FETCH_CONCURRENCY", "2")

    import app.config as config_module
    import app.services.threat_map_queue as threat_map_queue_module
    import app.main as main_module

    config_module.get_settings.cache_clear()
    threat_map_queue_module = importlib.reload(threat_map_queue_module)
    main_module = importlib.reload(main_module)
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
        "preset": "balanced",
    }


def _poll_terminal(client: TestClient, job_id: str, headers: dict[str, str], timeout_seconds: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/threat-map/jobs/{job_id}", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"succeeded", "partial_success", "failed", "cancelled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for job {job_id} terminal state")


def test_low_resource_stress_queue_and_worker_invariants(monkeypatch, tmp_path: Path):
    main_module, queue_module = _load_main_with_env(monkeypatch, tmp_path)

    state_lock = threading.Lock()
    started_order: list[str] = []
    active_count = 0
    max_active = 0

    def _mock_process_job(*, settings, job_id, payload, progress_callback, is_cancelled):
        nonlocal active_count, max_active

        with state_lock:
            started_order.append(job_id)
            active_count += 1
            max_active = max(max_active, active_count)

        progress_callback(JobProgressUpdate(progress=50, message=f"Encoding {job_id}", current_year=1990))
        time.sleep(0.12)
        progress_callback(JobProgressUpdate(progress=95, message=f"Encoding {job_id}", current_year=2024))

        out = settings.outputs_dir / f"threat_map_{job_id}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"stress-mp4")

        with state_lock:
            active_count -= 1

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
        responses = [
            client.post("/api/v1/threat-map/jobs", json=_valid_payload(), headers=headers)
            for _ in range(4)
        ]

        assert responses[0].status_code == 202
        assert responses[0].json()["status"] == "queued"
        assert responses[1].status_code == 202
        assert responses[1].json()["status"] == "deferred"
        assert responses[2].status_code == 202
        assert responses[2].json()["status"] == "deferred"

        # Queue length is capped at 3 pending jobs on this constrained profile.
        assert responses[3].status_code == 429

        accepted_ids = [responses[i].json()["jobId"] for i in range(3)]

        terminals = [_poll_terminal(client, job_id, headers) for job_id in accepted_ids]
        assert all(job["status"] == "succeeded" for job in terminals)

    # FIFO invariant: the worker should start jobs in submission order.
    assert started_order == accepted_ids

    # Single-worker invariant under low-resource settings.
    assert max_active == 1
