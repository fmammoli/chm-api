from __future__ import annotations

from pathlib import Path
import tempfile
import time
import zipfile

import mercantile
import numpy as np
import pytest

from app.config import Settings
from app.models import ThreatMapJobCreateRequest
from app.services.chm_service import ServiceValidationError
from app.services.threat_map_service import (
    RenderOptions,
    ThreatMapResourceLimitError,
    YEARS,
    _build_zip_fallback,
    _draw_overlay_geometry,
    _fetch_single_tile_with_retry,
    _load_legend_entries,
    _load_legend_entries_from_mapbiomas_colors_path,
    _load_legend_entries_from_qgz_path,
    _resolve_output_format,
    _resolve_threat_map_year_url,
    _resolve_overlay_geojson_inputs,
    process_threat_map_job,
    _to_rgb,
    validate_threat_map_request_payload,
)


class _FlakyReader:
    def __init__(self, fail_count: int):
        self.fail_count = fail_count
        self.calls = 0

    def get(self, z: int, x: int, y: int):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise RuntimeError("transient fetch error")
        return b"tile-bytes"


def test_retry_backoff_with_eventual_success(monkeypatch):
    settings = Settings(
        threat_map_retry_max_attempts=4,
        threat_map_retry_base_delay_seconds=0.01,
        threat_map_retry_max_delay_seconds=0.05,
    )

    reader = _FlakyReader(fail_count=2)
    tile = mercantile.Tile(x=1, y=1, z=2)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda value: slept.append(value))

    _fetch_single_tile_with_retry(reader, tile, settings, is_cancelled=lambda: False)

    assert reader.calls == 3
    assert len(slept) == 2
    assert slept[1] >= slept[0]


def test_retry_exhaustion_raises():
    settings = Settings(threat_map_retry_max_attempts=2)
    reader = _FlakyReader(fail_count=5)
    tile = mercantile.Tile(x=1, y=1, z=2)

    with pytest.raises(Exception, match="Failed tile"):
        _fetch_single_tile_with_retry(reader, tile, settings, is_cancelled=lambda: False)


def test_zip_fallback_contains_first_and_last_year(monkeypatch, tmp_path: Path):
    settings = Settings(outputs_dir=tmp_path / "outputs")
    output_zip = tmp_path / "outputs" / "fallback.zip"

    monkeypatch.setattr(
        "app.services.threat_map_service._render_year_frame_from_tiles",
        lambda **kwargs: np.zeros((8, 8, 3), dtype=np.uint8),
    )

    result = _build_zip_fallback(
        settings=settings,
        geometry=type("Geom", (), {"bounds": (106.7, -6.4, 107.0, -6.1)})(),
        overlay_layers=[],
        options=RenderOptions(
            width=8,
            height=8,
            fps=0.67,
            frame_duration_seconds=1.5,
            ffmpeg_preset="ultrafast",
            crf=32,
        ),
        output_zip=output_zip,
        started_at=time.monotonic(),
        progress_callback=lambda update: None,
        is_cancelled=lambda: False,
    )

    assert result["status"] == "partial_success"
    assert result["yearsRendered"] == len(YEARS)

    with zipfile.ZipFile(output_zip, "r") as archive:
        names = set(archive.namelist())
    assert "frame_1990.png" in names
    assert "frame_2024.png" in names


def test_year_span_invariant():
    assert YEARS[0] == 1990
    assert YEARS[-1] == 2024


def test_resolve_output_format_forces_frames_archive_when_mp4_requested_and_server_mp4_generation_disabled():
    settings = Settings(threat_map_enable_server_mp4_generation=False)

    assert _resolve_output_format("mp4", settings) == "frames_tar_gz"


def test_resolve_output_format_forces_frames_archive_when_mp4_requested_and_server_mp4_generation_enabled():
    settings = Settings(threat_map_enable_server_mp4_generation=True)

    assert _resolve_output_format("mp4", settings) == "frames_tar_gz"


def test_to_rgb_preserves_baked_pmtiles_colors():
    image = np.array(
        [
            [[3, 127, 0], [5, 127, 0]],
            [[76, 127, 0], [9, 127, 0]],
        ],
        dtype=np.uint8,
    )

    mapped = _to_rgb(image)

    assert tuple(mapped[0, 0, :]) == (3, 127, 0)
    assert tuple(mapped[0, 1, :]) == (5, 127, 0)
    assert tuple(mapped[1, 0, :]) == (76, 127, 0)
    assert tuple(mapped[1, 1, :]) == (9, 127, 0)


