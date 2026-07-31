from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import gzip
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
from threading import Lock
import tempfile
import time
from typing import Callable

import mercantile
import numpy as np
from PIL import Image
import pmtiles.reader as pm_reader
from pmtiles.tile import Compression as PMTilesCompression, HeaderDict
import rasterio
import rasterio.features
import rasterio.mask
from rasterio.enums import Resampling
from rasterio.transform import Affine, from_bounds
from rasterio.warp import reproject
import requests
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from app.config import Settings
from app.services.chm_service import (
    ServiceValidationError,
    _extract_geometry,
    _transform_geometry_to_crs,
    _validate_geometry,
)

logger = logging.getLogger("chm_api")


@dataclass
class YearCrop:
    year: int
    source_url: str
    data: np.ndarray
    valid_mask: np.ndarray
    transform: Affine
    crs: str


@dataclass
class TileStats:
    total_pixels: int
    valid_pixels: int
    forest_loss_pixels: int
    forest_gain_pixels: int
    baseline_forest_pixels: int
    comparison_forest_pixels: int
    total_area_ha: float
    valid_area_ha: float
    forest_loss_ha: float
    forest_gain_ha: float
    baseline_forest_ha: float
    comparison_forest_ha: float


def _empty_tile_stats() -> TileStats:
    return TileStats(
        total_pixels=0,
        valid_pixels=0,
        forest_loss_pixels=0,
        forest_gain_pixels=0,
        baseline_forest_pixels=0,
        comparison_forest_pixels=0,
        total_area_ha=0.0,
        valid_area_ha=0.0,
        forest_loss_ha=0.0,
        forest_gain_ha=0.0,
        baseline_forest_ha=0.0,
        comparison_forest_ha=0.0,
    )


