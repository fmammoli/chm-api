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
import rasterio
import rasterio.features
import rasterio.mask
from rasterio.transform import from_bounds
import requests
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from app.config import Settings
from app.services.chm_service import (
	ServiceValidationError,
	_ctrees_agb_remote_cog_url,
	_extract_geometry,
	_transform_geometry_to_crs,
	_validate_geometry,
)
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


@dataclass
class AgbCogYearData:
	year: int
	url: str
	scale_factor: float
	values: np.ndarray
	aoi_mask: np.ndarray
	valid_mask: np.ndarray


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
	_validate_geometry(geom, settings, enforce_indonesia_only=False)
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
	_validate_geometry(geometry, settings, enforce_indonesia_only=False)
	logger.info("agb_stats_pipeline step=1_validate_input status=done")

	thresholds = _resolve_thresholds(agb_thresholds_mgha, settings)
	bin_edges = np.linspace(
		settings.agb_stats_histogram_min_mgha,
		settings.agb_stats_histogram_max_mgha,
		settings.agb_stats_histogram_bins + 1,
		dtype=np.float64,
	)

	if progress_callback is not None:
		progress_callback(15, "Opening CTrees AGB COG sources")

	baseline_year = settings.agb_stats_baseline_year
	comparison_year = settings.agb_stats_comparison_year
	baseline_url = _resolve_year_url(settings, baseline_year)
	comparison_url = _resolve_year_url(settings, comparison_year)

	logger.info(
		"agb_stats_pipeline step=2_open_cogs status=start baseline_year=%s comparison_year=%s",
		baseline_year,
		comparison_year,
	)
	baseline_data = _load_agb_cog_year_data(year=baseline_year, geometry=geometry, settings=settings)
	if progress_callback is not None:
		progress_callback(45, f"Loaded baseline AGB COG for {baseline_year}")
	comparison_data = _load_agb_cog_year_data(year=comparison_year, geometry=geometry, settings=settings)
	logger.info("agb_stats_pipeline step=2_open_cogs status=done")
	if not math.isclose(baseline_data.scale_factor, comparison_data.scale_factor, rel_tol=1e-9, abs_tol=1e-9):
		logger.warning(
			"agb_stats_scale_factor_mismatch baseline=%s comparison=%s using_comparison=%s",
			baseline_data.scale_factor,
			comparison_data.scale_factor,
			comparison_data.scale_factor,
		)

	if baseline_data.values.shape != comparison_data.values.shape:
		raise ServiceValidationError("Baseline and comparison COG clips are misaligned (shape mismatch)")
	if not np.array_equal(baseline_data.aoi_mask, comparison_data.aoi_mask):
		raise ServiceValidationError("Baseline and comparison COG clips are misaligned (AOI mask mismatch)")

	total_pixels = int(comparison_data.aoi_mask.sum())
	if total_pixels == 0:
		raise ServiceValidationError("No CTrees AGB coverage intersects the AOI")

	comparison_valid_pixels = int(comparison_data.valid_mask.sum())
	baseline_valid_pixels = int(baseline_data.valid_mask.sum())
	if comparison_valid_pixels == 0:
		raise ServiceValidationError("No valid AGB pixels found inside AOI")

	comparison_values_valid = comparison_data.values[comparison_data.valid_mask].astype(np.float64, copy=False)
	baseline_values_valid = baseline_data.values[baseline_data.valid_mask].astype(np.float64, copy=False)

	if comparison_values_valid.size == 0:
		raise ServiceValidationError("No valid comparison-year AGB values found inside AOI")

	if progress_callback is not None:
		progress_callback(90, "Finalizing AGB statistics")

	mean_agb = float(np.mean(comparison_values_valid))
	variance = float(np.var(comparison_values_valid))
	stddev = math.sqrt(variance)
	p10, p25, p50, p75, p90, p95 = np.quantile(comparison_values_valid, [0.10, 0.25, 0.50, 0.75, 0.90, 0.95]).tolist()
	aoi_area_ha = _aoi_area_ha(geometry)
	coverage_fraction = comparison_valid_pixels / total_pixels
	comparison_area_ha = aoi_area_ha * coverage_fraction
	baseline_area_ha = aoi_area_ha * (baseline_valid_pixels / total_pixels)
	comparison_total = mean_agb * comparison_area_ha
	baseline_mean = float(np.mean(baseline_values_valid)) if baseline_values_valid.size else 0.0
	baseline_total = baseline_mean * baseline_area_ha

	common_mask = baseline_data.valid_mask & comparison_data.valid_mask
	common_valid_pixels = int(common_mask.sum())
	if common_valid_pixels > 0:
		comparison_common = comparison_data.values[common_mask].astype(np.float64, copy=False)
		baseline_common = baseline_data.values[common_mask].astype(np.float64, copy=False)
		delta = comparison_common - baseline_common
		positive_delta = delta[delta > 0]
		negative_delta = delta[delta < 0]
		common_area_ha = aoi_area_ha * (common_valid_pixels / total_pixels)
		common_pixel_area_ha = common_area_ha / common_valid_pixels
		agb_increase_mg = float(positive_delta.sum() * common_pixel_area_ha)
		agb_decrease_mg = float((-negative_delta).sum() * common_pixel_area_ha)
		agb_increase_area_ha = float(positive_delta.size * common_pixel_area_ha)
		agb_decrease_area_ha = float(negative_delta.size * common_pixel_area_ha)
	else:
		agb_increase_mg = 0.0
		agb_decrease_mg = 0.0
		agb_increase_area_ha = 0.0
		agb_decrease_area_ha = 0.0

	hist_min = min(settings.agb_stats_histogram_min_mgha, float(np.min(comparison_values_valid)))
	hist_max = max(settings.agb_stats_histogram_max_mgha, float(np.max(comparison_values_valid)))
	if hist_max <= hist_min:
		hist_max = hist_min + 1.0
	bin_edges = np.linspace(hist_min, hist_max, settings.agb_stats_histogram_bins + 1, dtype=np.float64)
	histogram_counts, _ = np.histogram(comparison_values_valid, bins=bin_edges)
	threshold_counts = {threshold: int(np.count_nonzero(comparison_values_valid >= threshold)) for threshold in thresholds}

	threshold_metrics: list[dict[str, float]] = []
	for threshold in thresholds:
		hits = threshold_counts.get(threshold, 0)
		ratio = (hits / comparison_valid_pixels) if comparison_valid_pixels else 0.0
		threshold_metrics.append(
			{
				"thresholdMgHa": round(threshold, 4),
				"coverRatio": round(ratio, 6),
				"coverPercent": round(ratio * 100.0, 4),
				"coverAreaHa": round(comparison_area_ha * ratio, 4),
			}
		)

	net_change = comparison_total - baseline_total
	net_change_pct = (net_change / baseline_total * 100.0) if baseline_total > 0 else 0.0
	total_agb_mgha_2025 = comparison_total / comparison_area_ha if comparison_area_ha > 0 else 0.0

	return {
		"baselineYear": baseline_year,
		"comparisonYear": comparison_year,
		"minAgbMgHa": round(float(np.min(comparison_values_valid)), 4),
		"maxAgbMgHa": round(float(np.max(comparison_values_valid)), 4),
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
		"agbIncreaseMg": round(agb_increase_mg, 4),
		"agbDecreaseMg": round(agb_decrease_mg, 4),
		"netChangeAgbMg": round(net_change, 4),
		"netChangeAgbMgHa": round(net_change / comparison_area_ha if comparison_area_ha > 0 else 0.0, 4),
		"netChangePercent": round(net_change_pct, 4),
		"agbIncreaseAreaHa": round(agb_increase_area_ha, 4),
		"agbDecreaseAreaHa": round(agb_decrease_area_ha, 4),
		"analyzedAreaHa": round(comparison_area_ha, 4),
		"aoiAreaHa": round(aoi_area_ha, 4),
		"coverageFraction": round(coverage_fraction, 6),
		"validPixelCount": comparison_valid_pixels,
		"agbCoverByThreshold": threshold_metrics,
		"metadata": {
			"baselineUrl": baseline_url,
			"comparisonUrl": comparison_url,
			"sourceFormat": "ctrees_agb_cog",
			"histogramBins": settings.agb_stats_histogram_bins,
			"histogramMinMgHa": round(hist_min, 4),
			"histogramMaxMgHa": round(hist_max, 4),
			"histogramSampleCount": int(histogram_counts.sum()),
			"thresholdsMgHa": ",".join(f"{threshold:g}" for threshold in thresholds),
			"baselineYear": baseline_year,
			"comparisonYear": comparison_year,
			"valueScaleFactor": comparison_data.scale_factor,
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
	resolved = _ctrees_agb_remote_cog_url(settings, year=year, variable="agb")
	logger.info("agb_year_cog_url year=%s url=%s", year, resolved)
	return resolved


def _load_agb_cog_year_data(*, year: int, geometry: BaseGeometry, settings: Settings) -> AgbCogYearData:
	url = _resolve_year_url(settings, year)
	try:
		with rasterio.open(url) as src:
			nodata = _resolve_nodata(src.nodata, src.dtypes[0])
			scale_factor = _resolve_agb_scale_factor(src, settings)
			geom_src = _transform_geometry_to_crs(geometry, src.crs)
			masked, transform = rasterio.mask.mask(
				src,
				[mapping(geom_src)],
				crop=True,
				nodata=nodata,
				all_touched=False,
				filled=True,
			)
	except Exception as exc:
		raise ServiceValidationError(f"Failed to read CTrees AGB COG for year {year}") from exc

	if masked.size == 0:
		raise ServiceValidationError(f"No CTrees AGB data intersects AOI for year {year}")

	values = masked[0].astype(np.float64, copy=False)
	if scale_factor != 1.0:
		values = values / scale_factor
	aoi_mask = rasterio.features.geometry_mask(
		[mapping(geom_src)],
		out_shape=values.shape,
		transform=transform,
		invert=True,
	)
	valid_mask = aoi_mask & np.isfinite(values) & ~np.isclose(values, nodata) & (values >= 0.0)

	return AgbCogYearData(
		year=year,
		url=url,
		scale_factor=scale_factor,
		values=values,
		aoi_mask=aoi_mask,
		valid_mask=valid_mask,
	)


def _resolve_agb_scale_factor(src: rasterio.io.DatasetReader, settings: Settings) -> float:
	# Prefer explicit metadata from source if present; otherwise use configured default.
	tags = src.tags(1)
	for key in ("agb_scale_factor", "scale_factor", "AGB_SCALE_FACTOR", "SCALE_FACTOR"):
		raw = tags.get(key)
		if raw is None:
			continue
		try:
			value = float(raw)
			if value > 0.0:
				return value
		except ValueError:
			continue
	return float(settings.agb_stats_scale_factor)


def _resolve_nodata(nodata: float | None, dtype: str) -> float:
	if nodata is not None:
		return float(nodata)
	if np.issubdtype(np.dtype(dtype), np.integer):
		return float(np.iinfo(np.dtype(dtype)).max)
	return -9999.0


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