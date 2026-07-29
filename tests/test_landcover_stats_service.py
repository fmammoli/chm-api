from __future__ import annotations

import numpy as np
import pytest
from rasterio.transform import Affine
from shapely.geometry import Polygon

from app.config import Settings
from app.services.landcover_stats_service import (
    TileStats,
    YearCrop,
    _compute_landcover_change_stats_from_pmtiles,
    _extract_pmtiles_class_values,
    _forest_mask_from_pmtiles_image,
    _resolve_forest_classes,
    _valid_mask_from_pmtiles_image,
    compute_landcover_change_stats,
)


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


def test_compute_landcover_change_stats_basic_math(monkeypatch):
    settings = Settings()

    baseline = np.array(
        [
            [3, 3],
            [0, 9],
        ],
        dtype=np.int32,
    )
    comparison = np.array(
        [
            [0, 3],
            [9, 9],
        ],
        dtype=np.int32,
    )

    baseline_crop = YearCrop(
        year=1990,
        source_url="https://example.test/1990.tif",
        data=baseline,
        valid_mask=np.ones_like(baseline, dtype=bool),
        transform=Affine(30.0, 0.0, 0.0, 0.0, -30.0, 0.0),
        crs="EPSG:3857",
    )
    comparison_crop = YearCrop(
        year=2024,
        source_url="https://example.test/2024.tif",
        data=comparison,
        valid_mask=np.ones_like(comparison, dtype=bool),
        transform=Affine(30.0, 0.0, 0.0, 0.0, -30.0, 0.0),
        crs="EPSG:3857",
    )

    monkeypatch.setattr(
        "app.services.landcover_stats_service._resolve_year_url",
        lambda _settings, year: f"https://example.test/{year}.tif",
    )

    def _mock_crop(year: int, source_url: str, _geometry):
        return baseline_crop if year == 1990 else comparison_crop

    monkeypatch.setattr("app.services.landcover_stats_service._crop_landcover_year", _mock_crop)

    result = compute_landcover_change_stats(
        geojson_obj=_geojson_feature_collection(),
        baseline_year=1990,
        comparison_year=2024,
        settings=settings,
    )

    assert result["coverageFraction"] == 1.0
    assert result["validPixelCount"] == 4

    aoi_area_ha = float(result["aoiAreaHa"])
    assert aoi_area_ha > 0

    expected_quarter_area = round(aoi_area_ha / 4.0, 4)
    expected_half_area = round(aoi_area_ha / 2.0, 4)

    assert float(result["forestLossHa"]) == pytest.approx(expected_quarter_area, abs=1e-3)
    assert float(result["forestGainHa"]) == pytest.approx(0.0, abs=1e-6)
    assert float(result["forestLossPct"]) == pytest.approx(25.0, abs=1e-6)
    assert float(result["forestGainPct"]) == pytest.approx(0.0, abs=1e-6)
    assert float(result["baselineForestAreaHa"]) == pytest.approx(expected_half_area, abs=1e-3)
    assert float(result["comparisonForestAreaHa"]) == pytest.approx(expected_quarter_area, abs=1e-3)
    assert float(result["netForestChangeHa"]) == pytest.approx(-expected_quarter_area, abs=1e-3)


def test_resolve_forest_classes_uses_default_mapbiomas_forest_classes_when_unconfigured():
    settings = Settings(landcover_forest_classes=[], landcover_planted_forest_classes=[])

    assert _resolve_forest_classes(settings) == [3, 5, 76]


