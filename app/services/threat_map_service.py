from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import random
import resource
import subprocess
import tarfile
import tempfile
import time
from typing import Any, Callable
import xml.etree.ElementTree as ET
import zipfile

import mercantile
import numpy as np
from PIL import Image, ImageDraw
from pyproj import Transformer
from pydantic import BaseModel, Field
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from app.config import Settings
from app.models import ThreatMapJobCreateRequest, ThreatMapPreset
from app.services.chm_service import ServiceValidationError, _extract_geometry, _transform_geometry_to_crs, _validate_geometry
from app.services.landcover_stats_service import (
    _build_pmtiles_reader,
    _decode_pmtiles_png_tile,
    _extract_pmtiles_class_values,
    _resolve_year_url,
)


YEARS = list(range(1990, 2025))
YEARS_EXPECTED = len(YEARS)
logger = logging.getLogger("chm_api")


class ThreatMapError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ThreatMapResourceLimitError(ThreatMapError):
    pass


@dataclass(frozen=True)
class RenderOptions:
    width: int
    height: int
    fps: float
    frame_duration_seconds: float
    ffmpeg_preset: str
    crf: int


class JobProgressUpdate(BaseModel):
    progress: int = Field(ge=0, le=100)
    message: str | None = None
    current_year: int | None = None


def _resolve_overlay_geojson_inputs(
    payload: ThreatMapJobCreateRequest,
) -> tuple[dict[str, Any], dict[str, Any] | None, str, bool]:
    """Return primary geojson, overlay geojson, overlay CRS, and extraction flag.

    If `overlayGeojson` is provided explicitly, it is used as-is.
    Otherwise, frontend payloads that mix overlay features into `geojson`
    (marked by `properties.source == "threat-map-overlay"`) are split.
    """

    if payload.overlayGeojson is not None:
        return payload.geojson, payload.overlayGeojson, payload.overlayGeojsonCrs, False

    geojson = payload.geojson
    if not isinstance(geojson, dict) or geojson.get("type") != "FeatureCollection":
        return geojson, None, payload.geojsonCrs, False

    features = geojson.get("features")
    if not isinstance(features, list):
        return geojson, None, payload.geojsonCrs, False

    primary_features: list[Any] = []
    overlay_features: list[Any] = []
    for feature in features:
        if not isinstance(feature, dict):
            primary_features.append(feature)
            continue
        properties = feature.get("properties")
        source = ""
        if isinstance(properties, dict):
            source = str(properties.get("source", "")).strip().lower()

        if source == "threat-map-overlay":
            overlay_features.append(feature)
        else:
            primary_features.append(feature)

    if not overlay_features or not primary_features:
        return geojson, None, payload.geojsonCrs, False

    return (
        {"type": "FeatureCollection", "features": primary_features},
        {"type": "FeatureCollection", "features": overlay_features},
        payload.geojsonCrs,
        True,
    )


