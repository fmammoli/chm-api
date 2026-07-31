from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import gzip
from io import BytesIO
import json
import logging
import math
from threading import Lock
from typing import Callable

import mercantile
import numpy as np
from PIL import Image
import pmtiles.reader as pm_reader
from pmtiles.tile import Compression as PMTilesCompression, HeaderDict
import rasterio.features
from rasterio.transform import from_bounds
import requests
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from app.config import Settings
from app.services.chm_service import ServiceValidationError, _extract_geometry, _transform_geometry_to_crs, _validate_geometry
from app.services.landcover_stats_service import _aoi_area_ha

logger = logging.getLogger("chm_api")


@dataclass
class AgbTileStats:
	total_pixels: int
	valid_pixels_2025: int
	sum_agb_mgha_2025: float
	sum_agb_sq_mgha2_2025: float
	min_agb_mgha_2025: float
	max_agb_mgha_2025: float
	valid_area_ha_2025: float
	total_area_ha: float
	total_agb_mg_2025: float
	baseline_total_agb_mg: float
	comparison_total_agb_mg: float
	agb_increase_mg: float
	agb_decrease_mg: float
	agb_increase_area_ha: float
	agb_decrease_area_ha: float
	histogram_counts: np.ndarray
	threshold_counts: dict[float, int]


def validate_agb_stats_request_payload(geojson_obj: dict, settings: Settings) -> None:
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
		"agb_stats_payload_validated payload_bytes=%s geom_type=%s bounds=%s",
		payload_len,
		geom.geom_type,
		tuple(round(v, 6) for v in geom.bounds),
	)