def test_pmtiles_metadata_includes_configured_forest_classes(monkeypatch):
    settings = Settings()
    geometry = Polygon([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])

    class DummyReader:
        def header(self):
            return {"tile_type": "PNG", "max_zoom": 12, "tile_compression": None}

    monkeypatch.setattr("app.services.landcover_stats_service._build_pmtiles_reader", lambda _url: DummyReader())
    monkeypatch.setattr("app.services.landcover_stats_service._transform_geometry_to_crs", lambda geometry, _crs: geometry)
    monkeypatch.setattr("app.services.landcover_stats_service._resolve_forest_rgb_colors", lambda _settings: {(0, 0, 0)})
    monkeypatch.setattr(
        "app.services.landcover_stats_service._process_pmtiles_tile",
        lambda *args, **kwargs: TileStats(
            total_pixels=4,
            valid_pixels=4,
            forest_loss_pixels=0,
            forest_gain_pixels=0,
            baseline_forest_pixels=0,
            comparison_forest_pixels=0,
            total_area_ha=4.0,
            valid_area_ha=4.0,
            forest_loss_ha=0.0,
            forest_gain_ha=0.0,
            baseline_forest_ha=0.0,
            comparison_forest_ha=0.0,
        ),
    )
    monkeypatch.setattr("app.services.landcover_stats_service._aoi_area_ha", lambda _geometry: 100.0)

    result = _compute_landcover_change_stats_from_pmtiles(
        geometry=geometry,
        baseline_year=1990,
        comparison_year=2024,
        baseline_url="https://example.test/1990.pmtiles",
        comparison_url="https://example.test/2024.pmtiles",
        settings=settings,
        progress_callback=None,
    )

    metadata = result["metadata"]
    assert metadata["forestClasses"] == "3,5,76"
    assert metadata["forestColors"]


def test_pmtiles_class_encoded_rgb_uses_class_channel_for_forest_mask_and_validity():
    image = np.array(
        [
            [[3, 127, 0], [0, 0, 0]],
            [[76, 127, 0], [24, 127, 0]],
        ],
        dtype=np.uint8,
    )

    class_values = _extract_pmtiles_class_values(image)
    assert class_values is not None
    assert class_values.tolist() == [[3, 0], [76, 24]]

    valid_mask = _valid_mask_from_pmtiles_image(image, class_values)
    assert valid_mask.tolist() == [[True, False], [True, True]]

    forest_mask = _forest_mask_from_pmtiles_image(
        image,
        class_values=class_values,
        forest_classes={3, 5, 76},
        forest_colors={(31, 141, 73)},
    )
    assert forest_mask.tolist() == [[True, False], [True, False]]


def test_pmtiles_class_encoded_r_only_uses_red_channel_for_forest_mask_and_validity():
    image = np.array(
        [
            [[3, 0, 0], [0, 0, 0]],
            [[76, 0, 0], [24, 0, 0]],
        ],
        dtype=np.uint8,
    )

    class_values = _extract_pmtiles_class_values(image)
    assert class_values is not None
    assert class_values.tolist() == [[3, 0], [76, 24]]

    valid_mask = _valid_mask_from_pmtiles_image(image, class_values)
    assert valid_mask.tolist() == [[True, False], [True, True]]

    forest_mask = _forest_mask_from_pmtiles_image(
        image,
        class_values=class_values,
        forest_classes={3, 5, 76},
        forest_colors={(31, 141, 73)},
    )
    assert forest_mask.tolist() == [[True, False], [True, False]]


def test_pmtiles_rgba_still_supports_color_based_forest_mask_when_not_class_encoded():
    image = np.array(
        [
            [[31, 141, 73, 255], [20, 20, 20, 255]],
            [[31, 141, 73, 0], [0, 0, 0, 0]],
        ],
        dtype=np.uint8,
    )

    class_values = _extract_pmtiles_class_values(image)
    assert class_values is None

    valid_mask = _valid_mask_from_pmtiles_image(image, class_values)
    assert valid_mask.tolist() == [[True, True], [False, False]]

    forest_mask = _forest_mask_from_pmtiles_image(
        image,
        class_values=class_values,
        forest_classes={3, 5, 76},
        forest_colors={(31, 141, 73)},
    )
    assert forest_mask.tolist() == [[True, False], [True, False]]
