from __future__ import annotations

import numpy as np

from app.config import Settings
from app.services.agb_stats_service import AgbCogYearData, compute_agb_stats, validate_agb_stats_request_payload


def _geojson_feature_collection() -> dict:
	return {
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
							[106.8, -6.4],
							[106.8, -6.3],
							[106.7, -6.3],
							[106.7, -6.4],
						]
					],
				},
			}
		],
	}


def _outside_indonesia_geojson_feature_collection() -> dict:
	return {
		"type": "FeatureCollection",
		"features": [
			{
				"type": "Feature",
				"properties": {},
				"geometry": {
					"type": "Polygon",
					"coordinates": [
						[
							[-123.2, 37.6],
							[-122.9, 37.6],
							[-122.9, 37.9],
							[-123.2, 37.9],
							[-123.2, 37.6],
						]
					],
				},
			}
		],
	}


def test_agb_stats_validation_accepts_polygon_outside_indonesia() -> None:
	settings = Settings()
	validate_agb_stats_request_payload(_outside_indonesia_geojson_feature_collection(), settings)


def test_compute_agb_stats_returns_expected_core_metrics(monkeypatch):
	settings = Settings(
		agb_stats_histogram_min_mgha=0.0,
		agb_stats_histogram_max_mgha=300.0,
		agb_stats_histogram_bins=64,
		agb_stats_scale_factor=1.0,
		agb_stats_default_thresholds_mgha=[50.0, 100.0, 150.0],
	)
	monkeypatch.setattr("app.services.agb_stats_service._aoi_area_ha", lambda _geometry: 4.0)

	aoi_mask = np.array([[True, True], [True, True]], dtype=bool)
	comparison_values = np.array([[10.0, 20.0], [30.0, 40.0]], dtype=np.float64)
	baseline_values = np.array([[5.0, 15.0], [25.0, 35.0]], dtype=np.float64)

	def _mock_year_loader(*, year, geometry, settings):
		if year == settings.agb_stats_baseline_year:
			return AgbCogYearData(
				year=year,
				url="baseline.tif",
				scale_factor=1.0,
				values=baseline_values,
				aoi_mask=aoi_mask,
				valid_mask=aoi_mask,
			)
		return AgbCogYearData(
			year=year,
			url="comparison.tif",
			scale_factor=1.0,
			values=comparison_values,
			aoi_mask=aoi_mask,
			valid_mask=aoi_mask,
		)

	monkeypatch.setattr("app.services.agb_stats_service._load_agb_cog_year_data", _mock_year_loader)

	result = compute_agb_stats(
		geojson_obj=_geojson_feature_collection(),
		settings=settings,
	)

	assert result["baselineYear"] == 2000
	assert result["comparisonYear"] == 2025
	assert result["validPixelCount"] == 4
	assert result["coverageFraction"] == 1.0
	assert result["minAgbMgHa"] == 10.0
	assert result["maxAgbMgHa"] == 40.0
	assert result["meanAgbMgHa"] == 25.0
	assert result["totalAgbMg"] == 100.0
	assert result["totalAgbMgHa"] == 25.0
	assert result["baselineTotalAgbMg"] == 80.0
	assert result["netChangeAgbMg"] == 20.0
	assert result["netChangePercent"] == 25.0
	assert result["agbIncreaseMg"] == 20.0
	assert result["agbDecreaseMg"] == 0.0

	threshold_metrics = result["agbCoverByThreshold"]
	assert threshold_metrics[0]["thresholdMgHa"] == 50.0
	assert threshold_metrics[0]["coverRatio"] == 0.0
	assert threshold_metrics[1]["thresholdMgHa"] == 100.0
	assert threshold_metrics[1]["coverRatio"] == 0.0


def test_compute_agb_stats_uses_custom_thresholds(monkeypatch):
	settings = Settings(
		agb_stats_histogram_bins=64,
		agb_stats_scale_factor=1.0,
		agb_stats_default_thresholds_mgha=[50.0, 100.0, 150.0],
	)
	monkeypatch.setattr("app.services.agb_stats_service._aoi_area_ha", lambda _geometry: 4.0)

	aoi_mask = np.array([[True, True], [True, True]], dtype=bool)
	comparison_values = np.array([[5.0, 10.0], [10.0, 15.0]], dtype=np.float64)
	baseline_values = np.array([[5.0, 7.5], [8.0, 9.5]], dtype=np.float64)

	def _mock_year_loader(*, year, geometry, settings):
		if year == settings.agb_stats_baseline_year:
			return AgbCogYearData(
				year=year,
				url="baseline.tif",
				scale_factor=1.0,
				values=baseline_values,
				aoi_mask=aoi_mask,
				valid_mask=aoi_mask,
			)
		return AgbCogYearData(
			year=year,
			url="comparison.tif",
			scale_factor=1.0,
			values=comparison_values,
			aoi_mask=aoi_mask,
			valid_mask=aoi_mask,
		)

	monkeypatch.setattr("app.services.agb_stats_service._load_agb_cog_year_data", _mock_year_loader)

	result = compute_agb_stats(
		geojson_obj=_geojson_feature_collection(),
		settings=settings,
		agb_thresholds_mgha=[150.0, 25.0],
	)

	assert [entry["thresholdMgHa"] for entry in result["agbCoverByThreshold"]] == [25.0, 150.0]