def compute_agb_stats(
	*,
	geojson_obj: dict,
	settings: Settings,
	agb_thresholds_mgha: list[float] | None = None,
	progress_callback: Callable[[int, str | None], None] | None = None,
) -> dict[str, float | int | list[dict[str, float]] | dict[str, str | int | float]]:
	logger.info("agb_stats_pipeline step=1_validate_input status=start")
	geometry = _extract_geometry(geojson_obj)
	_validate_geometry(geometry, settings)
	logger.info("agb_stats_pipeline step=1_validate_input status=done")

	thresholds = _resolve_thresholds(agb_thresholds_mgha, settings)
	bin_edges = np.linspace(
		settings.agb_stats_histogram_min_mgha,
		settings.agb_stats_histogram_max_mgha,
		settings.agb_stats_histogram_bins + 1,
		dtype=np.float64,
	)

	if progress_callback is not None:
		progress_callback(15, "Opening AGB PMTiles sources")

	baseline_year = settings.agb_stats_baseline_year
	comparison_year = settings.agb_stats_comparison_year
	baseline_url = _resolve_year_url(settings, baseline_year)
	comparison_url = _resolve_year_url(settings, comparison_year)

	logger.info(
		"agb_stats_pipeline step=2_open_pmtiles status=start baseline_year=%s comparison_year=%s",
		baseline_year,
		comparison_year,
	)
	baseline_reader = _build_pmtiles_reader(baseline_url)
	comparison_reader = _build_pmtiles_reader(comparison_url)
	baseline_header = baseline_reader.header()
	comparison_header = comparison_reader.header()
	for header, year in ((baseline_header, baseline_year), (comparison_header, comparison_year)):
		tile_type = str(getattr(header["tile_type"], "name", header["tile_type"]))
		if tile_type != "PNG":
			raise ServiceValidationError(f"Unsupported AGB PMTiles tile type for {year}: {tile_type}")
	logger.info("agb_stats_pipeline step=2_open_pmtiles status=done")

	max_zoom = min(int(baseline_header["max_zoom"]), int(comparison_header["max_zoom"]))
	target_zoom = min(settings.agb_stats_pmtiles_zoom, max_zoom)
	tiles = list(mercantile.tiles(*geometry.bounds, [target_zoom], truncate=True))
	if not tiles:
		raise ServiceValidationError("No PMTiles tiles intersect the AOI")
	if len(tiles) > settings.agb_stats_max_tiles_per_request:
		raise ServiceValidationError("AOI intersects too many AGB PMTiles tiles")

	logger.info("agb_stats_tiles_selected target_zoom=%s tile_count=%s", target_zoom, len(tiles))
	if progress_callback is not None:
		progress_callback(25, f"Processing {len(tiles)} AGB PMTiles tiles")

	geometry_3857 = _transform_geometry_to_crs(geometry, "EPSG:3857")
	worker_count = max(1, min(settings.agb_stats_tile_fetch_concurrency, len(tiles), 8))
	aggregate = _empty_tile_stats(bin_count=settings.agb_stats_histogram_bins, thresholds=thresholds)
	processed = 0

	with ThreadPoolExecutor(max_workers=worker_count) as executor:
		futures = [
			executor.submit(
				_process_agb_tile,
				tile,
				geometry_3857,
				baseline_reader,
				baseline_header,
				comparison_reader,
				comparison_header,
				thresholds,
				bin_edges,
				settings,
			)
			for tile in tiles
		]

		for future in futures:
			tile_stats = future.result()
			_merge_tile_stats(aggregate, tile_stats)
			processed += 1
			if progress_callback is not None:
				progress_callback(25 + int((processed / len(tiles)) * 55), f"Processed {processed}/{len(tiles)} tiles")

	if aggregate.total_pixels == 0:
		raise ServiceValidationError("No PMTiles coverage intersects the AOI")
	if aggregate.valid_pixels_2025 == 0:
		raise ServiceValidationError("No valid AGB pixels found inside AOI")
	if not math.isfinite(aggregate.min_agb_mgha_2025) or not math.isfinite(aggregate.max_agb_mgha_2025):
		raise ServiceValidationError("Unable to derive AGB min/max values from AOI")

	if progress_callback is not None:
		progress_callback(90, "Finalizing AGB statistics")

	mean_agb = aggregate.sum_agb_mgha_2025 / aggregate.valid_pixels_2025
	variance = max(0.0, (aggregate.sum_agb_sq_mgha2_2025 / aggregate.valid_pixels_2025) - (mean_agb * mean_agb))
	stddev = math.sqrt(variance)
	p10 = _hist_quantile(aggregate.histogram_counts, bin_edges, 0.10)
	p25 = _hist_quantile(aggregate.histogram_counts, bin_edges, 0.25)
	p50 = _hist_quantile(aggregate.histogram_counts, bin_edges, 0.50)
	p75 = _hist_quantile(aggregate.histogram_counts, bin_edges, 0.75)
	p90 = _hist_quantile(aggregate.histogram_counts, bin_edges, 0.90)
	p95 = _hist_quantile(aggregate.histogram_counts, bin_edges, 0.95)
	aoi_area_ha = _aoi_area_ha(geometry)
	coverage_fraction = aggregate.valid_pixels_2025 / aggregate.total_pixels
	threshold_metrics: list[dict[str, float]] = []
	for threshold in thresholds:
		hits = aggregate.threshold_counts.get(threshold, 0)
		ratio = (hits / aggregate.valid_pixels_2025) if aggregate.valid_pixels_2025 else 0.0
		threshold_metrics.append(
			{
				"thresholdMgHa": round(threshold, 4),
				"coverRatio": round(ratio, 6),
				"coverPercent": round(ratio * 100.0, 4),
				"coverAreaHa": round(aggregate.valid_area_ha_2025 * ratio, 4),
			}
		)

	comparison_total = aggregate.comparison_total_agb_mg
	baseline_total = aggregate.baseline_total_agb_mg
	net_change = comparison_total - baseline_total
	net_change_pct = (net_change / baseline_total * 100.0) if baseline_total > 0 else 0.0
	total_agb_mgha_2025 = (
		comparison_total / aggregate.valid_area_ha_2025 if aggregate.valid_area_ha_2025 > 0 else 0.0
	)

	return {
		"baselineYear": baseline_year,
		"comparisonYear": comparison_year,
		"minAgbMgHa": round(aggregate.min_agb_mgha_2025, 4),
		"maxAgbMgHa": round(aggregate.max_agb_mgha_2025, 4),
		"meanAgbMgHa": round(mean_agb, 4),
		"medianAgbMgHa": round(p50, 4),
		"stdDevAgbMgHa": round(stddev, 4),
		"varianceAgbMgHa2": round(variance, 4),
		"p10AgbMgHa": round(p10, 4),
		"p25AgbMgHa": round(p25, 4),
		"p75AgbMgHa": round(p75, 4),
		"p90AgbMgHa": round(p90, 4),
		"p95AgbMgHa": round(p95, 4),
		"interquartileRangeMgHa": round(p75 - p25, 4),
		"coefficientOfVariation": round((stddev / mean_agb) if mean_agb > 0 else 0.0, 6),
		"totalAgbMg": round(comparison_total, 4),
		"totalAgbMgHa": round(total_agb_mgha_2025, 4),
		"baselineTotalAgbMg": round(baseline_total, 4),
		"comparisonTotalAgbMg": round(comparison_total, 4),
		"agbIncreaseMg": round(aggregate.agb_increase_mg, 4),
		"agbDecreaseMg": round(aggregate.agb_decrease_mg, 4),
		"netChangeAgbMg": round(net_change, 4),
		"netChangeAgbMgHa": round(net_change / aggregate.valid_area_ha_2025 if aggregate.valid_area_ha_2025 > 0 else 0.0, 4),
		"netChangePercent": round(net_change_pct, 4),
		"agbIncreaseAreaHa": round(aggregate.agb_increase_area_ha, 4),
		"agbDecreaseAreaHa": round(aggregate.agb_decrease_area_ha, 4),
		"analyzedAreaHa": round(aggregate.valid_area_ha_2025, 4),
		"aoiAreaHa": round(aoi_area_ha, 4),
		"coverageFraction": round(coverage_fraction, 6),
		"validPixelCount": aggregate.valid_pixels_2025,
		"agbCoverByThreshold": threshold_metrics,
		"metadata": {
			"baselineUrl": baseline_url,
			"comparisonUrl": comparison_url,
			"sourceFormat": "pmtiles_png",
			"zoom": target_zoom,
			"tileCount": len(tiles),
			"histogramBins": settings.agb_stats_histogram_bins,
			"histogramMinMgHa": settings.agb_stats_histogram_min_mgha,
			"histogramMaxMgHa": settings.agb_stats_histogram_max_mgha,
			"thresholdsMgHa": ",".join(f"{threshold:g}" for threshold in thresholds),
			"baselineYear": baseline_year,
			"comparisonYear": comparison_year,
		},
	}