def validate_threat_map_request_payload(payload: ThreatMapJobCreateRequest, settings: Settings) -> dict:
    try:
        payload_len = len(json.dumps(payload.geojson).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ServiceValidationError("Invalid JSON payload") from exc

    if payload_len > settings.max_geojson_bytes:
        raise ServiceValidationError("GeoJSON payload too large")

    if payload.preset == ThreatMapPreset.ultra:
        raise ServiceValidationError("Preset 'ultra' is disabled on this server")

    if payload.width is not None and payload.width > settings.threat_map_max_size:
        raise ServiceValidationError(f"width must be <= {settings.threat_map_max_size}")

    if payload.height is not None and payload.height > settings.threat_map_max_size:
        raise ServiceValidationError(f"height must be <= {settings.threat_map_max_size}")

    if payload.width == 2048 or payload.height == 2048:
        raise ServiceValidationError("2048 output is not allowed on this server")

    if payload.preset == ThreatMapPreset.high and not settings.threat_map_allow_high_preset:
        raise ServiceValidationError("Preset 'high' is currently disabled; use 'balanced'")

    primary_geojson, overlay_geojson, overlay_crs, _ = _resolve_overlay_geojson_inputs(payload)

    _normalize_input_geometry(primary_geojson, payload.geojsonCrs, settings)

    if overlay_geojson is not None:
        _normalize_input_geometry(overlay_geojson, overlay_crs, settings)

    options = _resolve_render_options(payload, settings)
    return {
        "payloadBytes": payload_len,
        "options": options,
    }


def _resolve_render_options(payload: ThreatMapJobCreateRequest, settings: Settings) -> RenderOptions:
    if payload.preset == ThreatMapPreset.balanced:
        base_size = settings.threat_map_balanced_size
    elif payload.preset == ThreatMapPreset.high:
        base_size = settings.threat_map_high_size
    else:
        raise ServiceValidationError("Unsupported preset")

    width = payload.width or base_size
    height = payload.height or base_size
    if width > settings.threat_map_max_size or height > settings.threat_map_max_size:
        raise ServiceValidationError(f"Threat map max size is {settings.threat_map_max_size}x{settings.threat_map_max_size}")

    fps = payload.fps if payload.fps is not None else settings.threat_map_default_fps
    frame_duration = (
        payload.frameDurationSeconds
        if payload.frameDurationSeconds is not None
        else settings.threat_map_default_frame_duration_seconds
    )

    return RenderOptions(
        width=width,
        height=height,
        fps=fps,
        frame_duration_seconds=frame_duration,
        ffmpeg_preset=settings.threat_map_ffmpeg_preset,
        crf=settings.threat_map_ffmpeg_crf,
    )


def process_threat_map_job(
    *,
    settings: Settings,
    job_id: str,
    payload: ThreatMapJobCreateRequest,
    progress_callback: Callable[[JobProgressUpdate], None],
    is_cancelled: Callable[[], bool],
) -> dict:
    primary_geojson, overlay_geojson, overlay_crs, used_embedded_overlay = _resolve_overlay_geojson_inputs(payload)

    geometry = _normalize_input_geometry(primary_geojson, payload.geojsonCrs, settings)
    overlay_geometry: BaseGeometry | None = None
    if overlay_geojson is not None:
        overlay_geometry = _normalize_input_geometry(overlay_geojson, overlay_crs, settings)
    if used_embedded_overlay:
        logger.info("threat_map_overlay_extracted_from_geojson features_embedded=true")
    options = _resolve_render_options(payload, settings)

    if YEARS[0] != 1990 or YEARS[-1] != 2024:
        raise ThreatMapError("internal_invariant", "Unexpected configured year span")

    temp_root = settings.threat_map_temp_root
    temp_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=f"threat_map_{job_id}_", dir=str(temp_root)))

    output_mp4 = settings.outputs_dir / f"threat_map_{job_id}.mp4"
    output_zip = settings.outputs_dir / f"threat_map_{job_id}.zip"
    output_frames_tar_gz = settings.outputs_dir / f"threat_map_{job_id}_frames.tar.gz"
    output_mp4.parent.mkdir(parents=True, exist_ok=True)

    started_at = time.monotonic()
    warnings: list[str] = []
    logger.info(
        "threat_map_process_start job_id=%s preset=%s output_format=%s width=%s height=%s fps=%s frame_duration=%s",
        job_id,
        payload.preset,
        payload.outputFormat,
        options.width,
        options.height,
        options.fps,
        options.frame_duration_seconds,
    )

    try:
        if payload.outputFormat == "frames_tar_gz":
            logger.info("threat_map_pipeline_selected job_id=%s pipeline=frames_tar_gz", job_id)
            result = _build_frames_tar_gz_artifact(
                settings=settings,
                geometry=geometry,
                overlay_geometry=overlay_geometry,
                options=options,
                output_tar_gz=output_frames_tar_gz,
                started_at=started_at,
                progress_callback=progress_callback,
                is_cancelled=is_cancelled,
            )
            result["warnings"] = warnings
            logger.info(
                "threat_map_process_complete job_id=%s status=%s artifact_type=%s size_bytes=%s duration_seconds=%.2f",
                job_id,
                result.get("status"),
                result.get("artifactType"),
                result.get("sizeBytes"),
                time.monotonic() - started_at,
            )
            return result

        logger.info("threat_map_pipeline_selected job_id=%s pipeline=mp4", job_id)
        result = _encode_mp4(
            settings=settings,
            geometry=geometry,
            overlay_geometry=overlay_geometry,
            options=options,
            output_mp4=output_mp4,
            started_at=started_at,
            progress_callback=progress_callback,
            is_cancelled=is_cancelled,
        )
        result["warnings"] = warnings
        logger.info(
            "threat_map_process_complete job_id=%s status=%s artifact_type=%s size_bytes=%s duration_seconds=%.2f",
            job_id,
            result.get("status"),
            result.get("artifactType"),
            result.get("sizeBytes"),
            time.monotonic() - started_at,
        )
        return result
    except ThreatMapResourceLimitError as exc:
        if payload.outputFormat != "mp4":
            logger.warning(
                "threat_map_pipeline_resource_limit job_id=%s pipeline=%s reason_code=%s",
                job_id,
                payload.outputFormat,
                exc.code,
            )
            raise
        logger.warning("threat_map_mp4_fallback job_id=%s reason_code=%s", job_id, exc.code)
        warnings.append(f"mp4 encode failed, fallback to zip: {exc.code}")
        progress_callback(JobProgressUpdate(progress=95, message="MP4 fallback to zipped PNG frames"))
        result = _build_zip_fallback(
            settings=settings,
            geometry=geometry,
            overlay_geometry=overlay_geometry,
            options=options,
            output_zip=output_zip,
            started_at=started_at,
            progress_callback=progress_callback,
            is_cancelled=is_cancelled,
        )
        result["warnings"] = warnings
        result["fallbackReasonCode"] = exc.code
        logger.info(
            "threat_map_process_complete job_id=%s status=%s artifact_type=%s size_bytes=%s duration_seconds=%.2f",
            job_id,
            result.get("status"),
            result.get("artifactType"),
            result.get("sizeBytes"),
            time.monotonic() - started_at,
        )
        return result
    finally:
        _cleanup_workdir(workdir)


