from __future__ import annotations

import mercantile
import numpy as np

from app.config import Settings
from app.services.agb_stats_service import AgbTileStats, compute_agb_stats


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


def test_compute_agb_stats_returns_expected_core_metrics(monkeypatch):
	settings = Settings(
		agb_stats_histogram_min_mgha=0.0,
		agb_stats_histogram_max_mgha=300.0,
		agb_stats_histogram_bins=64,
		agb_stats_default_thresholds_mgha=[50.0, 100.0, 150.0],
	)

	class DummyReader:
		def header(self):
			return {"tile_type": "PNG", "max_zoom": 10}

	monkeypatch.setattr("app.services.agb_stats_service._build_pmtiles_reader", lambda _url: DummyReader())
	monkeypatch.setattr(
		"app.services.agb_stats_service.mercantile.tiles",
		lambda *args, **kwargs: [mercantile.Tile(x=1, y=1, z=10)],
	)
	monkeypatch.setattr("app.services.agb_stats_service._transform_geometry_to_crs", lambda geometry, _crs: geometry)
	monkeypatch.setattr("app.services.agb_stats_service._aoi_area_ha", lambda _geometry: 4.0)

	def _mock_tile(*_args, **_kwargs):
		return AgbTileStats(
			total_pixels=4,
			valid_pixels_2025=4,
			sum_agb_mgha_2025=100.0,
			sum_agb_sq_mgha2_2025=2700.0,
			min_agb_mgha_2025=10.0,
			max_agb_mgha_2025=40.0,
			valid_area_ha_2025=4.0,
			total_area_ha=4.0,
			total_agb_mg_2025=100.0,
			baseline_total_agb_mg=80.0,
			comparison_total_agb_mg=100.0,
			agb_increase_mg=25.0,
			agb_decrease_mg=5.0,
			agb_increase_area_ha=2.0,
			agb_decrease_area_ha=1.0,
			histogram_counts=np.array([1, 1, 1, 1] + [0] * 60, dtype=np.int64),
			threshold_counts={50.0: 3, 100.0: 1, 150.0: 0},
		)

	monkeypatch.setattr("app.services.agb_stats_service._process_agb_tile", _mock_tile)

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
	assert result["agbIncreaseMg"] == 25.0
	assert result["agbDecreaseMg"] == 5.0

	threshold_metrics = result["agbCoverByThreshold"]
	assert threshold_metrics[0]["thresholdMgHa"] == 50.0
	assert threshold_metrics[0]["coverRatio"] == 0.75
	assert threshold_metrics[1]["thresholdMgHa"] == 100.0
	assert threshold_metrics[1]["coverRatio"] == 0.25


def test_compute_agb_stats_uses_custom_thresholds(monkeypatch):
	settings = Settings(agb_stats_histogram_bins=64, agb_stats_default_thresholds_mgha=[50.0, 100.0, 150.0])

	class DummyReader:
		def header(self):
			return {"tile_type": "PNG", "max_zoom": 10}

	monkeypatch.setattr("app.services.agb_stats_service._build_pmtiles_reader", lambda _url: DummyReader())
	monkeypatch.setattr(
		"app.services.agb_stats_service.mercantile.tiles",
		lambda *args, **kwargs: [mercantile.Tile(x=1, y=1, z=10)],
	)
	monkeypatch.setattr("app.services.agb_stats_service._transform_geometry_to_crs", lambda geometry, _crs: geometry)
	monkeypatch.setattr("app.services.agb_stats_service._aoi_area_ha", lambda _geometry: 4.0)

	def _mock_tile(*_args, **_kwargs):
		return AgbTileStats(
			total_pixels=4,
			valid_pixels_2025=4,
			sum_agb_mgha_2025=40.0,
			sum_agb_sq_mgha2_2025=430.0,
			min_agb_mgha_2025=5.0,
			max_agb_mgha_2025=15.0,
			valid_area_ha_2025=4.0,
			total_area_ha=4.0,
			total_agb_mg_2025=40.0,
			baseline_total_agb_mg=30.0,
			comparison_total_agb_mg=40.0,
			agb_increase_mg=10.0,
			agb_decrease_mg=0.0,
			agb_increase_area_ha=1.0,
			agb_decrease_area_ha=0.0,
			histogram_counts=np.array([0, 1, 2, 1] + [0] * 60, dtype=np.int64),
			threshold_counts={75.0: 2, 25.0: 4},
		)

	monkeypatch.setattr("app.services.agb_stats_service._process_agb_tile", _mock_tile)

	result = compute_agb_stats(
		geojson_obj=_geojson_feature_collection(),
		settings=settings,
		agb_thresholds_mgha=[150.0, 25.0],
	)

	assert [entry["thresholdMgHa"] for entry in result["agbCoverByThreshold"]] == [25.0, 150.0]