def _resolve_thresholds(custom_thresholds: list[float] | None, settings: Settings) -> list[float]:
	source = custom_thresholds if custom_thresholds else settings.agb_stats_default_thresholds_mgha
	unique_sorted = sorted({float(value) for value in source if value > 0.0})
	if not unique_sorted:
		raise ServiceValidationError("No valid AGB thresholds configured")
	if len(unique_sorted) > settings.agb_stats_max_thresholds:
		raise ServiceValidationError("Too many AGB thresholds requested")
	return unique_sorted


def _resolve_year_url(settings: Settings, year: int) -> str:
	resolved = settings.agb_stats_url_template.format(base_url=settings.agb_stats_base_url.rstrip("/"), year=year)
	logger.info("agb_year_url_template year=%s url=%s", year, resolved)
	return resolved


def _build_pmtiles_reader(url: str) -> pm_reader.Reader:
	logger.info("agb_stats_pmtiles_reader_init url=%s", url)
	session = requests.Session()
	lock = Lock()
	cache: dict[tuple[int, int], bytes] = {}

	def get_bytes(offset: int, length: int) -> bytes:
		key = (offset, length)
		with lock:
			cached = cache.get(key)
		if cached is not None:
			return cached

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
		return payload

	return pm_reader.Reader(get_bytes)


