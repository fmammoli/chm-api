from __future__ import annotations

import mercantile
import numpy as np

from app.config import Settings
from app.services.chm_stats_service import TileChmStats, _decode_rgb_height_values, compute_chm_stats


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


def test_compute_chm_stats_returns_expected_core_metrics(monkeypatch):
    settings = Settings(
        chm_stats_histogram_min_m=0.0,
        chm_stats_histogram_max_m=40.0,
        chm_stats_histogram_bins=64,
        chm_stats_default_thresholds_m=[5.0, 10.0, 20.0],
    )

    class DummyReader:
        def header(self):
            return {"tile_type": "PNG", "max_zoom": 10}

    monkeypatch.setattr("app.services.chm_stats_service._build_pmtiles_reader", lambda _url: DummyReader())
    def _mock_tiles(_minx, _miny, _maxx, _maxy, zooms, truncate=True):
        z = list(zooms)[0]
        if z == 10:
            return [mercantile.Tile(x=1, y=1, z=10)]
        return []

    monkeypatch.setattr("app.services.chm_stats_service.mercantile.tiles", _mock_tiles)
    monkeypatch.setattr("app.services.chm_stats_service._transform_geometry_to_crs", lambda geometry, _crs: geometry)
    monkeypatch.setattr("app.services.chm_stats_service._aoi_area_ha", lambda _geometry: 4.0)

    def _mock_tile(*_args, **_kwargs):
        return TileChmStats(
            total_pixels=4,
            valid_pixels=4,
            sum_height_m=49.0,
            sum_height_sq_m2=1081.0,
            min_height_m=1.0,
            max_height_m=30.0,
            valid_area_ha=4.0,
            total_area_ha=4.0,
            canopy_volume_proxy_m3=490000.0,
            histogram_counts=np.array([1, 1, 1, 1] + [0] * 60, dtype=np.int64),
            threshold_counts={5.0: 3, 10.0: 2, 20.0: 1},
        )

    monkeypatch.setattr("app.services.chm_stats_service._process_chm_tile", _mock_tile)

    result = compute_chm_stats(
        geojson_obj=_geojson_feature_collection(),
        settings=settings,
    )

    assert result["validPixelCount"] == 4
    assert result["coverageFraction"] == 1.0
    assert result["minCanopyHeightM"] == 1.0
    assert result["maxCanopyHeightM"] == 30.0
    assert result["meanCanopyHeightM"] == 12.25
    assert result["analyzedAreaHa"] == 4.0
    assert result["aoiAreaHa"] == 4.0
    assert result["totalCanopyVolumeProxyM3"] == 490000.0

    threshold_metrics = result["canopyCoverByThreshold"]
    assert threshold_metrics[0]["thresholdM"] == 5.0
    assert threshold_metrics[0]["coverRatio"] == 0.75
    assert threshold_metrics[1]["thresholdM"] == 10.0
    assert threshold_metrics[1]["coverRatio"] == 0.5
    assert threshold_metrics[2]["thresholdM"] == 20.0
    assert threshold_metrics[2]["coverRatio"] == 0.25

    range_metrics = result["canopyCoverByRange"]
    assert [entry["label"] for entry in range_metrics] == ["<5m", "5-10m", "10-20m", ">=20m"]
    assert [entry["coverRatio"] for entry in range_metrics] == [0.25, 0.25, 0.25, 0.25]


def test_compute_chm_stats_uses_custom_thresholds(monkeypatch):
    settings = Settings(chm_stats_default_thresholds_m=[5.0, 10.0, 20.0], chm_stats_histogram_bins=64)

    class DummyReader:
        def header(self):
            return {"tile_type": "PNG", "max_zoom": 10}

    monkeypatch.setattr("app.services.chm_stats_service._build_pmtiles_reader", lambda _url: DummyReader())
    def _mock_tiles(_minx, _miny, _maxx, _maxy, zooms, truncate=True):
        z = list(zooms)[0]
        if z == 10:
            return [mercantile.Tile(x=1, y=1, z=10)]
        return []

    monkeypatch.setattr("app.services.chm_stats_service.mercantile.tiles", _mock_tiles)
    monkeypatch.setattr("app.services.chm_stats_service._transform_geometry_to_crs", lambda geometry, _crs: geometry)
    monkeypatch.setattr("app.services.chm_stats_service._aoi_area_ha", lambda _geometry: 4.0)

    def _mock_tile(*_args, **_kwargs):
        return TileChmStats(
            total_pixels=4,
            valid_pixels=4,
            sum_height_m=40.0,
            sum_height_sq_m2=430.0,
            min_height_m=5.0,
            max_height_m=15.0,
            valid_area_ha=4.0,
            total_area_ha=4.0,
            canopy_volume_proxy_m3=400000.0,
            histogram_counts=np.array([0, 1, 2, 1] + [0] * 60, dtype=np.int64),
            threshold_counts={7.0: 3, 12.0: 1},
        )

    monkeypatch.setattr("app.services.chm_stats_service._process_chm_tile", _mock_tile)

    result = compute_chm_stats(
        geojson_obj=_geojson_feature_collection(),
        settings=settings,
        canopy_thresholds_m=[12.0, 7.0],
    )

    threshold_metrics = result["canopyCoverByThreshold"]
    assert [entry["thresholdM"] for entry in threshold_metrics] == [7.0, 12.0]

    range_metrics = result["canopyCoverByRange"]
    assert [entry["label"] for entry in range_metrics] == ["<7m", "7-12m", ">=12m"]
    assert [entry["coverRatio"] for entry in range_metrics] == [0.25, 0.5, 0.25]


def test_decode_rgb_height_values_red_only_uses_quantized_scale():
    rgb = np.array(
        [
            [[0, 0, 0], [1, 0, 0]],
            [[4, 0, 0], [34, 0, 0]],
        ],
        dtype=np.uint8,
    )

    heights = _decode_rgb_height_values(rgb)

    assert np.isclose(float(heights[0, 0]), 0.0)
    assert np.isclose(float(heights[0, 1]), 0.4)
    assert np.isclose(float(heights[1, 0]), 1.6)
    assert np.isclose(float(heights[1, 1]), 13.6)


def test_decode_rgb_height_values_packed_rg_remains_supported():
    rgb = np.array(
        [
            [[1, 44, 0], [3, 232, 0]],
        ],
        dtype=np.uint8,
    )

    heights = _decode_rgb_height_values(rgb)

    assert np.isclose(float(heights[0, 0]), 30.0)
    assert np.isclose(float(heights[0, 1]), 100.0)