def validate_landcover_request_payload(geojson_obj: dict, settings: Settings) -> None:
    try:
        payload_len = len(json.dumps(geojson_obj).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ServiceValidationError("Invalid JSON payload") from exc

    if payload_len > settings.max_geojson_bytes:
        raise ServiceValidationError("GeoJSON payload too large")

    if not isinstance(geojson_obj, dict):
        raise ServiceValidationError("geojson must be an object")

    geom = _extract_geometry(geojson_obj)
    _validate_geometry(geom, settings)
    logger.info(
        "landcover_payload_validated payload_bytes=%s geom_type=%s bounds=%s",
        payload_len,
        geom.geom_type,
        tuple(round(v, 6) for v in geom.bounds),
    )


def compute_landcover_change_stats(
    *,
    geojson_obj: dict,
    baseline_year: int,
    comparison_year: int,
    settings: Settings,
    progress_callback: Callable[[int, str | None], None] | None = None,
) -> dict[str, float | int | dict[str, str | int | float]]:
    pipeline_started_at = time.perf_counter()
    if baseline_year == comparison_year:
        raise ServiceValidationError("baselineYear and comparisonYear must be different")

    logger.info(
        "landcover_pipeline step=1_validate_input status=start baseline_year=%s comparison_year=%s",
        baseline_year,
        comparison_year,
    )
    geometry = _extract_geometry(geojson_obj)
    _validate_geometry(geometry, settings)
    logger.info(
        "landcover_stats_start baseline_year=%s comparison_year=%s geom_type=%s bounds=%s",
        baseline_year,
        comparison_year,
        geometry.geom_type,
        tuple(round(v, 6) for v in geometry.bounds),
    )
    logger.info("landcover_pipeline step=1_validate_input status=done")

    logger.info("landcover_pipeline step=2_resolve_sources status=start")
    baseline_url = _resolve_year_url(settings, baseline_year)
    comparison_url = _resolve_year_url(settings, comparison_year)
    logger.info(
        "landcover_sources_resolved baseline_url=%s comparison_url=%s",
        baseline_url,
        comparison_url,
    )
    logger.info("landcover_pipeline step=2_resolve_sources status=done")

    logger.info("landcover_pipeline step=3_select_backend status=start")
    if baseline_url.endswith(".pmtiles") and comparison_url.endswith(".pmtiles"):
        logger.info("landcover_backend_selected backend=pmtiles")
        logger.info("landcover_pipeline step=3_select_backend status=done backend=pmtiles")
        result = _compute_landcover_change_stats_from_pmtiles(
            geometry=geometry,
            baseline_year=baseline_year,
            comparison_year=comparison_year,
            baseline_url=baseline_url,
            comparison_url=comparison_url,
            settings=settings,
            progress_callback=progress_callback,
        )
        logger.info(
            "landcover_pipeline status=done backend=pmtiles duration_ms=%s",
            int((time.perf_counter() - pipeline_started_at) * 1000),
        )
        return result

    logger.info("landcover_backend_selected backend=raster")
    logger.info("landcover_pipeline step=3_select_backend status=done backend=raster")
    result = _compute_landcover_change_stats_from_rasters(
        geometry=geometry,
        baseline_year=baseline_year,
        comparison_year=comparison_year,
        baseline_url=baseline_url,
        comparison_url=comparison_url,
        settings=settings,
        progress_callback=progress_callback,
    )
    logger.info(
        "landcover_pipeline status=done backend=raster duration_ms=%s",
        int((time.perf_counter() - pipeline_started_at) * 1000),
    )
    return result


def _compute_landcover_change_stats_from_rasters(
    *,
    geometry: BaseGeometry,
    baseline_year: int,
    comparison_year: int,
    baseline_url: str,
    comparison_url: str,
    settings: Settings,
    progress_callback: Callable[[int, str | None], None] | None,
) -> dict[str, float | int | dict[str, str | int | float]]:
    stage_started_at = time.perf_counter()
    logger.info(
        "landcover_raster_pipeline_start baseline_year=%s comparison_year=%s",
        baseline_year,
        comparison_year,
    )
    logger.info("landcover_raster step=1_read_crop status=start")
    if progress_callback is not None:
        progress_callback(25, "Reading baseline and comparison rasters")

    with ThreadPoolExecutor(max_workers=2) as executor:
        baseline_future = executor.submit(_crop_landcover_year, baseline_year, baseline_url, geometry)
        comparison_future = executor.submit(_crop_landcover_year, comparison_year, comparison_url, geometry)
        baseline_crop = baseline_future.result()
        comparison_crop = comparison_future.result()
    logger.info("landcover_raster step=1_read_crop status=done")

    logger.info("landcover_raster step=2_align status=start")
    if progress_callback is not None:
        progress_callback(55, "Aligning cropped rasters")

    comparison_aligned = _align_to_reference(reference=baseline_crop, target=comparison_crop)
    logger.info("landcover_raster step=2_align status=done")

    logger.info("landcover_raster step=3_compute_metrics status=start")
    if progress_callback is not None:
        progress_callback(75, "Computing forest loss and gain")

    forest_classes = _resolve_forest_classes(settings)
    if not forest_classes:
        raise ServiceValidationError("Forest class configuration is empty")
    logger.info(
        "landcover_raster_classes_resolved class_count=%s classes=%s",
        len(forest_classes),
        forest_classes,
    )

    valid_mask = baseline_crop.valid_mask & comparison_aligned.valid_mask
    valid_pixel_count = int(valid_mask.sum())
    total_pixel_count = int(valid_mask.size)
    if valid_pixel_count == 0:
        raise ServiceValidationError("No overlapping valid landcover pixels inside AOI")

    baseline_forest = np.isin(baseline_crop.data, list(forest_classes)) & valid_mask
    comparison_forest = np.isin(comparison_aligned.data, list(forest_classes)) & valid_mask

    forest_loss_pixels = int((baseline_forest & ~comparison_forest).sum())
    forest_gain_pixels = int((~baseline_forest & comparison_forest).sum())
    baseline_forest_pixels = int(baseline_forest.sum())
    comparison_forest_pixels = int(comparison_forest.sum())

    coverage_fraction = valid_pixel_count / total_pixel_count
    aoi_area_ha = _aoi_area_ha(geometry)
    analyzed_area_ha = aoi_area_ha * coverage_fraction

    forest_loss_ha = analyzed_area_ha * (forest_loss_pixels / valid_pixel_count)
    forest_gain_ha = analyzed_area_ha * (forest_gain_pixels / valid_pixel_count)
    baseline_forest_area_ha = analyzed_area_ha * (baseline_forest_pixels / valid_pixel_count)
    comparison_forest_area_ha = analyzed_area_ha * (comparison_forest_pixels / valid_pixel_count)
    forest_loss_pct = (forest_loss_ha / analyzed_area_ha * 100.0) if analyzed_area_ha > 0 else 0.0
    forest_gain_pct = (forest_gain_ha / analyzed_area_ha * 100.0) if analyzed_area_ha > 0 else 0.0

    logger.info(
        "landcover_raster_pipeline_done valid_pixels=%s total_pixels=%s coverage=%.6f loss_ha=%.4f gain_ha=%.4f net_ha=%.4f",
        valid_pixel_count,
        total_pixel_count,
        coverage_fraction,
        forest_loss_ha,
        forest_gain_ha,
        comparison_forest_area_ha - baseline_forest_area_ha,
    )
    logger.info(
        "landcover_raster step=3_compute_metrics status=done duration_ms=%s",
        int((time.perf_counter() - stage_started_at) * 1000),
    )

    return {
        "baselineYear": baseline_year,
        "comparisonYear": comparison_year,
        "forestLossHa": round(forest_loss_ha, 4),
        "forestGainHa": round(forest_gain_ha, 4),
        "forestLossPct": round(forest_loss_pct, 4),
        "forestGainPct": round(forest_gain_pct, 4),
        "netForestChangeHa": round(comparison_forest_area_ha - baseline_forest_area_ha, 4),
        "baselineForestAreaHa": round(baseline_forest_area_ha, 4),
        "comparisonForestAreaHa": round(comparison_forest_area_ha, 4),
        "analyzedAreaHa": round(analyzed_area_ha, 4),
        "aoiAreaHa": round(aoi_area_ha, 4),
        "coverageFraction": round(coverage_fraction, 6),
        "validPixelCount": valid_pixel_count,
        "metadata": {
            "baselineUrl": baseline_url,
            "comparisonUrl": comparison_url,
            "sourceFormat": "raster",
            "baselineCrs": baseline_crop.crs,
            "comparisonCrs": comparison_aligned.crs,
            "forestClasses": ",".join(str(value) for value in forest_classes),
        },
    }


def _compute_landcover_change_stats_from_pmtiles(
    *,
    geometry: BaseGeometry,
    baseline_year: int,
    comparison_year: int,
    baseline_url: str,
    comparison_url: str,
    settings: Settings,
    progress_callback: Callable[[int, str | None], None] | None,
) -> dict[str, float | int | dict[str, str | int | float]]:
    stage_started_at = time.perf_counter()
    logger.info(
        "landcover_pmtiles_pipeline_start baseline_year=%s comparison_year=%s",
        baseline_year,
        comparison_year,
    )
    logger.info("landcover_pmtiles step=1_open_sources status=start")
    if progress_callback is not None:
        progress_callback(20, "Opening PMTiles sources")

    baseline_reader = _build_pmtiles_reader(baseline_url)
    comparison_reader = _build_pmtiles_reader(comparison_url)
    baseline_header = baseline_reader.header()
    comparison_header = comparison_reader.header()

    baseline_tile_type = str(getattr(baseline_header["tile_type"], "name", baseline_header["tile_type"]))
    comparison_tile_type = str(getattr(comparison_header["tile_type"], "name", comparison_header["tile_type"]))
    logger.info(
        "landcover_pmtiles_headers baseline_tile_type=%s comparison_tile_type=%s baseline_max_zoom=%s comparison_max_zoom=%s",
        baseline_tile_type,
        comparison_tile_type,
        baseline_header.get("max_zoom"),
        comparison_header.get("max_zoom"),
    )
    logger.info("landcover_pmtiles step=1_open_sources status=done")
    if baseline_tile_type != "PNG" or comparison_tile_type != "PNG":
        raise ServiceValidationError(
            f"Unsupported PMTiles tile type for landcover stats. baseline={baseline_tile_type}, comparison={comparison_tile_type}."
        )

    geometry_3857 = _transform_geometry_to_crs(geometry, "EPSG:3857")
    minx, miny, maxx, maxy = geometry.bounds
    if minx == maxx or miny == maxy:
        raise ServiceValidationError("AOI has zero area")

    max_zoom = int(min(baseline_header["max_zoom"], comparison_header["max_zoom"]))
    target_zoom = min(settings.landcover_pmtiles_zoom, max_zoom)
    tiles = list(mercantile.tiles(minx, miny, maxx, maxy, [target_zoom], truncate=True))
    if not tiles:
        raise ServiceValidationError("No PMTiles tiles intersect the AOI")
    logger.info(
        "landcover_pmtiles_tiles_selected target_zoom=%s tile_count=%s",
        target_zoom,
        len(tiles),
    )

    logger.info("landcover_pmtiles step=2_resolve_forest_legend status=start")
    forest_classes = _resolve_forest_classes(settings)
    forest_colors = _resolve_forest_rgb_colors(settings)
    logger.info(
        "landcover_pmtiles_forest_classes class_count=%s classes=%s",
        len(forest_classes),
        forest_classes,
    )
    logger.info(
        "landcover_pmtiles_forest_colors color_count=%s colors=%s",
        len(forest_colors),
        [f"{r:02x}{g:02x}{b:02x}" for r, g, b in sorted(forest_colors)],
    )
    logger.info("landcover_pmtiles step=2_resolve_forest_legend status=done")

    logger.info("landcover_pmtiles step=3_process_tiles status=start")
    if progress_callback is not None:
        progress_callback(40, f"Reading and classifying {len(tiles)} PMTiles tiles")

    worker_count = max(1, min(settings.download_workers, len(tiles), 8))
    forest_class_set = set(forest_classes)
    stats = _empty_tile_stats()
    processed = 0

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(
                _process_pmtiles_tile,
                tile,
                geometry_3857,
                baseline_reader,
                baseline_header,
                comparison_reader,
                comparison_header,
                forest_class_set,
                forest_colors,
            )
            for tile in tiles
        ]

        for future in futures:
            tile_stats = future.result()
            processed += 1
            stats.total_pixels += tile_stats.total_pixels
            stats.valid_pixels += tile_stats.valid_pixels
            stats.forest_loss_pixels += tile_stats.forest_loss_pixels
            stats.forest_gain_pixels += tile_stats.forest_gain_pixels
            stats.baseline_forest_pixels += tile_stats.baseline_forest_pixels
            stats.comparison_forest_pixels += tile_stats.comparison_forest_pixels
            stats.total_area_ha += tile_stats.total_area_ha
            stats.valid_area_ha += tile_stats.valid_area_ha
            stats.forest_loss_ha += tile_stats.forest_loss_ha
            stats.forest_gain_ha += tile_stats.forest_gain_ha
            stats.baseline_forest_ha += tile_stats.baseline_forest_ha
            stats.comparison_forest_ha += tile_stats.comparison_forest_ha

            if progress_callback is not None:
                progress = 40 + int((processed / len(tiles)) * 45)
                progress_callback(progress, f"Processed {processed}/{len(tiles)} tiles")
    logger.info("landcover_pmtiles step=3_process_tiles status=done processed_tiles=%s", len(tiles))

    if stats.total_pixels == 0:
        raise ServiceValidationError("No PMTiles coverage intersects the AOI")
    if stats.valid_pixels == 0:
        raise ServiceValidationError("No overlapping valid PMTiles pixels found inside AOI")

    if progress_callback is not None:
        progress_callback(90, "Finalizing landcover statistics")

    logger.info("landcover_pmtiles step=4_finalize_metrics status=start")

    aoi_area_ha = _aoi_area_ha(geometry)
    coverage_fraction = stats.valid_pixels / stats.total_pixels
    forest_loss_pct = (stats.forest_loss_ha / stats.valid_area_ha * 100.0) if stats.valid_area_ha > 0 else 0.0
    forest_gain_pct = (stats.forest_gain_ha / stats.valid_area_ha * 100.0) if stats.valid_area_ha > 0 else 0.0
    logger.info(
        "landcover_pmtiles_pipeline_done valid_pixels=%s total_pixels=%s coverage=%.6f loss_ha=%.4f gain_ha=%.4f net_ha=%.4f",
        stats.valid_pixels,
        stats.total_pixels,
        coverage_fraction,
        stats.forest_loss_ha,
        stats.forest_gain_ha,
        stats.comparison_forest_ha - stats.baseline_forest_ha,
    )
    logger.info(
        "landcover_pmtiles step=4_finalize_metrics status=done duration_ms=%s",
        int((time.perf_counter() - stage_started_at) * 1000),
    )

    return {
        "baselineYear": baseline_year,
        "comparisonYear": comparison_year,
        "forestLossHa": round(stats.forest_loss_ha, 4),
        "forestGainHa": round(stats.forest_gain_ha, 4),
        "forestLossPct": round(forest_loss_pct, 4),
        "forestGainPct": round(forest_gain_pct, 4),
        "netForestChangeHa": round(stats.comparison_forest_ha - stats.baseline_forest_ha, 4),
        "baselineForestAreaHa": round(stats.baseline_forest_ha, 4),
        "comparisonForestAreaHa": round(stats.comparison_forest_ha, 4),
        "analyzedAreaHa": round(stats.valid_area_ha, 4),
        "aoiAreaHa": round(aoi_area_ha, 4),
        "coverageFraction": round(coverage_fraction, 6),
        "validPixelCount": stats.valid_pixels,
        "metadata": {
            "baselineUrl": baseline_url,
            "comparisonUrl": comparison_url,
            "sourceFormat": "pmtiles_png",
            "zoom": target_zoom,
            "forestClasses": ",".join(str(value) for value in forest_classes),
            "forestColors": ",".join(f"{r:02x}{g:02x}{b:02x}" for r, g, b in sorted(forest_colors)),
        },
    }