def _process_agb_tile(
	tile: mercantile.Tile,
	geometry_3857: BaseGeometry,
	baseline_reader: pm_reader.Reader,
	baseline_header: HeaderDict,
	comparison_reader: pm_reader.Reader,
	comparison_header: HeaderDict,
	thresholds: list[float],
	bin_edges: np.ndarray,
	settings: Settings,
) -> AgbTileStats:
	baseline_bytes = baseline_reader.get(tile.z, tile.x, tile.y)
	comparison_bytes = comparison_reader.get(tile.z, tile.x, tile.y)
	if not baseline_bytes or not comparison_bytes:
		return _stats_for_missing_tile(tile, geometry_3857, thresholds, len(bin_edges) - 1)

	baseline_values, baseline_valid = _decode_pmtiles_tile_to_agb_values(baseline_bytes, baseline_header["tile_compression"])
	comparison_values, comparison_valid = _decode_pmtiles_tile_to_agb_values(comparison_bytes, comparison_header["tile_compression"])
	height, width = comparison_values.shape
	aoi_mask = _rasterize_tile_aoi_mask(tile, geometry_3857, width, height)
	total_pixels = int(aoi_mask.sum())
	if total_pixels == 0:
		return _empty_tile_stats(bin_count=len(bin_edges) - 1, thresholds=thresholds)

	comparison_mask = (
		aoi_mask
		& comparison_valid
		& np.isfinite(comparison_values)
		& (comparison_values >= settings.agb_stats_histogram_min_mgha)
		& (comparison_values <= settings.agb_stats_histogram_max_mgha)
	)
	baseline_mask = (
		aoi_mask
		& baseline_valid
		& np.isfinite(baseline_values)
		& (baseline_values >= settings.agb_stats_histogram_min_mgha)
		& (baseline_values <= settings.agb_stats_histogram_max_mgha)
	)
	common_mask = baseline_mask & comparison_mask
	comparison_valid_pixels = int(comparison_mask.sum())
	baseline_valid_pixels = int(baseline_mask.sum())
	common_valid_pixels = int(common_mask.sum())
	pixel_area_ha = _tile_pixel_area_ha(tile, width, height)
	total_area_ha = total_pixels * pixel_area_ha
	if comparison_valid_pixels == 0:
		return AgbTileStats(
			total_pixels=total_pixels,
			valid_pixels_2025=comparison_valid_pixels,
			sum_agb_mgha_2025=0.0,
			sum_agb_sq_mgha2_2025=0.0,
			min_agb_mgha_2025=0.0,
			max_agb_mgha_2025=0.0,
			valid_area_ha_2025=comparison_valid_pixels * pixel_area_ha,
			total_area_ha=total_area_ha,
			total_agb_mg_2025=0.0,
			baseline_total_agb_mg=0.0,
			comparison_total_agb_mg=0.0,
			agb_increase_mg=0.0,
			agb_decrease_mg=0.0,
			agb_increase_area_ha=0.0,
			agb_decrease_area_ha=0.0,
			histogram_counts=np.zeros(len(bin_edges) - 1, dtype=np.int64),
			threshold_counts={threshold: 0 for threshold in thresholds},
		)

	comparison_values_valid = comparison_values[comparison_mask].astype(np.float64, copy=False)
	baseline_values_valid = baseline_values[baseline_mask].astype(np.float64, copy=False)
	if common_valid_pixels > 0:
		comparison_common = comparison_values[common_mask].astype(np.float64, copy=False)
		baseline_common = baseline_values[common_mask].astype(np.float64, copy=False)
		delta = comparison_common - baseline_common
		positive_delta = delta[delta > 0]
		negative_delta = delta[delta < 0]
	else:
		positive_delta = np.array([], dtype=np.float64)
		negative_delta = np.array([], dtype=np.float64)
	histogram_counts, _ = np.histogram(comparison_values_valid, bins=bin_edges)
	threshold_counts = {threshold: int(np.count_nonzero(comparison_values_valid >= threshold)) for threshold in thresholds}
	comparison_total_agb_mg = float(comparison_values_valid.sum() * pixel_area_ha)
	baseline_total_agb_mg = float(baseline_values_valid.sum() * pixel_area_ha)
	agb_increase_mg = float(positive_delta.sum() * pixel_area_ha)
	agb_decrease_mg = float((-negative_delta).sum() * pixel_area_ha)

	return AgbTileStats(
		total_pixels=total_pixels,
		valid_pixels_2025=comparison_valid_pixels,
		sum_agb_mgha_2025=float(comparison_values_valid.sum()),
		sum_agb_sq_mgha2_2025=float(np.square(comparison_values_valid).sum()),
		min_agb_mgha_2025=float(comparison_values_valid.min()),
		max_agb_mgha_2025=float(comparison_values_valid.max()),
		valid_area_ha_2025=comparison_valid_pixels * pixel_area_ha,
		total_area_ha=total_area_ha,
		total_agb_mg_2025=comparison_total_agb_mg,
		baseline_total_agb_mg=baseline_total_agb_mg,
		comparison_total_agb_mg=comparison_total_agb_mg,
		agb_increase_mg=agb_increase_mg,
		agb_decrease_mg=agb_decrease_mg,
		agb_increase_area_ha=positive_delta.size * pixel_area_ha,
		agb_decrease_area_ha=negative_delta.size * pixel_area_ha,
		histogram_counts=histogram_counts.astype(np.int64),
		threshold_counts=threshold_counts,
	)