def _build_frames_tar_gz_artifact(
    *,
    settings: Settings,
    geometry: BaseGeometry,
    overlay_geometry: BaseGeometry | None,
    options: RenderOptions,
    output_tar_gz: Path,
    started_at: float,
    progress_callback: Callable[[JobProgressUpdate], None],
    is_cancelled: Callable[[], bool],
) -> dict:
    years_rendered = 0
    output_tar_gz.parent.mkdir(parents=True, exist_ok=True)
    logger.info("threat_map_frames_archive_start path=%s", output_tar_gz)

    metadata = {
        "version": 1,
        "artifactType": "frames_tar_gz",
        "width": options.width,
        "height": options.height,
        "fps": options.fps,
        "frameDurationSeconds": options.frame_duration_seconds,
        "yearStart": YEARS[0],
        "yearEnd": YEARS[-1],
        "yearsExpected": YEARS_EXPECTED,
        "framesPattern": "frames/frame_{year}.png",
    }

    with tarfile.open(output_tar_gz, mode="w:gz") as archive:
        metadata_bytes = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
        with tempfile.NamedTemporaryFile(suffix=".json") as tmp_manifest:
            tmp_manifest.write(metadata_bytes)
            tmp_manifest.flush()
            archive.add(tmp_manifest.name, arcname="manifest.json")

        for year in YEARS:
            _ensure_not_cancelled(is_cancelled)
            _ensure_request_timeout(settings, started_at)
            year_started = time.monotonic()

            frame = _render_year_frame_from_tiles(
                year=year,
                geometry=geometry,
                overlay_geometry=overlay_geometry,
                settings=settings,
                width=options.width,
                height=options.height,
                started_at=year_started,
                is_cancelled=is_cancelled,
            )
            year_elapsed = time.monotonic() - year_started
            logger.info("threat_map_year_frame_rendered year=%s mode=frames_tar_gz elapsed_seconds=%.2f", year, year_elapsed)

            image = Image.fromarray(frame, mode="RGB")
            with tempfile.NamedTemporaryFile(suffix=".png") as tmp_file:
                image.save(tmp_file.name, format="PNG")
                archive.add(tmp_file.name, arcname=f"frames/frame_{year}.png")

            years_rendered += 1
            progress_callback(
                JobProgressUpdate(
                    progress=5 + int((years_rendered / YEARS_EXPECTED) * 90),
                    message=f"Frame packaged for year {year}",
                    current_year=year,
                )
            )
            _ensure_year_timeout(settings, year_started)
            _ensure_memory_limit(settings)

    return {
        "status": "succeeded",
        "artifactType": "frames_tar_gz",
        "contentType": "application/gzip",
        "path": str(output_tar_gz),
        "sizeBytes": output_tar_gz.stat().st_size,
        "yearsRendered": years_rendered,
        "yearsExpected": YEARS_EXPECTED,
    }


