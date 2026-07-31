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
	}


def test_create_agb_stats_job_returns_202(monkeypatch, tmp_path: Path):
	client, main_module = _load_main_with_env(monkeypatch, tmp_path)

	monkeypatch.setattr(main_module, "validate_agb_stats_request_payload", lambda geojson, settings: None)
	monkeypatch.setattr(main_module, "run_agb_stats_job", lambda settings, job_id, geojson_obj: None)

	response = client.post(
		"/api/v1/ctrees/agb/stats/jobs",
		json=_valid_payload(),
		headers={"host": "localhost", "X-API-Key": "test-key"},
	)

	assert response.status_code == 202
	payload = response.json()
	assert payload["status"] == "queued"
	assert payload["jobId"]


def test_create_agb_stats_job_queue_full(monkeypatch, tmp_path: Path):
	client, main_module = _load_main_with_env(monkeypatch, tmp_path)

	monkeypatch.setattr(main_module, "validate_agb_stats_request_payload", lambda geojson, settings: None)
	monkeypatch.setattr(
		main_module,
		"get_queue_snapshot",
		lambda _settings: {"queued": 6, "running": 0, "succeeded": 0, "failed": 0},
	)

	response = client.post(
		"/api/v1/ctrees/agb/stats/jobs",
		json=_valid_payload(),
		headers={"host": "localhost", "X-API-Key": "test-key"},
	)

	assert response.status_code == 429
	assert response.json()["detail"] == "Job queue is full. Please retry in a few minutes."


def test_create_agb_stats_job_validation_422(monkeypatch, tmp_path: Path):
	client, main_module = _load_main_with_env(monkeypatch, tmp_path)

	def _raise_validation(_geojson, _settings):
		raise ServiceValidationError("Invalid polygon")

	monkeypatch.setattr(main_module, "validate_agb_stats_request_payload", _raise_validation)

	response = client.post(
		"/api/v1/ctrees/agb/stats/jobs",
		json=_valid_payload(),
		headers={"host": "localhost", "X-API-Key": "test-key"},
	)

	assert response.status_code == 422
	assert response.json()["message"] == "Invalid polygon"


def test_get_agb_stats_job_not_found(monkeypatch, tmp_path: Path):
	client, _ = _load_main_with_env(monkeypatch, tmp_path)

	response = client.get(
		"/api/v1/ctrees/agb/stats/jobs/missing-job",
		headers={"host": "localhost", "X-API-Key": "test-key"},
	)

	assert response.status_code == 404
	assert response.json()["message"] == "Job not found"


def test_get_agb_stats_job_succeeded_payload(monkeypatch, tmp_path: Path):
	client, main_module = _load_main_with_env(monkeypatch, tmp_path)

	created = create_job(main_module.settings, message="AGB stats job created")

	succeeded_payload = {
		"jobId": created["jobId"],
		"status": "succeeded",
		"createdAt": created["createdAt"],
		"startedAt": created["createdAt"],
		"finishedAt": created["createdAt"],
		"progress": 100,
		"etaSeconds": 0,
		"message": "AGB stats completed",
		"result": {
			"baselineYear": 2000,
			"comparisonYear": 2025,
			"minAgbMgHa": 10.0,
			"maxAgbMgHa": 40.0,
			"meanAgbMgHa": 25.0,
			"medianAgbMgHa": 24.0,
			"stdDevAgbMgHa": 6.5,
			"varianceAgbMgHa2": 42.25,
			"p10AgbMgHa": 12.0,
			"p25AgbMgHa": 18.0,
			"p75AgbMgHa": 31.0,
			"p90AgbMgHa": 36.0,
			"p95AgbMgHa": 38.0,
			"interquartileRangeMgHa": 13.0,
			"coefficientOfVariation": 0.26,
			"totalAgbMg": 100.0,
			"totalAgbMgHa": 25.0,
			"baselineTotalAgbMg": 80.0,
			"comparisonTotalAgbMg": 100.0,
			"agbIncreaseMg": 25.0,
			"agbDecreaseMg": 5.0,
			"netChangeAgbMg": 20.0,
			"netChangeAgbMgHa": 5.0,
			"netChangePercent": 25.0,
			"agbIncreaseAreaHa": 2.0,
			"agbDecreaseAreaHa": 1.0,
			"analyzedAreaHa": 4.0,
			"aoiAreaHa": 4.0,
			"coverageFraction": 1.0,
			"validPixelCount": 4,
			"agbCoverByThreshold": [
				{"thresholdMgHa": 50.0, "coverRatio": 0.75, "coverPercent": 75.0, "coverAreaHa": 3.0},
				{"thresholdMgHa": 100.0, "coverRatio": 0.25, "coverPercent": 25.0, "coverAreaHa": 1.0},
			],
			"metadata": {"sourceFormat": "pmtiles_png", "zoom": 10},
		},
		"error": None,
	}

	monkeypatch.setattr(main_module, "get_job", lambda _settings, _job_id: succeeded_payload)

	response = client.get(
		f"/api/v1/ctrees/agb/stats/jobs/{created['jobId']}",
		headers={"host": "localhost", "X-API-Key": "test-key"},
	)

	assert response.status_code == 200
	payload = response.json()
	assert payload["status"] == "succeeded"
	assert payload["result"]["baselineYear"] == 2000
	assert payload["result"]["comparisonYear"] == 2025
	assert payload["result"]["netChangeAgbMg"] == 20.0