def _decode_pmtiles_tile_to_agb_values(tile_bytes: bytes, compression: PMTilesCompression) -> tuple[np.ndarray, np.ndarray]:
	if compression == PMTilesCompression.GZIP:
		tile_bytes = gzip.decompress(tile_bytes)
	elif compression not in {PMTilesCompression.NONE, PMTilesCompression.GZIP}:
		raise ServiceValidationError(f"Unsupported PMTiles tile compression: {compression}")

	image = Image.open(BytesIO(tile_bytes))
	if image.mode not in {"L", "LA", "I;16", "I", "RGB", "RGBA"}:
		image = image.convert("RGBA")
	arr = np.asarray(image)

	if arr.ndim == 2:
		values = arr.astype(np.float32, copy=False)
		valid = np.isfinite(values)
		return values, valid

	channel_count = arr.shape[2]
	if channel_count == 2:
		values = arr[:, :, 0].astype(np.float32, copy=False)
		valid = arr[:, :, 1] > 0
		return values, valid

	if channel_count >= 3:
		rgb = arr[:, :, :3]
		values = _decode_rgb_agb_values(rgb)
		if channel_count >= 4:
			valid = arr[:, :, 3] > 0
		else:
			valid = np.isfinite(values)
		return values, valid

	raise ServiceValidationError("Unsupported PMTiles image shape")


def _decode_rgb_agb_values(rgb: np.ndarray) -> np.ndarray:
	red = rgb[:, :, 0].astype(np.uint16)
	green = rgb[:, :, 1].astype(np.uint16)
	blue = rgb[:, :, 2].astype(np.uint16)

	if np.all(blue == 0):
		if not np.any(green):
			return red.astype(np.float32)

		packed = (red << 8) + green
		nonzero = packed[packed > 0]
		if nonzero.size > 0 and float(np.percentile(nonzero, 95)) > 255.0:
			return packed.astype(np.float32)
		return red.astype(np.float32)

	return (0.2126 * red + 0.7152 * green + 0.0722 * blue).astype(np.float32)


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


def _tile_pixel_area_ha(tile: mercantile.Tile, width: int, height: int) -> float:
	bounds = mercantile.xy_bounds(tile)
	pixel_width = (bounds.right - bounds.left) / width
	pixel_height = (bounds.top - bounds.bottom) / height
	return abs(pixel_width * pixel_height) / 10_000.0


def _empty_tile_stats(bin_count: int, thresholds: list[float]) -> AgbTileStats:
	return AgbTileStats(
		total_pixels=0,
		valid_pixels_2025=0,
		sum_agb_mgha_2025=0.0,
		sum_agb_sq_mgha2_2025=0.0,
		min_agb_mgha_2025=float("inf"),
		max_agb_mgha_2025=float("-inf"),
		valid_area_ha_2025=0.0,
		total_area_ha=0.0,
		total_agb_mg_2025=0.0,
		baseline_total_agb_mg=0.0,
		comparison_total_agb_mg=0.0,
		agb_increase_mg=0.0,
		agb_decrease_mg=0.0,
		agb_increase_area_ha=0.0,
		agb_decrease_area_ha=0.0,
		histogram_counts=np.zeros(bin_count, dtype=np.int64),
		threshold_counts={threshold: 0 for threshold in thresholds},
	)