def _encode_mp4(
    *,
    settings: Settings,
    geometry: BaseGeometry,
    overlay_geometry: BaseGeometry | None,
    options: RenderOptions,
    output_mp4: Path,
    started_at: float,
    progress_callback: Callable[[JobProgressUpdate], None],
    is_cancelled: Callable[[], bool],
) -> dict:
    logger.info("threat_map_mp4_encode_start output_path=%s", output_mp4)
    command = [
        "ffmpeg",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{options.width}x{options.height}",
        "-r",
        str(options.fps),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        options.ffmpeg_preset,
        "-crf",
        str(options.crf),
        "-pix_fmt",
        "yuv420p",
        str(output_mp4),
    ]

    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ThreatMapResourceLimitError("encode_failed", "ffmpeg not found") from exc

    frames_written = 0
    first_year_encoded: int | None = None
    last_year_encoded: int | None = None

    try:
        for idx, year in enumerate(YEARS):
            _ensure_not_cancelled(is_cancelled)
            _ensure_request_timeout(settings, started_at)
            year_started = time.monotonic()

            frame = _render_year_frame_from_tiles(
                year=year,
                geometry=geometry,
                overlay_geometry=overlay_geometry,
                settings=settings,
                width=options.width,
                height=options.height,
                started_at=year_started,
                is_cancelled=is_cancelled,
            )
            year_elapsed = time.monotonic() - year_started
            logger.info("threat_map_year_frame_rendered year=%s mode=mp4 elapsed_seconds=%.2f", year, year_elapsed)
            if proc.stdin is None:
                raise ThreatMapResourceLimitError("encode_failed", "ffmpeg stdin unavailable")
            try:
                proc.stdin.write(frame.tobytes())
            except BrokenPipeError as exc:
                raise ThreatMapResourceLimitError("resource_limit", "ffmpeg pipe closed during encode") from exc

            frames_written += 1
            if first_year_encoded is None:
                first_year_encoded = year
            last_year_encoded = year

            progress_callback(
                JobProgressUpdate(
                    progress=5 + int((frames_written / YEARS_EXPECTED) * 90),
                    message=f"Encoded year {year}",
                    current_year=year,
                )
            )
            _ensure_year_timeout(settings, year_started)
            _ensure_memory_limit(settings)

        if proc.stdin is not None:
            proc.stdin.close()
        stderr_bytes = proc.communicate(timeout=max(5, settings.threat_map_year_timeout_seconds))[1]
        if proc.returncode != 0:
            stderr_text = stderr_bytes.decode("utf-8", errors="ignore")
            raise ThreatMapResourceLimitError("encode_failed", f"ffmpeg failed: {stderr_text[-500:]}")
        logger.info("threat_map_mp4_encode_complete output_path=%s frames_written=%s", output_mp4, frames_written)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    if first_year_encoded != 1990:
        raise ThreatMapError("first_frame_invalid", "First encoded frame was not 1990")
    if last_year_encoded != 2024:
        raise ThreatMapError("last_frame_invalid", "Last encoded frame was not 2024")

    return {
        "status": "succeeded",
        "artifactType": "mp4",
        "contentType": "video/mp4",
        "path": str(output_mp4),
        "sizeBytes": output_mp4.stat().st_size,
        "yearsRendered": frames_written,
        "yearsExpected": YEARS_EXPECTED,
    }


def _build_zip_fallback(
    *,
    settings: Settings,
    geometry: BaseGeometry,
    overlay_geometry: BaseGeometry | None,
    options: RenderOptions,
    output_zip: Path,
    started_at: float,
    progress_callback: Callable[[JobProgressUpdate], None],
    is_cancelled: Callable[[], bool],
) -> dict:
    years_rendered = 0
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    logger.info("threat_map_zip_fallback_start path=%s", output_zip)
    with zipfile.ZipFile(output_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for idx, year in enumerate(YEARS):
            _ensure_not_cancelled(is_cancelled)
            _ensure_request_timeout(settings, started_at)
            year_started = time.monotonic()

            frame = _render_year_frame_from_tiles(
                year=year,
                geometry=geometry,
                overlay_geometry=overlay_geometry,
                settings=settings,
                width=options.width,
                height=options.height,
                started_at=year_started,
                is_cancelled=is_cancelled,
            )
            year_elapsed = time.monotonic() - year_started
            logger.info("threat_map_year_frame_rendered year=%s mode=zip_fallback elapsed_seconds=%.2f", year, year_elapsed)
            image = Image.fromarray(frame, mode="RGB")
            with tempfile.NamedTemporaryFile(suffix=".png") as tmp_file:
                image.save(tmp_file.name, format="PNG")
                archive.write(tmp_file.name, arcname=f"frame_{year}.png")

            years_rendered += 1
            progress_callback(
                JobProgressUpdate(
                    progress=5 + int((years_rendered / YEARS_EXPECTED) * 90),
                    message=f"Fallback frame year {year}",
                    current_year=year,
                )
            )
            _ensure_year_timeout(settings, year_started)
            _ensure_memory_limit(settings)

    return {
        "status": "partial_success",
        "artifactType": "zip",
        "contentType": "application/zip",
        "path": str(output_zip),
        "sizeBytes": output_zip.stat().st_size,
        "yearsRendered": years_rendered,
        "yearsExpected": YEARS_EXPECTED,
    }


def _resolve_required_tiles(geometry: BaseGeometry, settings: Settings) -> list[mercantile.Tile]:
    minx, miny, maxx, maxy = geometry.bounds
    zoom = settings.landcover_pmtiles_zoom
    tiles = list(mercantile.tiles(minx, miny, maxx, maxy, [zoom], truncate=True))
    if not tiles:
        raise ThreatMapError("tile_fetch_failed", "No AOI tiles resolved for frame rendering")
    logger.info("threat_map_tiles_resolved count=%s zoom=%s", len(tiles), zoom)
    return tiles


def _resolve_tiles_for_year(
    *,
    year: int,
    tiles: list[mercantile.Tile],
    settings: Settings,
    started_at: float,
    is_cancelled: Callable[[], bool],
) -> dict[mercantile.Tile, np.ndarray]:
    source_url = _resolve_year_url(settings, year)
    logger.info("threat_map_year_tiles_fetch_start year=%s source_url=%s tile_count=%s", year, source_url, len(tiles))
    reader = _build_pmtiles_reader(source_url)
    header = reader.header()

    decoded: dict[mercantile.Tile, np.ndarray] = {}

    concurrency = max(1, min(settings.threat_map_tile_fetch_concurrency, 2))
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(_fetch_single_tile_with_retry, reader, tile, settings, is_cancelled): tile for tile in tiles
        }
        for future in as_completed(futures):
            _ensure_not_cancelled(is_cancelled)
            _ensure_year_timeout(settings, started_at)
            tile = futures[future]
            payload = future.result()
            decoded[tile] = _decode_pmtiles_png_tile(payload, header["tile_compression"])

    if len(decoded) != len(tiles):
        raise ThreatMapError("tile_fetch_failed", f"Not all tiles resolved for year {year}")
    return decoded