def test_to_rgb_handles_read_only_input_array():
    image = np.array(
        [
            [[3, 127, 0], [5, 127, 0]],
        ],
        dtype=np.uint8,
    )
    image.setflags(write=False)

    mapped = _to_rgb(image)

    assert tuple(mapped[0, 0, :]) == (3, 127, 0)
    assert tuple(mapped[0, 1, :]) == (5, 127, 0)


def test_resolve_threat_map_year_url_uses_threat_map_bucket_defaults():
    settings = Settings()

    resolved = _resolve_threat_map_year_url(settings, 1990)

    assert (
        resolved
        == "https://www.bc-webgis.com/landcover-mapbiomas-pmtiles/1990_landcover.pmtiles"
    )


def test_threat_map_pmtiles_default_base_url_uses_custom_domain():
    settings = Settings()

    assert settings.threat_map_landcover_base_url == "https://www.bc-webgis.com/landcover-mapbiomas-pmtiles"


def test_load_legend_entries_from_mapbiomas_colors_path_reads_palette(tmp_path: Path):
    colors_path = tmp_path / "mapbiomas-colors.txt"
    colors_path.write_text("3 31 141 73 255 # Forest Formation\n5 4 56 29 255 # Mangrove\n", encoding="utf-8")

    entries = _load_legend_entries_from_mapbiomas_colors_path(str(colors_path))

    assert entries[0] == {"class_code": "3", "label": "Forest Formation / Formasi Hutan", "color": "#1f8d49"}
    assert entries[1] == {"class_code": "5", "label": "Mangrove / Mangrove", "color": "#04381d"}


def test_load_legend_entries_from_qgz_path_reads_palette(tmp_path: Path):
        qgs_xml = """
<qgis>
    <projectlayers>
        <maplayer>
            <rasterrenderer type="paletted">
                <colorPalette>
                    <paletteEntry value="3" color="#1f8d49" label="Forest" alpha="255"/>
                    <paletteEntry value="5" color="#04381d" label="Mangrove" alpha="255"/>
                </colorPalette>
            </rasterrenderer>
        </maplayer>
    </projectlayers>
</qgis>
""".strip()

        qgz_path = tmp_path / "legend.qgz"
        with zipfile.ZipFile(qgz_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("legend.qgs", qgs_xml)

        entries = _load_legend_entries_from_qgz_path(str(qgz_path))
        assert len(entries) == 2
        assert entries[0] == {"class_code": "3", "label": "Forest / Formasi Hutan", "color": "#1f8d49"}
        assert entries[1] == {"class_code": "5", "label": "Mangrove / Mangrove", "color": "#04381d"}


def test_load_legend_entries_falls_back_to_qgz_when_manifest_and_colors_missing(tmp_path: Path, monkeypatch):
        qgs_xml = """
<qgis>
    <projectlayers>
        <maplayer>
            <rasterrenderer type="paletted">
                <colorPalette>
                    <paletteEntry value="76" color="#2f7360" label="Peat Swamp Forest" alpha="255"/>
                </colorPalette>
            </rasterrenderer>
        </maplayer>
    </projectlayers>
</qgis>
""".strip()

        qgz_path = tmp_path / "mekar_raya.qgz"
        with zipfile.ZipFile(qgz_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("mekar_raya.qgs", qgs_xml)

        monkeypatch.chdir(tmp_path)
        settings = Settings(
            threat_map_legend_manifest_path=tmp_path / "missing_manifest.json",
            threat_map_legend_colors_path=tmp_path / "missing_colors.txt",
        )
        entries = _load_legend_entries(settings)

        assert len(entries) == 1
        assert entries[0]["class_code"] == "76"
        assert entries[0]["label"] == "Peat Swamp Forest / Hutan Rawa Gambut"
        assert entries[0]["color"] == "#2f7360"


def test_resolve_overlay_geojson_inputs_splits_embedded_overlay_features() -> None:
    payload = ThreatMapJobCreateRequest(
        geojson={
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"source": "threat-map-square"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[106.7, -6.4], [107.0, -6.4], [107.0, -6.1], [106.7, -6.1], [106.7, -6.4]]],
                    },
                },
                {
                    "type": "Feature",
                    "properties": {"source": "threat-map-overlay"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[106.78, -6.33], [106.92, -6.33], [106.92, -6.19], [106.78, -6.19], [106.78, -6.33]]],
                    },
                },
            ],
        },
        geojsonCrs="EPSG:4326",
    )

    primary, overlay, overlay_crs, overlay_point, overlay_point_name, overlay_point_crs, extracted = _resolve_overlay_geojson_inputs(payload)

    assert extracted is True
    assert overlay is not None
    assert overlay_crs == "EPSG:4326"
    assert overlay_point is None
    assert overlay_point_name is None
    assert overlay_point_crs == "EPSG:3857"
    assert len(primary["features"]) == 1
    assert len(overlay["features"]) == 1


