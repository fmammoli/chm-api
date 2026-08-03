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
from app.services.chm_service import (
	ServiceValidationError,
	_extract_geometry,
	_transform_geometry_to_crs,
	_validate_geometry,
)
from app.services.landcover_stats_service import _aoi_area_ha

logger = logging.getLogger("chm_api")

# CHM PMTiles in this project store heights in the red channel with 0.4 m steps.
_CHM_RED_CHANNEL_SCALE_M = 0.4


@dataclass
class TileChmStats:
	total_pixels: int
	valid_pixels: int
	sum_height_m: float
	sum_height_sq_m2: float
	min_height_m: float
	max_height_m: float
	valid_area_ha: float
	total_area_ha: float
	canopy_volume_proxy_m3: float
	histogram_counts: np.ndarray
	threshold_counts: dict[float, int]


def validate_chm_stats_request_payload(geojson_obj: dict, settings: Settings) -> None:
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
		"chm_stats_payload_validated payload_bytes=%s geom_type=%s bounds=%s",
		payload_len,
		geom.geom_type,
		tuple(round(v, 6) for v in geom.bounds),
	)


def compute_chm_stats(
	*,
	geojson_obj: dict,
	settings: Settings,
	canopy_thresholds_m: list[float] | None = None,
	progress_callback: Callable[[int, str | None], None] | None = None,
) -> dict[str, float | int | list[dict[str, float]] | dict[str, str | int | float]]:
	logger.info("chm_stats_pipeline step=1_validate_input status=start")
	geometry = _extract_geometry(geojson_obj)
	_validate_geometry(geometry, settings)
	logger.info("chm_stats_pipeline step=1_validate_input status=done")

	thresholds = _resolve_thresholds(canopy_thresholds_m, settings)
	bin_edges = np.linspace(
		settings.chm_stats_histogram_min_m,
		settings.chm_stats_histogram_max_m,
		settings.chm_stats_histogram_bins + 1,
		dtype=np.float64,
	)

	if progress_callback is not None:
		progress_callback(20, "Opening CHM PMTiles source")

	logger.info("chm_stats_pipeline step=2_open_pmtiles status=start")
	reader = _build_pmtiles_reader(settings.chm_stats_pmtiles_url)
	header = reader.header()
	tile_type = str(getattr(header["tile_type"], "name", header["tile_type"]))
	if tile_type != "PNG":
		raise ServiceValidationError(f"Unsupported CHM PMTiles tile type: {tile_type}")
	logger.info("chm_stats_pipeline step=2_open_pmtiles status=done tile_type=%s", tile_type)

	max_zoom = int(header["max_zoom"])
	target_zoom, tiles = _select_stats_zoom_and_tiles(
		geometry=geometry,
		requested_zoom=min(settings.chm_stats_pmtiles_zoom, max_zoom),
		max_zoom=max_zoom,
		max_tiles=settings.chm_stats_max_tiles_per_request,
	)
	if not tiles:
		raise ServiceValidationError("No PMTiles tiles intersect the AOI")

	logger.info(
		"chm_stats_tiles_selected target_zoom=%s tile_count=%s",
		target_zoom,
		len(tiles),
	)

	if progress_callback is not None:
		progress_callback(35, f"Processing {len(tiles)} CHM PMTiles tiles")

	logger.info("chm_stats_pipeline step=3_process_tiles status=start")
	geometry_3857 = _transform_geometry_to_crs(geometry, "EPSG:3857")
	worker_count = max(1, min(settings.chm_stats_tile_fetch_concurrency, len(tiles), 8))
	aggregate = _empty_tile_stats(bin_count=settings.chm_stats_histogram_bins, thresholds=thresholds)
	processed = 0

	with ThreadPoolExecutor(max_workers=worker_count) as executor:
		futures = [
			executor.submit(
				_process_chm_tile,
				tile,
				geometry_3857,
				reader,
				header,
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
				progress_callback(35 + int((processed / len(tiles)) * 50), f"Processed {processed}/{len(tiles)} tiles")

	logger.info("chm_stats_pipeline step=3_process_tiles status=done processed_tiles=%s", processed)

	if aggregate.total_pixels == 0:
		raise ServiceValidationError("No PMTiles coverage intersects the AOI")
	if aggregate.valid_pixels == 0:
		raise ServiceValidationError("No valid CHM pixels found inside AOI")
	if not math.isfinite(aggregate.min_height_m) or not math.isfinite(aggregate.max_height_m):
		raise ServiceValidationError("Unable to derive CHM min/max values from AOI")

	if progress_callback is not None:
		progress_callback(92, "Finalizing CHM statistics")

	logger.info("chm_stats_pipeline step=4_finalize_metrics status=start")
	mean_height = aggregate.sum_height_m / aggregate.valid_pixels
	variance = max(0.0, (aggregate.sum_height_sq_m2 / aggregate.valid_pixels) - (mean_height * mean_height))
	stddev = math.sqrt(variance)

	p10 = _hist_quantile(aggregate.histogram_counts, bin_edges, 0.10)
	p25 = _hist_quantile(aggregate.histogram_counts, bin_edges, 0.25)
	p50 = _hist_quantile(aggregate.histogram_counts, bin_edges, 0.50)
	p75 = _hist_quantile(aggregate.histogram_counts, bin_edges, 0.75)
	p90 = _hist_quantile(aggregate.histogram_counts, bin_edges, 0.90)
	p95 = _hist_quantile(aggregate.histogram_counts, bin_edges, 0.95)

	aoi_area_ha = _aoi_area_ha(geometry)
	coverage_fraction = aggregate.valid_pixels / aggregate.total_pixels
	threshold_metrics: list[dict[str, float]] = []
	for threshold in thresholds:
		hits = aggregate.threshold_counts.get(threshold, 0)
		ratio = (hits / aggregate.valid_pixels) if aggregate.valid_pixels else 0.0
		threshold_metrics.append(
			{
				"thresholdM": round(threshold, 4),
				"coverRatio": round(ratio, 6),
				"coverPercent": round(ratio * 100.0, 4),
				"coverAreaHa": round(aggregate.valid_area_ha * ratio, 4),
			}
		)
	range_metrics = _build_threshold_range_metrics(
		thresholds=thresholds,
		threshold_counts=aggregate.threshold_counts,
		valid_pixels=aggregate.valid_pixels,
		valid_area_ha=aggregate.valid_area_ha,
	)

	logger.info(
		"chm_stats_pipeline_done valid_pixels=%s total_pixels=%s coverage=%.6f mean=%.4f min=%.4f max=%.4f",
		aggregate.valid_pixels,
		aggregate.total_pixels,
		coverage_fraction,
		mean_height,
		aggregate.min_height_m,
		aggregate.max_height_m,
	)
	logger.info("chm_stats_pipeline step=4_finalize_metrics status=done")

	return {
		"minCanopyHeightM": round(aggregate.min_height_m, 4),
		"maxCanopyHeightM": round(aggregate.max_height_m, 4),
		"meanCanopyHeightM": round(mean_height, 4),
		"medianCanopyHeightM": round(p50, 4),
		"stdDevCanopyHeightM": round(stddev, 4),
		"varianceCanopyHeightM2": round(variance, 4),
		"p10CanopyHeightM": round(p10, 4),
		"p25CanopyHeightM": round(p25, 4),
		"p75CanopyHeightM": round(p75, 4),
		"p90CanopyHeightM": round(p90, 4),
		"p95CanopyHeightM": round(p95, 4),
		"interquartileRangeM": round(p75 - p25, 4),
		"coefficientOfVariation": round((stddev / mean_height) if mean_height > 0 else 0.0, 6),
		"totalCanopyVolumeProxyM3": round(aggregate.canopy_volume_proxy_m3, 4),
		"analyzedAreaHa": round(aggregate.valid_area_ha, 4),
		"aoiAreaHa": round(aoi_area_ha, 4),
		"coverageFraction": round(coverage_fraction, 6),
		"validPixelCount": aggregate.valid_pixels,
		"canopyCoverByThreshold": threshold_metrics,
		"canopyCoverByRange": range_metrics,
		"metadata": {
			"sourceUrl": settings.chm_stats_pmtiles_url,
			"sourceFormat": "pmtiles_png",
			"zoom": target_zoom,
			"tileCount": len(tiles),
			"histogramBins": settings.chm_stats_histogram_bins,
			"histogramMinM": settings.chm_stats_histogram_min_m,
			"histogramMaxM": settings.chm_stats_histogram_max_m,
			"thresholdsM": ",".join(f"{threshold:g}" for threshold in thresholds),
			"canopyCoverByThresholdMode": "cumulative_ge",
			"canopyCoverByRangeMode": "disjoint_ranges",
		},
	}


def _resolve_thresholds(custom_thresholds: list[float] | None, settings: Settings) -> list[float]:
	source = custom_thresholds if custom_thresholds else settings.chm_stats_default_thresholds_m
	unique_sorted = sorted({float(value) for value in source if value > 0.0})
	if not unique_sorted:
		raise ServiceValidationError("No valid CHM thresholds configured")
	if len(unique_sorted) > settings.chm_stats_max_thresholds:
		raise ServiceValidationError("Too many canopy thresholds requested")
	return unique_sorted


def _select_stats_zoom_and_tiles(
	*,
	geometry: BaseGeometry,
	requested_zoom: int,
	max_zoom: int,
	max_tiles: int,
) -> tuple[int, list[mercantile.Tile]]:
	best_zoom = requested_zoom
	best_tiles = list(mercantile.tiles(*geometry.bounds, [requested_zoom], truncate=True))
	if len(best_tiles) > max_tiles:
		raise ServiceValidationError("AOI intersects too many CHM PMTiles tiles")

	# Improve statistical fidelity by using the finest zoom that still respects tile limits.
	for zoom in range(requested_zoom + 1, max_zoom + 1):
		candidate_tiles = list(mercantile.tiles(*geometry.bounds, [zoom], truncate=True))
		if len(candidate_tiles) > max_tiles:
			break
		best_zoom = zoom
		best_tiles = candidate_tiles

	return best_zoom, best_tiles


def _build_pmtiles_reader(url: str) -> pm_reader.Reader:
	logger.info("chm_stats_pmtiles_reader_init url=%s", url)
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


def _process_chm_tile(
	tile: mercantile.Tile,
	geometry_3857: BaseGeometry,
	reader: pm_reader.Reader,
	header: HeaderDict,
	thresholds: list[float],
	bin_edges: np.ndarray,
	settings: Settings,
) -> TileChmStats:
	tile_bytes = reader.get(tile.z, tile.x, tile.y)
	if not tile_bytes:
		return _stats_for_missing_tile(tile, geometry_3857, thresholds, len(bin_edges) - 1)

	heights, valid_from_source = _decode_pmtiles_tile_to_heights(tile_bytes, header["tile_compression"])
	height, width = heights.shape
	aoi_mask = _rasterize_tile_aoi_mask(tile, geometry_3857, width, height)
	total_pixels = int(aoi_mask.sum())
	if total_pixels == 0:
		return _empty_tile_stats(bin_count=len(bin_edges) - 1, thresholds=thresholds)

	valid_mask = (
		aoi_mask
		& valid_from_source
		& np.isfinite(heights)
		& (heights >= settings.chm_stats_histogram_min_m)
		& (heights <= settings.chm_stats_histogram_max_m)
	)

	valid_pixels = int(valid_mask.sum())
	pixel_area_ha = _tile_pixel_area_ha(tile, width, height)
	total_area_ha = total_pixels * pixel_area_ha
	if valid_pixels == 0:
		return TileChmStats(
			total_pixels=total_pixels,
			valid_pixels=0,
			sum_height_m=0.0,
			sum_height_sq_m2=0.0,
			min_height_m=0.0,
			max_height_m=0.0,
			valid_area_ha=0.0,
			total_area_ha=total_area_ha,
			canopy_volume_proxy_m3=0.0,
			histogram_counts=np.zeros(len(bin_edges) - 1, dtype=np.int64),
			threshold_counts={threshold: 0 for threshold in thresholds},
		)

	values = heights[valid_mask].astype(np.float64, copy=False)
	histogram_counts, _ = np.histogram(values, bins=bin_edges)
	threshold_counts = {threshold: int(np.count_nonzero(values >= threshold)) for threshold in thresholds}
	pixel_area_m2 = pixel_area_ha * 10_000.0

	return TileChmStats(
		total_pixels=total_pixels,
		valid_pixels=valid_pixels,
		sum_height_m=float(values.sum()),
		sum_height_sq_m2=float(np.square(values).sum()),
		min_height_m=float(values.min()),
		max_height_m=float(values.max()),
		valid_area_ha=valid_pixels * pixel_area_ha,
		total_area_ha=total_area_ha,
		canopy_volume_proxy_m3=float(values.sum() * pixel_area_m2),
		histogram_counts=histogram_counts.astype(np.int64),
		threshold_counts=threshold_counts,
	)


def _decode_pmtiles_tile_to_heights(tile_bytes: bytes, compression: PMTilesCompression) -> tuple[np.ndarray, np.ndarray]:
	if compression == PMTilesCompression.GZIP:
		tile_bytes = gzip.decompress(tile_bytes)
	elif compression not in {PMTilesCompression.NONE, PMTilesCompression.GZIP}:
		raise ServiceValidationError(f"Unsupported PMTiles tile compression: {compression}")

	image = Image.open(BytesIO(tile_bytes))
	if image.mode not in {"L", "LA", "I;16", "I", "RGB", "RGBA"}:
		image = image.convert("RGBA")
	arr = np.asarray(image)

	if arr.ndim == 2:
		heights = arr.astype(np.float32, copy=False)
		valid = np.isfinite(heights)
		return heights, valid

	channel_count = arr.shape[2]
	if channel_count == 2:
		heights = arr[:, :, 0].astype(np.float32, copy=False)
		valid = arr[:, :, 1] > 0
		return heights, valid

	if channel_count >= 3:
		rgb = arr[:, :, :3]
		heights = _decode_rgb_height_values(rgb)
		if channel_count >= 4:
			valid = arr[:, :, 3] > 0
		else:
			valid = np.isfinite(heights)
		return heights, valid

	raise ServiceValidationError("Unsupported PMTiles image shape")


def _decode_rgb_height_values(rgb: np.ndarray) -> np.ndarray:
	red = rgb[:, :, 0].astype(np.uint16)
	green = rgb[:, :, 1].astype(np.uint16)
	blue = rgb[:, :, 2].astype(np.uint16)

	# Common CHM encoding packs 16-bit values across R/G with B as sentinel.
	if np.all(blue == 0):
		# If green is entirely zero, this dataset uses red-only quantized heights.
		# Decoding as packed R/G would create artificial values (e.g. 102.4 m spikes).
		if not np.any(green):
			return (red.astype(np.float32) * _CHM_RED_CHANNEL_SCALE_M).astype(np.float32)

		packed = (red << 8) + green
		nonzero = packed[packed > 0]
		if nonzero.size > 0 and float(np.percentile(nonzero, 95)) > 255.0:
			return (packed.astype(np.float32) / 10.0).astype(np.float32)
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


def _empty_tile_stats(bin_count: int, thresholds: list[float]) -> TileChmStats:
	return TileChmStats(
		total_pixels=0,
		valid_pixels=0,
		sum_height_m=0.0,
		sum_height_sq_m2=0.0,
		min_height_m=float("inf"),
		max_height_m=float("-inf"),
		valid_area_ha=0.0,
		total_area_ha=0.0,
		canopy_volume_proxy_m3=0.0,
		histogram_counts=np.zeros(bin_count, dtype=np.int64),
		threshold_counts={threshold: 0 for threshold in thresholds},
	)


def _stats_for_missing_tile(
	tile: mercantile.Tile,
	geometry_3857: BaseGeometry,
	thresholds: list[float],
	bin_count: int,
) -> TileChmStats:
	width = 256
	height = 256
	mask = _rasterize_tile_aoi_mask(tile, geometry_3857, width, height)
	total_pixels = int(mask.sum())
	if total_pixels == 0:
		return _empty_tile_stats(bin_count=bin_count, thresholds=thresholds)
	pixel_area_ha = _tile_pixel_area_ha(tile, width, height)
	return TileChmStats(
		total_pixels=total_pixels,
		valid_pixels=0,
		sum_height_m=0.0,
		sum_height_sq_m2=0.0,
		min_height_m=float("inf"),
		max_height_m=float("-inf"),
		valid_area_ha=0.0,
		total_area_ha=total_pixels * pixel_area_ha,
		canopy_volume_proxy_m3=0.0,
		histogram_counts=np.zeros(bin_count, dtype=np.int64),
		threshold_counts={threshold: 0 for threshold in thresholds},
	)


def _merge_tile_stats(into: TileChmStats, tile_stats: TileChmStats) -> None:
	into.total_pixels += tile_stats.total_pixels
	into.valid_pixels += tile_stats.valid_pixels
	into.sum_height_m += tile_stats.sum_height_m
	into.sum_height_sq_m2 += tile_stats.sum_height_sq_m2
	into.valid_area_ha += tile_stats.valid_area_ha
	into.total_area_ha += tile_stats.total_area_ha
	into.canopy_volume_proxy_m3 += tile_stats.canopy_volume_proxy_m3
	into.histogram_counts += tile_stats.histogram_counts
	for threshold, value in tile_stats.threshold_counts.items():
		into.threshold_counts[threshold] = into.threshold_counts.get(threshold, 0) + value

	if tile_stats.valid_pixels > 0:
		into.min_height_m = min(into.min_height_m, tile_stats.min_height_m)
		into.max_height_m = max(into.max_height_m, tile_stats.max_height_m)


def _hist_quantile(hist_counts: np.ndarray, bin_edges: np.ndarray, quantile: float) -> float:
	total = int(hist_counts.sum())
	if total <= 0:
		return 0.0

	target = quantile * total
	cumulative = np.cumsum(hist_counts)
	idx = int(np.searchsorted(cumulative, target, side="left"))
	idx = max(0, min(idx, len(hist_counts) - 1))

	left = float(bin_edges[idx])
	right = float(bin_edges[idx + 1])
	prev = int(cumulative[idx - 1]) if idx > 0 else 0
	bin_count = int(hist_counts[idx])
	if bin_count <= 0:
		return (left + right) / 2.0
	fraction = (target - prev) / bin_count
	fraction = max(0.0, min(float(fraction), 1.0))
	return left + ((right - left) * fraction)


def _build_threshold_range_metrics(
	*,
	thresholds: list[float],
	threshold_counts: dict[float, int],
	valid_pixels: int,
	valid_area_ha: float,
) -> list[dict[str, float | str | None]]:
	if valid_pixels <= 0:
		return []

	ranges: list[dict[str, float | str | None]] = []
	first = thresholds[0]
	below_first_count = max(0, valid_pixels - int(threshold_counts.get(first, 0)))
	ranges.append(_range_metric(None, first, below_first_count, valid_pixels, valid_area_ha))

	for idx in range(1, len(thresholds)):
		lower = thresholds[idx - 1]
		upper = thresholds[idx]
		lower_hits = int(threshold_counts.get(lower, 0))
		upper_hits = int(threshold_counts.get(upper, 0))
		count = max(0, lower_hits - upper_hits)
		ranges.append(_range_metric(lower, upper, count, valid_pixels, valid_area_ha))

	last = thresholds[-1]
	last_count = int(threshold_counts.get(last, 0))
	ranges.append(_range_metric(last, None, last_count, valid_pixels, valid_area_ha))
	return ranges


def _range_metric(
	lower: float | None,
	upper: float | None,
	count: int,
	valid_pixels: int,
	valid_area_ha: float,
) -> dict[str, float | str | None]:
	ratio = (count / valid_pixels) if valid_pixels else 0.0
	return {
		"lowerBoundM": round(lower, 4) if lower is not None else None,
		"upperBoundM": round(upper, 4) if upper is not None else None,
		"label": _range_label(lower, upper),
		"coverRatio": round(ratio, 6),
		"coverPercent": round(ratio * 100.0, 4),
		"coverAreaHa": round(valid_area_ha * ratio, 4),
	}


def _range_label(lower: float | None, upper: float | None) -> str:
	if lower is None and upper is None:
		return "all"
	if lower is None:
		return f"<{upper:g}m"
	if upper is None:
		return f">={lower:g}m"
	return f"{lower:g}-{upper:g}m"