def _fetch_single_tile_with_retry(reader, tile: mercantile.Tile, settings: Settings, is_cancelled: Callable[[], bool]) -> bytes:
    attempts = settings.threat_map_retry_max_attempts
    for attempt in range(1, attempts + 1):
        _ensure_not_cancelled(is_cancelled)
        try:
            payload = reader.get(tile.z, tile.x, tile.y)
            if payload is None:
                raise ThreatMapError("tile_fetch_failed", f"Missing tile z/x/y={tile.z}/{tile.x}/{tile.y}")
            return payload
        except Exception as exc:
            if attempt >= attempts:
                raise ThreatMapError("tile_fetch_failed", f"Failed tile z/x/y={tile.z}/{tile.x}/{tile.y}") from exc

            base = settings.threat_map_retry_base_delay_seconds
            cap = settings.threat_map_retry_max_delay_seconds
            delay = min(cap, base * (2 ** (attempt - 1)))
            jitter = random.uniform(0.0, min(0.25, delay * 0.25))
            logger.warning(
                "threat_map_tile_retry z=%s x=%s y=%s attempt=%s delay_seconds=%.2f",
                tile.z,
                tile.x,
                tile.y,
                attempt,
                delay + jitter,
            )
            time.sleep(delay + jitter)


def _render_year_frame_from_tiles(
    *,
    year: int,
    geometry: BaseGeometry,
    overlay_geometry: BaseGeometry | None,
    settings: Settings,
    width: int,
    height: int,
    started_at: float,
    is_cancelled: Callable[[], bool],
) -> np.ndarray:
    tiles = _resolve_required_tiles(geometry, settings)
    decoded_tiles = _resolve_tiles_for_year(
        year=year,
        tiles=tiles,
        settings=settings,
        started_at=started_at,
        is_cancelled=is_cancelled,
    )

    min_x = min(tile.x for tile in tiles)
    min_y = min(tile.y for tile in tiles)
    max_x = max(tile.x for tile in tiles)
    max_y = max(tile.y for tile in tiles)
    tile_span_x = (max_x - min_x) + 1
    tile_span_y = (max_y - min_y) + 1
    map_min_x_m = float("inf")
    map_min_y_m = float("inf")
    map_max_x_m = float("-inf")
    map_max_y_m = float("-inf")
    for tile in tiles:
        bounds_m = mercantile.xy_bounds(tile)
        map_min_x_m = min(map_min_x_m, float(bounds_m.left))
        map_min_y_m = min(map_min_y_m, float(bounds_m.bottom))
        map_max_x_m = max(map_max_x_m, float(bounds_m.right))
        map_max_y_m = max(map_max_y_m, float(bounds_m.top))

    legend_entries = _load_legend_entries(settings)
    class_color_map = _class_color_map(legend_entries)

    legend_h = max(72, min(140, int(height * 0.22)))
    map_h = max(1, height - legend_h)

    frame = np.zeros((height, width, 3), dtype=np.uint8)

    for tile in sorted(tiles, key=lambda item: (item.y, item.x)):
        _ensure_not_cancelled(is_cancelled)
        image = decoded_tiles.get(tile)
        if image is None:
            raise ThreatMapError("tile_fetch_failed", f"Decoded tile missing for year {year}: z/x/y={tile.z}/{tile.x}/{tile.y}")

        rgb = _to_rgb(image, class_color_map)
        rel_x = tile.x - min_x
        rel_y = tile.y - min_y

        x0 = int((rel_x * width) / tile_span_x)
        x1 = int(((rel_x + 1) * width) / tile_span_x)
        y0 = int((rel_y * map_h) / tile_span_y)
        y1 = int(((rel_y + 1) * map_h) / tile_span_y)
        if x1 <= x0:
            x1 = min(width, x0 + 1)
        if y1 <= y0:
            y1 = min(height, y0 + 1)

        tile_img = Image.fromarray(rgb, mode="RGB")
        tile_resized = np.asarray(tile_img.resize((x1 - x0, y1 - y0), resample=Image.Resampling.NEAREST), dtype=np.uint8)
        frame[y0:y1, x0:x1, :] = tile_resized

    _draw_overlay_geometry(
        frame,
        overlay_geometry=overlay_geometry,
        map_h=map_h,
        map_bounds_mercator=(map_min_x_m, map_min_y_m, map_max_x_m, map_max_y_m),
    )

    _draw_legend_band(frame, year=year, legend_entries=legend_entries, map_h=map_h)
    return frame