def test_resolve_overlay_geojson_inputs_extracts_embedded_point_layer() -> None:
    payload = ThreatMapJobCreateRequest(
        geojson={
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"source": "threat-map-square"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[106.7, -6.4], [107.0, -6.4], [107.0, -6.1], [106.7, -6.1], [106.7, -6.4]]],
                    },
                },
                {
                    "type": "Feature",
                    "properties": {"source": "threat-map-point", "name": "Camp A"},
                    "geometry": {
                        "type": "Point",
                        "coordinates": [106.84, -6.26],
                    },
                },
            ],
        },
        geojsonCrs="EPSG:4326",
    )

    primary, overlay, overlay_crs, overlay_point, overlay_point_name, overlay_point_crs, extracted = _resolve_overlay_geojson_inputs(payload)

    assert extracted is True
    assert overlay is None
    assert overlay_crs == "EPSG:4326"
    assert overlay_point is not None
    assert overlay_point_name == "Camp A"
    assert overlay_point_crs == "EPSG:4326"
    assert len(primary["features"]) == 1


def test_validate_accepts_overlay_embedded_in_geojson() -> None:
    settings = Settings()
    payload = ThreatMapJobCreateRequest(
        geojson={
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"source": "threat-map-square"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[106.7, -6.4], [107.0, -6.4], [107.0, -6.1], [106.7, -6.1], [106.7, -6.4]]],
                    },
                },
                {
                    "type": "Feature",
                    "properties": {"source": "threat-map-overlay"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[106.78, -6.33], [106.92, -6.33], [106.92, -6.19], [106.78, -6.19], [106.78, -6.33]]],
                    },
                },
            ],
        },
        geojsonCrs="EPSG:4326",
    )

    validated = validate_threat_map_request_payload(payload, settings)
    assert "options" in validated