def _stats_for_missing_tile(
	tile: mercantile.Tile,
	geometry_3857: BaseGeometry,
	thresholds: list[float],
	bin_count: int,
) -> AgbTileStats:
	width = 256
	height = 256
	mask = _rasterize_tile_aoi_mask(tile, geometry_3857, width, height)
	total_pixels = int(mask.sum())
	if total_pixels == 0:
		return _empty_tile_stats(bin_count=bin_count, thresholds=thresholds)
	pixel_area_ha = _tile_pixel_area_ha(tile, width, height)
	return AgbTileStats(
		total_pixels=total_pixels,
		valid_pixels_2025=0,
		sum_agb_mgha_2025=0.0,
		sum_agb_sq_mgha2_2025=0.0,
		min_agb_mgha_2025=float("inf"),
		max_agb_mgha_2025=float("-inf"),
		valid_area_ha_2025=0.0,
		total_area_ha=total_pixels * pixel_area_ha,
		total_agb_mg_2025=0.0,
		baseline_total_agb_mg=0.0,
		comparison_total_agb_mg=0.0,
		agb_increase_mg=0.0,
		agb_decrease_mg=0.0,
		agb_increase_area_ha=0.0,
		agb_decrease_area_ha=0.0,
		histogram_counts=np.zeros(bin_count, dtype=np.int64),
		threshold_counts={threshold: 0 for threshold in thresholds},
	)


def _merge_tile_stats(into: AgbTileStats, tile_stats: AgbTileStats) -> None:
	into.total_pixels += tile_stats.total_pixels
	into.valid_pixels_2025 += tile_stats.valid_pixels_2025
	into.sum_agb_mgha_2025 += tile_stats.sum_agb_mgha_2025
	into.sum_agb_sq_mgha2_2025 += tile_stats.sum_agb_sq_mgha2_2025
	into.valid_area_ha_2025 += tile_stats.valid_area_ha_2025
	into.total_area_ha += tile_stats.total_area_ha
	into.total_agb_mg_2025 += tile_stats.total_agb_mg_2025
	into.baseline_total_agb_mg += tile_stats.baseline_total_agb_mg
	into.comparison_total_agb_mg += tile_stats.comparison_total_agb_mg
	into.agb_increase_mg += tile_stats.agb_increase_mg
	into.agb_decrease_mg += tile_stats.agb_decrease_mg
	into.agb_increase_area_ha += tile_stats.agb_increase_area_ha
	into.agb_decrease_area_ha += tile_stats.agb_decrease_area_ha
	into.histogram_counts += tile_stats.histogram_counts
	for threshold, count in tile_stats.threshold_counts.items():
		into.threshold_counts[threshold] = into.threshold_counts.get(threshold, 0) + count
	if math.isfinite(tile_stats.min_agb_mgha_2025):
		if not math.isfinite(into.min_agb_mgha_2025) or tile_stats.min_agb_mgha_2025 < into.min_agb_mgha_2025:
			into.min_agb_mgha_2025 = tile_stats.min_agb_mgha_2025
	if math.isfinite(tile_stats.max_agb_mgha_2025):
		if not math.isfinite(into.max_agb_mgha_2025) or tile_stats.max_agb_mgha_2025 > into.max_agb_mgha_2025:
			into.max_agb_mgha_2025 = tile_stats.max_agb_mgha_2025


def _hist_quantile(histogram_counts: np.ndarray, bin_edges: np.ndarray, quantile: float) -> float:
	total = int(histogram_counts.sum())
	if total <= 0:
		return 0.0
	target = quantile * total
	cumulative = np.cumsum(histogram_counts)
	index = int(np.searchsorted(cumulative, target, side="left"))
	index = min(max(index, 0), len(bin_edges) - 2)
	left = float(bin_edges[index])
	right = float(bin_edges[index + 1])
	prev = 0 if index == 0 else int(cumulative[index - 1])
	count = int(histogram_counts[index])
	if count <= 0:
		return left
	position = (target - prev) / count
	return left + max(0.0, min(1.0, position)) * (right - left)