def _draw_overlay_geometry(
    frame: np.ndarray,
    *,
    overlay_geometry: BaseGeometry | None,
    map_h: int,
    map_bounds_mercator: tuple[float, float, float, float],
) -> None:
    if overlay_geometry is None:
        return

    map_min_x, map_min_y, map_max_x, map_max_y = map_bounds_mercator
    if not np.isfinite([map_min_x, map_min_y, map_max_x, map_max_y]).all():
        return
    if map_max_x <= map_min_x or map_max_y <= map_min_y:
        return

    overlay_mercator = _transform_geometry_to_crs(overlay_geometry, "EPSG:3857")
    if overlay_mercator.is_empty:
        return

    height, width, _ = frame.shape
    image = Image.fromarray(frame, mode="RGB")
    draw = ImageDraw.Draw(image)

    line_width = max(2, width // 320)
    line_color = (255, 242, 0)

    def _to_pixel(x_m: float, y_m: float) -> tuple[int, int]:
        px = int(round(((x_m - map_min_x) / (map_max_x - map_min_x)) * (width - 1)))
        py = int(round(((map_max_y - y_m) / (map_max_y - map_min_y)) * max(0, map_h - 1)))
        return px, py

    def _draw_ring(coords) -> None:
        points = [_to_pixel(float(x), float(y)) for x, y in coords]
        if len(points) >= 2:
            draw.line(points + [points[0]], fill=line_color, width=line_width)

    polygons: list[Polygon] = []
    if isinstance(overlay_mercator, Polygon):
        polygons = [overlay_mercator]
    elif isinstance(overlay_mercator, MultiPolygon):
        polygons = list(overlay_mercator.geoms)
    elif hasattr(overlay_mercator, "geoms"):
        polygons = [geom for geom in overlay_mercator.geoms if isinstance(geom, Polygon)]

    for polygon in polygons:
        _draw_ring(polygon.exterior.coords)
        for interior in polygon.interiors:
            _draw_ring(interior.coords)

    frame[:, :, :] = np.asarray(image, dtype=np.uint8)


def _normalize_input_geometry(geojson_obj: dict, source_crs: str, settings: Settings) -> BaseGeometry:
    geom = _extract_geometry(geojson_obj)
    source = source_crs.upper()
    if source not in {"EPSG:4326", "EPSG:3857"}:
        raise ServiceValidationError(f"Unsupported CRS: {source_crs}")

    if source == "EPSG:3857":
        geom = _transform_geometry_between_crs(geom, source_crs="EPSG:3857", target_crs="EPSG:4326")

    _validate_geometry(geom, settings)
    return geom


def _transform_geometry_between_crs(geom: BaseGeometry, *, source_crs: str, target_crs: str) -> BaseGeometry:
    if source_crs == target_crs:
        return geom
    try:
        transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
        transformed = shapely_transform(transformer.transform, geom)
    except Exception as exc:  # pragma: no cover
        raise ServiceValidationError(f"Failed to transform geometry from {source_crs} to {target_crs}") from exc
    if transformed.is_empty:
        raise ServiceValidationError("Transformed geometry is empty")
    return transformed


def _to_rgb(image: np.ndarray, class_color_map: dict[int, tuple[int, int, int]]) -> np.ndarray:
    # Some decoded arrays are read-only views; use a writable copy before in-place color remap.
    rgb = np.array(_to_rgb_raw(image), copy=True)
    if not class_color_map:
        return rgb

    class_values = _extract_pmtiles_class_values(image)
    if class_values is None:
        return rgb

    for class_code, color in class_color_map.items():
        mask = class_values == class_code
        if np.any(mask):
            rgb[mask] = np.asarray(color, dtype=np.uint8)
    return rgb


def _to_rgb_raw(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return np.repeat(image[:, :, np.newaxis], 3, axis=2)

    if image.ndim == 3:
        if image.shape[2] >= 3:
            return image[:, :, :3]
        if image.shape[2] == 2:
            return np.repeat(image[:, :, :1], 3, axis=2)

    raise ThreatMapError("tile_decode_failed", "Unsupported tile image dimensions")


def _draw_legend_band(frame: np.ndarray, *, year: int, legend_entries: list[dict[str, str]], map_h: int) -> None:
    height, width, _ = frame.shape
    if map_h >= height:
        return

    band_h = max(1, height - map_h)

    # Build a subtle vertical gradient so the legend reads as a dedicated panel.
    top_rgb = np.array([245, 248, 252], dtype=np.float32)
    bottom_rgb = np.array([232, 238, 245], dtype=np.float32)
    if band_h == 1:
        frame[map_h:height, :, :] = top_rgb.astype(np.uint8)
    else:
        for idx in range(band_h):
            blend = idx / (band_h - 1)
            row_rgb = ((1.0 - blend) * top_rgb) + (blend * bottom_rgb)
            frame[map_h + idx, :, :] = row_rgb.astype(np.uint8)

    # Crisp divider between map and legend band.
    frame[map_h : min(map_h + 2, height), :, :] = (120, 132, 146)

    image = Image.fromarray(frame, mode="RGB")
    draw = ImageDraw.Draw(image)

    outer_pad = max(8, width // 96)
    panel_x0 = outer_pad
    panel_y0 = map_h + max(6, band_h // 16)
    panel_x1 = width - outer_pad
    panel_y1 = height - max(6, band_h // 16)

    if panel_x1 - panel_x0 < 40 or panel_y1 - panel_y0 < 24:
        frame[:, :, :] = np.asarray(image, dtype=np.uint8)
        return

    draw.rounded_rectangle(
        [panel_x0, panel_y0, panel_x1, panel_y1],
        radius=max(8, min(14, band_h // 5)),
        fill=(252, 253, 255),
        outline=(174, 186, 201),
        width=1,
    )

    header_pad_x = panel_x0 + 12
    header_y = panel_y0 + 8
    draw.text((header_pad_x, header_y), "Landcover Legend", fill=(32, 42, 54))

    year_text = f"Year {year}"
    year_w, year_h = _measure_text(draw, year_text)
    badge_pad_x = 8
    badge_pad_y = 3
    badge_h = year_h + (badge_pad_y * 2)
    badge_w = year_w + (badge_pad_x * 2)
    badge_x1 = panel_x1 - 12
    badge_x0 = badge_x1 - badge_w
    badge_y0 = header_y - 1
    badge_y1 = badge_y0 + badge_h
    if badge_x0 > header_pad_x + 90:
        draw.rounded_rectangle(
            [badge_x0, badge_y0, badge_x1, badge_y1],
            radius=8,
            fill=(236, 242, 249),
            outline=(171, 183, 198),
            width=1,
        )
        draw.text((badge_x0 + badge_pad_x, badge_y0 + badge_pad_y), year_text, fill=(38, 50, 65))

    section_y = header_y + year_h + 7
    draw.line([(panel_x0 + 10, section_y), (panel_x1 - 10, section_y)], fill=(191, 201, 214), width=1)

    content_x0 = panel_x0 + 10
    content_x1 = panel_x1 - 10
    content_y0 = section_y + 8
    row_h = 20
    swatch = 12
    gutter_x = 8
    label_pad = 6

    valid_entries: list[tuple[str, str, tuple[int, int, int]]] = []
    for entry in legend_entries:
        color = _parse_hex_color(entry.get("color", ""))
        if color is None:
            continue
        class_code = entry.get("class_code", "").strip()
        label = entry.get("label", "").strip()
        if not class_code and not label:
            continue
        valid_entries.append((class_code, label, color))

    if not valid_entries:
        draw.text((content_x0, content_y0), "No legend entries available", fill=(111, 123, 138))
        frame[:, :, :] = np.asarray(image, dtype=np.uint8)
        return

    available_w = max(1, content_x1 - content_x0)
    col_target_w = 220
    col_count = max(1, min(4, available_w // col_target_w))
    col_count = min(col_count, len(valid_entries))
    col_count = max(1, col_count)

    col_w = max(120, (available_w - ((col_count - 1) * gutter_x)) // col_count)
    max_rows = max(1, (panel_y1 - content_y0 - 6) // row_h)
    capacity = max_rows * col_count
    hidden_count = max(0, len(valid_entries) - capacity)

    for idx, (class_code, label, color) in enumerate(valid_entries[:capacity]):
        row = idx // col_count
        col = idx % col_count
        item_x = content_x0 + (col * (col_w + gutter_x))
        item_y = content_y0 + (row * row_h)

        swatch_y0 = item_y + 4
        swatch_x0 = item_x
        swatch_x1 = swatch_x0 + swatch
        swatch_y1 = swatch_y0 + swatch
        draw.rounded_rectangle(
            [swatch_x0, swatch_y0, swatch_x1, swatch_y1],
            radius=2,
            fill=color,
            outline=(108, 119, 133),
            width=1,
        )

        if class_code and label:
            text = f"{class_code}: {label}"
        elif class_code:
            text = class_code
        else:
            text = label

        text_x = swatch_x1 + label_pad
        max_text_w = max(24, col_w - (swatch + label_pad + 2))
        clipped = _truncate_to_width(draw, text, max_text_w)
        draw.text((text_x, item_y + 3), clipped, fill=(41, 53, 67))

    if hidden_count > 0:
        more_text = f"+{hidden_count} more classes"
        more_w, more_h = _measure_text(draw, more_text)
        more_x = panel_x1 - 12 - more_w
        more_y = max(content_y0, panel_y1 - more_h - 6)
        draw.text((more_x, more_y), more_text, fill=(95, 108, 124))

    frame[:, :, :] = np.asarray(image, dtype=np.uint8)


def _measure_text(draw: ImageDraw.ImageDraw, value: str) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), value)
    return max(0, right - left), max(0, bottom - top)


def _truncate_to_width(draw: ImageDraw.ImageDraw, value: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if _measure_text(draw, value)[0] <= max_width:
        return value

    ellipsis = "..."
    if _measure_text(draw, ellipsis)[0] > max_width:
        return ""

    for cut in range(len(value), 0, -1):
        candidate = value[:cut].rstrip() + ellipsis
        if _measure_text(draw, candidate)[0] <= max_width:
            return candidate
    return ellipsis


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max(1, max_chars - 1)] + "..."


def _parse_hex_color(value: str) -> tuple[int, int, int] | None:
    text = value.strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return None


def _class_color_map(entries: list[dict[str, str]]) -> dict[int, tuple[int, int, int]]:
    output: dict[int, tuple[int, int, int]] = {}
    for entry in entries:
        class_code_raw = entry.get("class_code", "")
        color = _parse_hex_color(entry.get("color", ""))
        if color is None:
            continue
        try:
            class_code = int(float(class_code_raw))
        except ValueError:
            continue
        output[class_code] = color
    return output


@lru_cache(maxsize=4)
def _load_legend_entries_from_path(path_text: str) -> list[dict[str, str]]:
    path = Path(path_text)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []

    output: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        output.append(
            {
                "class_code": str(entry.get("class_code", "")),
                "label": str(entry.get("label", "")),
                "color": str(entry.get("color", "")),
            }
        )
    return output


@lru_cache(maxsize=4)
def _load_legend_entries_from_qgz_path(path_text: str) -> list[dict[str, str]]:
    path = Path(path_text)
    if not path.exists() or path.suffix.lower() != ".qgz":
        return []

    try:
        with zipfile.ZipFile(path, "r") as archive:
            qgs_members = [name for name in archive.namelist() if name.lower().endswith(".qgs")]
            if not qgs_members:
                return []
            qgs_xml = archive.read(qgs_members[0])
        root = ET.fromstring(qgs_xml)
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
        return []

    entries: list[dict[str, str]] = []
    seen_class_codes: set[str] = set()

    for node in root.findall(".//rasterrenderer/colorPalette/paletteEntry"):
        class_code = str(node.attrib.get("value", "")).strip()
        color = str(node.attrib.get("color", "")).strip()
        label = str(node.attrib.get("label", "")).strip()
        if not class_code or not color or class_code in seen_class_codes:
            continue
        seen_class_codes.add(class_code)
        entries.append(
            {
                "class_code": class_code,
                "label": label,
                "color": color,
            }
        )

    return entries


def _load_legend_entries(settings: Settings) -> list[dict[str, str]]:
    manifest_entries = _load_legend_entries_from_path(str(settings.threat_map_legend_manifest_path))
    if manifest_entries:
        return manifest_entries

    # Fallback for local workflows where the extracted manifest file is absent.
    repo_root = Path(__file__).resolve().parents[2]
    fallback_candidates = [
        Path("mekar_raya.qgz"),
        repo_root / "mekar_raya.qgz",
    ]
    for candidate in fallback_candidates:
        qgz_entries = _load_legend_entries_from_qgz_path(str(candidate))
        if qgz_entries:
            logger.info("threat_map_legend_fallback_loaded source=%s count=%s", candidate, len(qgz_entries))
            return qgz_entries

    return []


def _ensure_request_timeout(settings: Settings, started_at: float) -> None:
    if time.monotonic() - started_at > settings.threat_map_request_timeout_seconds:
        raise ThreatMapResourceLimitError("request_timeout", "Threat map request timeout")


def _ensure_year_timeout(settings: Settings, started_at: float) -> None:
    if time.monotonic() - started_at > settings.threat_map_year_timeout_seconds:
        raise ThreatMapResourceLimitError("year_timeout", "Threat map year timeout")


def _ensure_not_cancelled(is_cancelled: Callable[[], bool]) -> None:
    if is_cancelled():
        raise ThreatMapError("cancelled", "Threat map job cancelled")


def _ensure_memory_limit(settings: Settings) -> None:
    rss = _current_rss_mb()
    if rss > settings.threat_map_memory_rss_limit_mb:
        raise ThreatMapResourceLimitError("resource_limit", f"RSS memory limit exceeded: {rss} MB")


def _current_rss_mb() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    value = int(usage.ru_maxrss)
    # macOS reports bytes; Linux reports KB.
    if value > 10_000_000:
        return int(value / (1024 * 1024))
    return int(value / 1024)


def _cleanup_workdir(workdir: Path) -> None:
    if not workdir.exists():
        return
    for root, _, files in os.walk(workdir, topdown=False):
        for name in files:
            try:
                Path(root, name).unlink(missing_ok=True)
            except OSError:
                continue
        try:
            Path(root).rmdir()
        except OSError:
            continue


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