def _resolve_year_url(settings: Settings, year: int) -> str:
    if year == 1990 and settings.landcover_year_1990_url:
        logger.info("landcover_year_url_override year=%s url=%s", year, settings.landcover_year_1990_url)
        return settings.landcover_year_1990_url
    if year == 2024 and settings.landcover_year_2024_url:
        logger.info("landcover_year_url_override year=%s url=%s", year, settings.landcover_year_2024_url)
        return settings.landcover_year_2024_url

    resolved = settings.landcover_url_template.format(base_url=settings.landcover_base_url.rstrip("/"), year=year)
    logger.info("landcover_year_url_template year=%s url=%s", year, resolved)
    return resolved


def _build_pmtiles_reader(url: str) -> pm_reader.Reader:
    logger.info("landcover_pmtiles_reader_init url=%s", url)
    session = requests.Session()
    lock = Lock()
    cache: dict[tuple[int, int], bytes] = {}
    cache_root = _resolve_pmtiles_range_cache_root()
    url_cache_dir = _pmtiles_range_cache_dir(cache_root, url)

    def get_bytes(offset: int, length: int) -> bytes:
        key = (offset, length)
        with lock:
            cached = cache.get(key)
        if cached is not None:
            return cached

        if url_cache_dir is not None:
            cached_path = url_cache_dir / f"{offset}_{length}.bin"
            disk_payload = _read_pmtiles_range_cache(cached_path, length)
            if disk_payload is not None:
                with lock:
                    cache[key] = disk_payload
                return disk_payload

        headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
        response = session.get(url, headers=headers, timeout=60)
        response.raise_for_status()
        payload = response.content
        if len(payload) != length:
            raise ServiceValidationError(
                f"PMTiles range read returned unexpected length. url={url}, requested={length}, received={len(payload)}"
            )

        with lock:
            cache[key] = payload
        if url_cache_dir is not None:
            _write_pmtiles_range_cache(url_cache_dir / f"{offset}_{length}.bin", payload)
        return payload

    return pm_reader.Reader(get_bytes)