def test_frames_tar_gz_timeout_does_not_fallback_to_zip(monkeypatch, tmp_path: Path):
    settings = Settings(
        outputs_dir=tmp_path / "outputs",
        threat_map_temp_root=tmp_path / "tmp",
    )

    payload = ThreatMapJobCreateRequest(
        geojson={
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
        geojsonCrs="EPSG:4326",
        outputFormat="frames_tar_gz",
    )

    monkeypatch.setattr(
        "app.services.threat_map_service._build_frames_tar_gz_artifact",
        lambda **kwargs: (_ for _ in ()).throw(ThreatMapResourceLimitError("request_timeout", "timeout")),
    )

    def _zip_should_not_be_called(**kwargs):
        raise AssertionError("zip fallback should not run for frames_tar_gz pipeline")

    monkeypatch.setattr("app.services.threat_map_service._build_zip_fallback", _zip_should_not_be_called)

    with pytest.raises(ThreatMapResourceLimitError, match="timeout"):
        process_threat_map_job(
            settings=settings,
            job_id="job-test",
            payload=payload,
            progress_callback=lambda update: None,
            is_cancelled=lambda: False,
        )


def test_validate_accepts_overlay_geojson() -> None:
    settings = Settings()
    payload = ThreatMapJobCreateRequest(
        geojson={
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
        geojsonCrs="EPSG:4326",
        overlayGeojson={
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
        overlayGeojsonCrs="EPSG:4326",
    )

    validated = validate_threat_map_request_payload(payload, settings)
    assert "options" in validated


def test_validate_accepts_named_overlay_point() -> None:
    settings = Settings()
    payload = ThreatMapJobCreateRequest(
        geojson={
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
        geojsonCrs="EPSG:4326",
        overlayPointGeojson={
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [106.84, -6.26]},
            "properties": {},
        },
        overlayPointName="Sampling point",
        overlayPointCrs="EPSG:4326",
    )

    validated = validate_threat_map_request_payload(payload, settings)
    assert "options" in validated


def test_validate_accepts_styled_overlay_layers() -> None:
    settings = Settings()
    payload = ThreatMapJobCreateRequest(
        geojson={
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
        geojsonCrs="EPSG:4326",
        overlayLayers=[
            {
                "id": "wood-fiber",
                "label": "Wood Fiber Concession",
                "geojsonCrs": "EPSG:4326",
                "geojson": {
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
                "style": {
                    "strokeColor": "#f59e0b",
                    "fillColor": "#f59e0b",
                    "fillOpacity": 0.2,
                },
                "showInLegend": True,
                "legendOrder": 10,
            },
            {
                "id": "camp-a",
                "label": "Camp A",
                "geojsonCrs": "EPSG:4326",
                "geojson": {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [106.84, -6.26]},
                    "properties": {},
                },
                "style": {
                    "markerColor": "#ef4444",
                    "labelBgColor": "#ffffff",
                },
                "showInLegend": True,
                "legendOrder": 20,
            },
        ],
    )

    validated = validate_threat_map_request_payload(payload, settings)
    assert "options" in validated


def test_validate_rejects_payload_without_polygon_aoi() -> None:
    settings = Settings()
    payload = ThreatMapJobCreateRequest(
        geojson={
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"source": "threat-map-point", "name": "Only point"},
                    "geometry": {
                        "type": "Point",
                        "coordinates": [106.84, -6.26],
                    },
                }
            ],
        },
        geojsonCrs="EPSG:4326",
    )

    with pytest.raises(ServiceValidationError, match="No AOI polygon found in geojson"):
        validate_threat_map_request_payload(payload, settings)


def test_validate_accepts_overlay_layer_with_mixed_point_and_polygon() -> None:
    settings = Settings()
    payload = ThreatMapJobCreateRequest(
        geojson={
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
        geojsonCrs="EPSG:4326",
        overlayLayers=[
            {
                "id": "mixed-layer",
                "label": "Mixed layer",
                "geojsonCrs": "EPSG:4326",
                "geojson": {
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
                        },
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Point",
                                "coordinates": [106.84, -6.26],
                            },
                            "properties": {"name": "Camp A"},
                        },
                    ],
                },
                "showInLegend": True,
            }
        ],
    )

    validated = validate_threat_map_request_payload(payload, settings)
    assert "options" in validated


def test_draw_overlay_geometry_draws_visible_lines() -> None:
    frame = np.zeros((128, 128, 3), dtype=np.uint8)
    overlay = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [106.75, -6.35],
                            [106.95, -6.35],
                            [106.95, -6.15],
                            [106.75, -6.15],
                            [106.75, -6.35],
                        ]
                    ],
                },
                "properties": {},
            }
        ],
    }

    from app.services.chm_service import _extract_geometry

    overlay_geom = _extract_geometry(overlay)
    _draw_overlay_geometry(
        frame,
        overlay_geometry=overlay_geom,
        overlay_point_geometry=None,
        overlay_point_name=None,
        map_h=96,
        map_bounds_mercator=(-20000000.0, -20000000.0, 20000000.0, 20000000.0),
    )

    yellow_mask = (frame[:, :, 0] == 255) & (frame[:, :, 1] == 242) & (frame[:, :, 2] == 0)
    assert int(np.count_nonzero(yellow_mask)) > 0


def test_draw_overlay_geometry_draws_named_point_and_label(monkeypatch) -> None:
    frame = np.zeros((128, 128, 3), dtype=np.uint8)

    from shapely.geometry import Point

    import app.services.threat_map_service as threat_map_service_module

    # Keep this unit test deterministic by avoiding CRS math here; the service
    # already exercises the real transformation path in other validation tests.
    monkeypatch.setattr(threat_map_service_module, "_transform_geometry_to_crs", lambda geometry, _crs: geometry)

    _draw_overlay_geometry(
        frame,
        overlay_geometry=None,
        overlay_point_geometry=Point(0.0, 0.0),
        overlay_point_name="Sampling point",
        map_h=96,
        map_bounds_mercator=(-1.0, -1.0, 1.0, 1.0),
    )

    red_mask = (frame[:, :, 0] == 255) & (frame[:, :, 1] == 78) & (frame[:, :, 2] == 78)
    white_mask = (frame[:, :, 0] == 255) & (frame[:, :, 1] == 255) & (frame[:, :, 2] == 255)

    assert int(np.count_nonzero(red_mask)) > 0
    assert int(np.count_nonzero(white_mask)) > 0