def _resolve_pmtiles_range_cache_root() -> Path | None:
    raw_dir = os.getenv("PMTILES_RANGE_CACHE_DIR", "").strip()
    if not raw_dir:
        return None

    root = Path(raw_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("pmtiles_range_cache_unavailable path=%s", root)
        return None
    return root


def _pmtiles_range_cache_dir(cache_root: Path | None, url: str) -> Path | None:
    if cache_root is None:
        return None
    url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    path = cache_root / "ranges" / url_hash
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("pmtiles_range_cache_unavailable path=%s", path)
        return None
    return path


def _read_pmtiles_range_cache(path: Path, expected_length: int) -> bytes | None:
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    if len(payload) != expected_length:
        return None
    return payload


def _write_pmtiles_range_cache(path: Path, payload: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(delete=False, dir=str(path.parent)) as tmp_file:
            tmp_file.write(payload)
            tmp_path = Path(tmp_file.name)
        tmp_path.replace(path)
    except OSError:
        # Best-effort cache write; request path should continue without failing.
        return


def _process_pmtiles_tile(
    tile: mercantile.Tile,
    geometry_3857: BaseGeometry,
    baseline_reader: pm_reader.Reader,
    baseline_header: HeaderDict,
    comparison_reader: pm_reader.Reader,
    comparison_header: HeaderDict,
    forest_classes: set[int],
    forest_colors: set[tuple[int, int, int]],
) -> TileStats:
    baseline_tile_bytes = baseline_reader.get(tile.z, tile.x, tile.y)
    comparison_tile_bytes = comparison_reader.get(tile.z, tile.x, tile.y)

    baseline_img = _decode_pmtiles_png_tile(baseline_tile_bytes, baseline_header["tile_compression"]) if baseline_tile_bytes else None
    comparison_img = (
        _decode_pmtiles_png_tile(comparison_tile_bytes, comparison_header["tile_compression"]) if comparison_tile_bytes else None
    )

    width = 256
    height = 256
    if baseline_img is not None:
        height, width = baseline_img.shape[0], baseline_img.shape[1]
    elif comparison_img is not None:
        height, width = comparison_img.shape[0], comparison_img.shape[1]

    if baseline_img is None or comparison_img is None:
        logger.debug(
            "landcover_pmtiles_tile_missing_data z=%s x=%s y=%s baseline_present=%s comparison_present=%s",
            tile.z,
            tile.x,
            tile.y,
            baseline_img is not None,
            comparison_img is not None,
        )
        return _stats_for_missing_tile(tile, geometry_3857, width, height)

    if baseline_img.shape[:2] != comparison_img.shape[:2]:
        raise ServiceValidationError(
            f"Mismatched PMTiles tile dimensions for z/x/y={tile.z}/{tile.x}/{tile.y}"
        )

    mask = _rasterize_tile_aoi_mask(tile, geometry_3857, width, height)
    total_pixels = int(mask.sum())
    if total_pixels == 0:
        return _empty_tile_stats()

    baseline_class_values = _extract_pmtiles_class_values(baseline_img)
    comparison_class_values = _extract_pmtiles_class_values(comparison_img)

    baseline_valid = _valid_mask_from_pmtiles_image(baseline_img, baseline_class_values)
    comparison_valid = _valid_mask_from_pmtiles_image(comparison_img, comparison_class_values)
    valid_mask = mask & baseline_valid & comparison_valid
    valid_pixels = int(valid_mask.sum())

    baseline_forest = (
        _forest_mask_from_pmtiles_image(
            baseline_img,
            class_values=baseline_class_values,
            forest_classes=forest_classes,
            forest_colors=forest_colors,
        )
        & valid_mask
    )
    comparison_forest = (
        _forest_mask_from_pmtiles_image(
            comparison_img,
            class_values=comparison_class_values,
            forest_classes=forest_classes,
            forest_colors=forest_colors,
        )
        & valid_mask
    )

    forest_loss_pixels = int((baseline_forest & ~comparison_forest).sum())
    forest_gain_pixels = int((~baseline_forest & comparison_forest).sum())
    baseline_forest_pixels = int(baseline_forest.sum())
    comparison_forest_pixels = int(comparison_forest.sum())

    pixel_area_ha = _tile_pixel_area_ha(tile, width, height)
    return TileStats(
        total_pixels=total_pixels,
        valid_pixels=valid_pixels,
        forest_loss_pixels=forest_loss_pixels,
        forest_gain_pixels=forest_gain_pixels,
        baseline_forest_pixels=baseline_forest_pixels,
        comparison_forest_pixels=comparison_forest_pixels,
        total_area_ha=total_pixels * pixel_area_ha,
        valid_area_ha=valid_pixels * pixel_area_ha,
        forest_loss_ha=forest_loss_pixels * pixel_area_ha,
        forest_gain_ha=forest_gain_pixels * pixel_area_ha,
        baseline_forest_ha=baseline_forest_pixels * pixel_area_ha,
        comparison_forest_ha=comparison_forest_pixels * pixel_area_ha,
    )


def _stats_for_missing_tile(tile: mercantile.Tile, geometry_3857: BaseGeometry, width: int, height: int) -> TileStats:
    mask = _rasterize_tile_aoi_mask(tile, geometry_3857, width, height)
    total_pixels = int(mask.sum())
    if total_pixels == 0:
        return _empty_tile_stats()
    pixel_area_ha = _tile_pixel_area_ha(tile, width, height)
    return TileStats(
        total_pixels=total_pixels,
        valid_pixels=0,
        forest_loss_pixels=0,
        forest_gain_pixels=0,
        baseline_forest_pixels=0,
        comparison_forest_pixels=0,
        total_area_ha=total_pixels * pixel_area_ha,
        valid_area_ha=0.0,
        forest_loss_ha=0.0,
        forest_gain_ha=0.0,
        baseline_forest_ha=0.0,
        comparison_forest_ha=0.0,
    )


def _decode_pmtiles_png_tile(tile_bytes: bytes, compression: PMTilesCompression) -> np.ndarray:
    if compression == PMTilesCompression.GZIP:
        tile_bytes = gzip.decompress(tile_bytes)
    elif compression not in {PMTilesCompression.NONE, PMTilesCompression.GZIP}:
        raise ServiceValidationError(f"Unsupported PMTiles tile compression: {compression}")

    image = Image.open(BytesIO(tile_bytes))
    if image.mode not in {"L", "LA", "RGB", "RGBA"}:
        image = image.convert("RGBA")
    return np.asarray(image, dtype=np.uint8)


def _extract_pmtiles_class_values(image: np.ndarray) -> np.ndarray | None:
    if image.ndim == 2:
        return image.astype(np.int32, copy=False)

    if image.ndim != 3:
        return None

    channel_count = image.shape[2]
    if channel_count == 2:
        # LA-style source where first channel stores class values.
        return image[:, :, 0].astype(np.int32, copy=False)

    if channel_count >= 3:
        red = image[:, :, 0].astype(np.int32, copy=False)
        green = image[:, :, 1]
        blue = image[:, :, 2]
        nonzero_class = red > 0

        # MapBiomas PMTiles generated by geotiff2pmtiles often encode class in R,
        # with sentinel G/B channels (127/0). In this case, rely on class IDs directly.
        if np.any(nonzero_class) and np.all(green[nonzero_class] == 127) and np.all(blue[nonzero_class] == 0):
            return red

        # Some class-value PMTiles use R-only encoding with zeroed G/B channels.
        if np.any(nonzero_class) and np.all(green[nonzero_class] == 0) and np.all(blue[nonzero_class] == 0):
            return red

    return None


def _valid_mask_from_pmtiles_image(image: np.ndarray, class_values: np.ndarray | None) -> np.ndarray:
    if image.ndim == 3 and image.shape[2] >= 4:
        return image[:, :, 3] > 0

    if class_values is not None:
        return class_values > 0

    if image.ndim == 2:
        return image > 0

    if image.ndim == 3 and image.shape[2] >= 3:
        return np.any(image[:, :, :3] != 0, axis=2)

    return np.zeros(image.shape[:2], dtype=bool)


def _forest_mask_from_pmtiles_image(
    image: np.ndarray,
    *,
    class_values: np.ndarray | None,
    forest_classes: set[int],
    forest_colors: set[tuple[int, int, int]],
) -> np.ndarray:
    if class_values is not None:
        if not forest_classes:
            return np.zeros(image.shape[:2], dtype=bool)
        return np.isin(class_values, list(forest_classes))

    if image.ndim == 3 and image.shape[2] >= 3:
        return _forest_mask_from_rgba(image, forest_colors)

    return np.zeros(image.shape[:2], dtype=bool)


def _rasterize_tile_aoi_mask(tile: mercantile.Tile, geometry_3857: BaseGeometry, width: int, height: int) -> np.ndarray:
    bounds = mercantile.xy_bounds(tile)
    transform = from_bounds(bounds.left, bounds.bottom, bounds.right, bounds.top, width, height)
    rasterized = rasterio.features.rasterize(
        [mapping(geometry_3857)],
        out_shape=(height, width),
        transform=transform,
        fill=0,
        all_touched=False,
        dtype=np.uint8,
    )
    if rasterized is None:
        return np.zeros((height, width), dtype=bool)
    return rasterized.astype(bool)


def _forest_mask_from_rgba(rgba: np.ndarray, forest_colors: set[tuple[int, int, int]]) -> np.ndarray:
    rgb = rgba[:, :, :3]
    mask = np.zeros((rgb.shape[0], rgb.shape[1]), dtype=bool)
    for color in forest_colors:
        mask |= np.all(rgb == np.array(color, dtype=np.uint8), axis=2)
    return mask


def _tile_pixel_area_ha(tile: mercantile.Tile, width: int, height: int) -> float:
    bounds = mercantile.xy_bounds(tile)
    pixel_width = (bounds.right - bounds.left) / width
    pixel_height = (bounds.top - bounds.bottom) / height
    return abs(pixel_width * pixel_height) / 10_000.0


def _resolve_forest_classes(settings: Settings) -> list[int]:
    configured_classes = [value for value in settings.landcover_forest_classes if value is not None]
    planted_classes = [value for value in settings.landcover_planted_forest_classes if value is not None]

    if configured_classes or planted_classes:
        forest_classes = sorted(set(configured_classes) | set(planted_classes))
        return forest_classes

    default_classes = [3, 5, 76]
    logger.info("landcover_forest_classes_defaulted classes=%s", default_classes)
    return default_classes


def _resolve_forest_rgb_colors(settings: Settings) -> set[tuple[int, int, int]]:
    colors: set[tuple[int, int, int]] = set()
    for raw in settings.landcover_forest_colors:
        value = raw.strip().lower().lstrip("#")
        if len(value) != 6:
            continue
        try:
            colors.add((int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)))
        except ValueError:
            continue
    if not colors:
        raise ServiceValidationError("LANDCOVER_FOREST_COLORS configuration is empty or invalid")
    logger.info(
        "landcover_forest_colors_resolved color_count=%s colors=%s",
        len(colors),
        [f"{r:02x}{g:02x}{b:02x}" for r, g, b in sorted(colors)],
    )
    return colors


def _crop_landcover_year(year: int, source_url: str, geometry: BaseGeometry) -> YearCrop:
    try:
        logger.info("landcover_raster_crop_start year=%s source=%s", year, source_url)
        with rasterio.Env(AWS_NO_SIGN_REQUEST="YES"):
            with rasterio.open(source_url) as src:
                if src.crs is None:
                    raise ServiceValidationError(f"Landcover source for year {year} has no CRS")

                geom_src = _transform_geometry_to_crs(geometry, src.crs)
                masked_data, transform = rasterio.mask.mask(
                    src,
                    [mapping(geom_src)],
                    crop=True,
                    indexes=1,
                    filled=False,
                )

                masked_array = np.ma.asarray(masked_data)
                data = np.asarray(masked_array.data, dtype=np.int32)
                valid_mask = ~np.ma.getmaskarray(masked_array)
                if src.nodata is not None:
                    valid_mask = valid_mask & (data != int(src.nodata))

                if data.size == 0 or valid_mask.sum() == 0:
                    raise ServiceValidationError(f"No valid landcover pixels found for year {year} in the AOI")

                logger.info(
                    "landcover_raster_crop_done year=%s crs=%s shape=%s valid_pixels=%s",
                    year,
                    str(src.crs),
                    tuple(data.shape),
                    int(valid_mask.sum()),
                )

                return YearCrop(
                    year=year,
                    source_url=source_url,
                    data=data,
                    valid_mask=valid_mask,
                    transform=transform,
                    crs=str(src.crs),
                )
    except ServiceValidationError:
        raise
    except Exception as exc:
        logger.exception("landcover_crop_failed year=%s source=%s", year, source_url)
        raise ServiceValidationError(f"Failed to read landcover source for year {year}") from exc


def _align_to_reference(reference: YearCrop, target: YearCrop) -> YearCrop:
    if (
        reference.crs == target.crs
        and reference.transform == target.transform
        and reference.data.shape == target.data.shape
    ):
        logger.info("landcover_raster_alignment_not_needed reference_year=%s target_year=%s", reference.year, target.year)
        return target

    logger.info(
        "landcover_raster_alignment_reproject reference_year=%s target_year=%s reference_shape=%s target_shape=%s reference_crs=%s target_crs=%s",
        reference.year,
        target.year,
        tuple(reference.data.shape),
        tuple(target.data.shape),
        reference.crs,
        target.crs,
    )

    nodata_marker = -32768
    src_data = np.where(target.valid_mask, target.data, nodata_marker).astype(np.int32)
    dst_data = np.full(reference.data.shape, nodata_marker, dtype=np.int32)

    reproject(
        source=src_data,
        destination=dst_data,
        src_transform=target.transform,
        src_crs=target.crs,
        dst_transform=reference.transform,
        dst_crs=reference.crs,
        src_nodata=nodata_marker,
        dst_nodata=nodata_marker,
        resampling=Resampling.nearest,
    )

    return YearCrop(
        year=target.year,
        source_url=target.source_url,
        data=dst_data,
        valid_mask=(dst_data != nodata_marker),
        transform=reference.transform,
        crs=reference.crs,
    )


def _aoi_area_ha(geometry: BaseGeometry) -> float:
    geometry_3857 = _transform_geometry_to_crs(geometry, "EPSG:3857")
    area_ha = float(geometry_3857.area) / 10_000.0
    logger.info("landcover_aoi_area_computed area_ha=%.4f", area_ha)
    return area_ha
