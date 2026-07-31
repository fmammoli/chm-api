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
from shapely.geometry import MultiPoint, MultiPolygon, Point, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform, unary_union

from app.config import Settings
from app.models import ThreatMapJobCreateRequest, ThreatMapOverlayLayer, ThreatMapPreset
from app.services.chm_service import ServiceValidationError, _extract_geometry, _transform_geometry_to_crs, _validate_geometry
from app.services.landcover_stats_service import (
    _build_pmtiles_reader,
    _decode_pmtiles_png_tile,
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


@dataclass(frozen=True)
class OverlayRenderStyle:
    stroke_color: tuple[int, int, int]
    stroke_width: int
    fill_color: tuple[int, int, int]
    fill_opacity: float
    marker_color: tuple[int, int, int]
    marker_outline_color: tuple[int, int, int]
    marker_size: int
    label_color: tuple[int, int, int]
    label_bg_color: tuple[int, int, int]


@dataclass(frozen=True)
class OverlayRenderLayer:
    id: str
    label: str
    kind: str
    geometry: BaseGeometry
    style: OverlayRenderStyle
    show_in_legend: bool
    legend_order: int


class JobProgressUpdate(BaseModel):
    progress: int = Field(ge=0, le=100)
    message: str | None = None
    current_year: int | None = None


def _resolve_overlay_geojson_inputs(
    payload: ThreatMapJobCreateRequest,
) -> tuple[dict[str, Any], dict[str, Any] | None, str, dict[str, Any] | None, str | None, str, bool]:
    """Return normalized AOI/overlay inputs extracted from frontend payload.

    If `overlayGeojson` is provided explicitly, it is used as-is.
    If `overlayPointGeojson` is provided explicitly, it is used as-is.
    Otherwise, frontend payloads that mix extra layers into `geojson` are split
    into AOI polygon features, overlay polygon features, and point overlays.
    """

    overlay_point_geojson = payload.overlayPointGeojson
    overlay_point_name = payload.overlayPointName
    overlay_point_crs = payload.overlayPointCrs

    if payload.overlayGeojson is not None:
        return (
            payload.geojson,
            payload.overlayGeojson,
            payload.overlayGeojsonCrs,
            overlay_point_geojson,
            overlay_point_name,
            overlay_point_crs,
            False,
        )

    geojson = payload.geojson
    if not isinstance(geojson, dict) or geojson.get("type") != "FeatureCollection":
        return (
            geojson,
            None,
            payload.geojsonCrs,
            overlay_point_geojson,
            overlay_point_name,
            overlay_point_crs,
            False,
        )

    features = geojson.get("features")
    if not isinstance(features, list):
        return (
            geojson,
            None,
            payload.geojsonCrs,
            overlay_point_geojson,
            overlay_point_name,
            overlay_point_crs,
            False,
        )

    primary_features: list[Any] = []
    overlay_features: list[Any] = []
    point_features: list[Any] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        source = ""
        if isinstance(properties, dict):
            source = str(properties.get("source", "")).strip().lower()

        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        if not isinstance(geometry, dict):
            continue

        geom_type = ""
        geom_type = str(geometry.get("type", "")).strip()
        is_polygon = geom_type in {"Polygon", "MultiPolygon"}
        is_point = geom_type in {"Point", "MultiPoint"}

        if source == "threat-map-overlay" and is_polygon:
            overlay_features.append(feature)
        elif source in {"threat-map-point", "overlay-point", "threat-map-label-point"} or is_point:
            point_features.append(feature)
            if overlay_point_name is None and isinstance(properties, dict):
                candidate_name = str(properties.get("name", "")).strip()
                if candidate_name:
                    overlay_point_name = candidate_name
        else:
            if is_polygon:
                primary_features.append(feature)

    if point_features and overlay_point_geojson is None:
        overlay_point_geojson = {"type": "FeatureCollection", "features": point_features}
        # Embedded point features share the same CRS as the parent geojson payload.
        overlay_point_crs = payload.geojsonCrs

    if not primary_features:
        if overlay_features or point_features:
            raise ServiceValidationError(
                "No AOI polygon found in geojson. Send AOI as Polygon/MultiPolygon and overlays via overlayLayers."
            )
        return (
            geojson,
            payload.overlayGeojson,
            payload.overlayGeojsonCrs,
            overlay_point_geojson,
            overlay_point_name,
            overlay_point_crs,
            bool(overlay_features or point_features),
        )

    overlay_geojson = {"type": "FeatureCollection", "features": overlay_features} if overlay_features else None

    return (
        {"type": "FeatureCollection", "features": primary_features},
        overlay_geojson,
        payload.geojsonCrs,
        overlay_point_geojson,
        overlay_point_name,
        overlay_point_crs,
        bool(overlay_features or point_features),
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

    (
        primary_geojson,
        overlay_geojson,
        overlay_crs,
        overlay_point_geojson,
        _overlay_point_name,
        overlay_point_crs,
        _,
    ) = _resolve_overlay_geojson_inputs(payload)

    _normalize_input_geometry(primary_geojson, payload.geojsonCrs, settings)

    if overlay_geojson is not None:
        _normalize_input_geometry(overlay_geojson, overlay_crs, settings)

    if overlay_point_geojson is not None:
        _normalize_overlay_point_geometry(overlay_point_geojson, overlay_point_crs, settings)

    _build_overlay_layers(
        payload=payload,
        settings=settings,
        legacy_overlay_geojson=overlay_geojson,
        legacy_overlay_crs=overlay_crs,
        legacy_overlay_point_geojson=overlay_point_geojson,
        legacy_overlay_point_name=_overlay_point_name,
        legacy_overlay_point_crs=overlay_point_crs,
    )

    options = _resolve_render_options(payload, settings)
    return {
        "payloadBytes": payload_len,
        "options": options,
    }


def _resolve_output_format(requested_format: str, settings: Settings) -> str:
    if requested_format == "mp4":
        logger.info("threat_map_frames_only_requested requested_format=mp4 resolved_format=frames_tar_gz")
        return "frames_tar_gz"
    return requested_format


def _resolve_render_options(payload: ThreatMapJobCreateRequest, settings: Settings) -> RenderOptions:
    if settings.threat_map_low_resource_mode:
        base_size = settings.threat_map_low_resource_width
        max_size = settings.threat_map_low_resource_max_size
        default_fps = settings.threat_map_low_resource_fps
        default_frame_duration = settings.threat_map_low_resource_frame_duration_seconds
    else:
        if payload.preset == ThreatMapPreset.balanced:
            base_size = settings.threat_map_balanced_size
        elif payload.preset == ThreatMapPreset.high:
            base_size = settings.threat_map_high_size
        else:
            raise ServiceValidationError("Unsupported preset")
        max_size = settings.threat_map_max_size
        default_fps = settings.threat_map_default_fps
        default_frame_duration = settings.threat_map_default_frame_duration_seconds

    width = payload.width or base_size
    height = payload.height or base_size
    if width > max_size or height > max_size:
        raise ServiceValidationError(f"Threat map max size is {max_size}x{max_size}")

    fps = payload.fps if payload.fps is not None else default_fps
    frame_duration = (
        payload.frameDurationSeconds
        if payload.frameDurationSeconds is not None
        else default_frame_duration
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
    (
        primary_geojson,
        overlay_geojson,
        overlay_crs,
        overlay_point_geojson,
        overlay_point_name,
        overlay_point_crs,
        used_embedded_overlay,
    ) = _resolve_overlay_geojson_inputs(payload)

    geometry = _normalize_input_geometry(primary_geojson, payload.geojsonCrs, settings)
    overlay_layers = _build_overlay_layers(
        payload=payload,
        settings=settings,
        legacy_overlay_geojson=overlay_geojson,
        legacy_overlay_crs=overlay_crs,
        legacy_overlay_point_geojson=overlay_point_geojson,
        legacy_overlay_point_name=overlay_point_name,
        legacy_overlay_point_crs=overlay_point_crs,
    )
    if used_embedded_overlay:
        logger.info("threat_map_overlay_extracted_from_geojson features_embedded=true")
    options = _resolve_render_options(payload, settings)
    output_format = _resolve_output_format(payload.outputFormat, settings)

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
        output_format,
        options.width,
        options.height,
        options.fps,
        options.frame_duration_seconds,
    )

    try:
        if output_format == "frames_tar_gz":
            logger.info("threat_map_pipeline_selected job_id=%s pipeline=frames_tar_gz", job_id)
            result = _build_frames_tar_gz_artifact(
                settings=settings,
                geometry=geometry,
                overlay_layers=overlay_layers,
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
            overlay_layers=overlay_layers,
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
        if output_format != "mp4":
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
            overlay_layers=overlay_layers,
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
    overlay_layers: list[OverlayRenderLayer],
    options: RenderOptions,
    output_tar_gz: Path,
    started_at: float,
    progress_callback: Callable[[JobProgressUpdate], None],
    is_cancelled: Callable[[], bool],
) -> dict:
    years_rendered = 0
    output_tar_gz.parent.mkdir(parents=True, exist_ok=True)
    logger.info("threat_map_frames_archive_start path=%s", output_tar_gz)

    legend_entries = _build_overlay_legend_entries(overlay_layers) + _load_legend_entries(settings)
    legend_height = _compute_legend_band_height(width=options.width, legend_entries=legend_entries)
    frame_height = options.height + legend_height

    metadata = {
        "version": 1,
        "artifactType": "frames_tar_gz",
        "width": options.width,
        "height": frame_height,
        "mapHeight": options.height,
        "legendHeight": legend_height,
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
                overlay_layers=overlay_layers,
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
    overlay_layers: list[OverlayRenderLayer],
    options: RenderOptions,
    output_mp4: Path,
    started_at: float,
    progress_callback: Callable[[JobProgressUpdate], None],
    is_cancelled: Callable[[], bool],
) -> dict:
    logger.info("threat_map_mp4_encode_start output_path=%s", output_mp4)
    legend_entries = _build_overlay_legend_entries(overlay_layers) + _load_legend_entries(settings)
    frame_h = options.height + _compute_legend_band_height(width=options.width, legend_entries=legend_entries)

    command = [
        "ffmpeg",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{options.width}x{frame_h}",
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
                overlay_layers=overlay_layers,
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
    overlay_layers: list[OverlayRenderLayer],
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
                overlay_layers=overlay_layers,
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
    zoom = settings.threat_map_low_resource_zoom if settings.threat_map_low_resource_mode else settings.landcover_pmtiles_zoom
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
    source_url = _resolve_threat_map_year_url(settings, year)
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


def _resolve_threat_map_year_url(settings: Settings, year: int) -> str:
    if year == 1990 and settings.threat_map_landcover_year_1990_url:
        logger.info("threat_map_year_url_override year=%s url=%s", year, settings.threat_map_landcover_year_1990_url)
        return settings.threat_map_landcover_year_1990_url
    if year == 2024 and settings.threat_map_landcover_year_2024_url:
        logger.info("threat_map_year_url_override year=%s url=%s", year, settings.threat_map_landcover_year_2024_url)
        return settings.threat_map_landcover_year_2024_url

    resolved = settings.threat_map_landcover_url_template.format(
        base_url=settings.threat_map_landcover_base_url.rstrip("/"),
        year=year,
    )
    logger.info("threat_map_year_url_template year=%s url=%s", year, resolved)
    return resolved


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
    overlay_layers: list[OverlayRenderLayer],
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

    geom_mercator = _transform_geometry_to_crs(geometry, "EPSG:3857")
    map_min_x_m, map_min_y_m, map_max_x_m, map_max_y_m = geom_mercator.bounds
    if map_max_x_m <= map_min_x_m or map_max_y_m <= map_min_y_m:
        raise ThreatMapError("tile_fetch_failed", "AOI bounds are invalid for frame rendering")

    legend_entries = _build_overlay_legend_entries(overlay_layers) + _load_legend_entries(settings)

    # Preserve full requested map size; grow the final frame vertically for legend rows.
    map_h = max(1, height)
    legend_h = _compute_legend_band_height(width=width, legend_entries=legend_entries)
    frame_h = map_h + legend_h

    frame = np.zeros((frame_h, width, 3), dtype=np.uint8)

    for tile in sorted(tiles, key=lambda item: (item.y, item.x)):
        _ensure_not_cancelled(is_cancelled)
        image = decoded_tiles.get(tile)
        if image is None:
            raise ThreatMapError("tile_fetch_failed", f"Decoded tile missing for year {year}: z/x/y={tile.z}/{tile.x}/{tile.y}")

        rgb = _to_rgb(image)
        tile_bounds = mercantile.xy_bounds(tile)
        tile_left = float(tile_bounds.left)
        tile_right = float(tile_bounds.right)
        tile_bottom = float(tile_bounds.bottom)
        tile_top = float(tile_bounds.top)

        # Clip each tile to AOI bounds to avoid rendering area outside requested extent.
        clip_left = max(tile_left, map_min_x_m)
        clip_right = min(tile_right, map_max_x_m)
        clip_bottom = max(tile_bottom, map_min_y_m)
        clip_top = min(tile_top, map_max_y_m)
        if clip_right <= clip_left or clip_top <= clip_bottom:
            continue

        x0 = int(round(((clip_left - map_min_x_m) / (map_max_x_m - map_min_x_m)) * width))
        x1 = int(round(((clip_right - map_min_x_m) / (map_max_x_m - map_min_x_m)) * width))
        y0 = int(round(((map_max_y_m - clip_top) / (map_max_y_m - map_min_y_m)) * map_h))
        y1 = int(round(((map_max_y_m - clip_bottom) / (map_max_y_m - map_min_y_m)) * map_h))
        x0 = max(0, min(width, x0))
        x1 = max(0, min(width, x1))
        y0 = max(0, min(map_h, y0))
        y1 = max(0, min(map_h, y1))
        if x1 <= x0:
            x1 = min(width, x0 + 1)
        if y1 <= y0:
            y1 = min(map_h, y0 + 1)

        tile_h_px, tile_w_px = rgb.shape[0], rgb.shape[1]
        tile_span_x = tile_right - tile_left
        tile_span_y = tile_top - tile_bottom
        if tile_span_x <= 0.0 or tile_span_y <= 0.0:
            continue

        # Crop the source tile to the clipped mercator span first; resizing the full
        # tile into a clipped strip causes visible horizontal/vertical squashing.
        src_x0 = int(np.floor(((clip_left - tile_left) / tile_span_x) * tile_w_px))
        src_x1 = int(np.ceil(((clip_right - tile_left) / tile_span_x) * tile_w_px))
        src_y0 = int(np.floor(((tile_top - clip_top) / tile_span_y) * tile_h_px))
        src_y1 = int(np.ceil(((tile_top - clip_bottom) / tile_span_y) * tile_h_px))

        src_x0 = max(0, min(tile_w_px, src_x0))
        src_x1 = max(0, min(tile_w_px, src_x1))
        src_y0 = max(0, min(tile_h_px, src_y0))
        src_y1 = max(0, min(tile_h_px, src_y1))
        if src_x1 <= src_x0:
            src_x1 = min(tile_w_px, src_x0 + 1)
        if src_y1 <= src_y0:
            src_y1 = min(tile_h_px, src_y0 + 1)

        src_rgb = rgb[src_y0:src_y1, src_x0:src_x1, :]
        tile_img = Image.fromarray(src_rgb, mode="RGB")
        tile_resized = np.asarray(tile_img.resize((x1 - x0, y1 - y0), resample=Image.Resampling.NEAREST), dtype=np.uint8)
        frame[y0:y1, x0:x1, :] = tile_resized

    _draw_overlay_layers(
        frame,
        overlay_layers=overlay_layers,
        map_h=map_h,
        map_bounds_mercator=(map_min_x_m, map_min_y_m, map_max_x_m, map_max_y_m),
    )

    _draw_legend_band(frame, year=year, legend_entries=legend_entries, map_h=map_h)
    return frame


def _build_overlay_layers(
    *,
    payload: ThreatMapJobCreateRequest,
    settings: Settings,
    legacy_overlay_geojson: dict[str, Any] | None,
    legacy_overlay_crs: str,
    legacy_overlay_point_geojson: dict[str, Any] | None,
    legacy_overlay_point_name: str | None,
    legacy_overlay_point_crs: str,
) -> list[OverlayRenderLayer]:
    layers: list[OverlayRenderLayer] = []

    if legacy_overlay_geojson is not None:
        geometry = _normalize_input_geometry(legacy_overlay_geojson, legacy_overlay_crs, settings)
        layers.append(
            OverlayRenderLayer(
                id="legacy-overlay",
                label="Overlay",
                kind="polygon",
                geometry=geometry,
                style=_resolve_overlay_style(None, kind="polygon"),
                show_in_legend=False,
                legend_order=1000,
            )
        )

    if legacy_overlay_point_geojson is not None:
        geometry = _normalize_overlay_point_geometry(legacy_overlay_point_geojson, legacy_overlay_point_crs, settings)
        layers.append(
            OverlayRenderLayer(
                id="legacy-point",
                label=_resolve_overlay_point_name(legacy_overlay_point_name),
                kind="point",
                geometry=geometry,
                style=_resolve_overlay_style(None, kind="point"),
                show_in_legend=False,
                legend_order=1001,
            )
        )

    if payload.overlayLayers:
        for raw_layer in payload.overlayLayers:
            layer = ThreatMapOverlayLayer.model_validate(raw_layer)
            polygon_geom, point_geom = _extract_overlay_layer_geometry_parts(layer.geojson)
            if polygon_geom is None and point_geom is None:
                raise ServiceValidationError("Overlay layer geometry must include point or polygon features")

            label = layer.label.strip() or layer.id
            show_in_legend = layer.showInLegend

            if polygon_geom is not None:
                polygon_input = {
                    "type": "Feature",
                    "geometry": mapping(polygon_geom),
                    "properties": {},
                }
                geometry = _normalize_input_geometry(polygon_input, layer.geojsonCrs, settings)
                layers.append(
                    OverlayRenderLayer(
                        id=f"{layer.id}::polygon",
                        label=label,
                        kind="polygon",
                        geometry=geometry,
                        style=_resolve_overlay_style(layer, kind="polygon"),
                        show_in_legend=show_in_legend,
                        legend_order=layer.legendOrder,
                    )
                )
                show_in_legend = False

            if point_geom is not None:
                point_input = {
                    "type": "Feature",
                    "geometry": mapping(point_geom),
                    "properties": {},
                }
                geometry = _normalize_overlay_point_geometry(point_input, layer.geojsonCrs, settings)
                layers.append(
                    OverlayRenderLayer(
                        id=f"{layer.id}::point",
                        label=label,
                        kind="point",
                        geometry=geometry,
                        style=_resolve_overlay_style(layer, kind="point"),
                        show_in_legend=show_in_legend,
                        legend_order=layer.legendOrder,
                    )
                )

    return layers


def _extract_overlay_layer_geometry_parts(geojson_obj: dict[str, Any]) -> tuple[BaseGeometry | None, BaseGeometry | None]:
    polygon_parts: list[BaseGeometry] = []
    point_parts: list[BaseGeometry] = []

    def _add_geometry(geometry_obj: dict[str, Any]) -> None:
        sanitized = _sanitize_overlay_geometry_object(geometry_obj)
        if sanitized is None:
            return
        try:
            geom = _repair_overlay_geometry(shape(sanitized))
        except Exception:
            return
        polygon_geom, point_geom = _split_overlay_geometry_parts(geom)
        if polygon_geom is not None:
            polygon_parts.append(polygon_geom)
        if point_geom is not None:
            point_parts.append(point_geom)

    if not isinstance(geojson_obj, dict):
        return None, None

    geo_type = str(geojson_obj.get("type", "")).strip()
    if geo_type == "FeatureCollection":
        features = geojson_obj.get("features", [])
        for feature in features:
            if not isinstance(feature, dict):
                continue
            geometry = feature.get("geometry")
            if not isinstance(geometry, dict):
                continue
            _add_geometry(geometry)
    elif geo_type == "Feature":
        geometry = geojson_obj.get("geometry")
        if isinstance(geometry, dict):
            _add_geometry(geometry)
    else:
        geometry = geojson_obj.get("geometry") if isinstance(geojson_obj.get("geometry"), dict) else geojson_obj
        if isinstance(geometry, dict):
            _add_geometry(geometry)

    polygon_geom: BaseGeometry | None = unary_union(polygon_parts) if polygon_parts else None
    point_geom: BaseGeometry | None = unary_union(point_parts) if point_parts else None
    return polygon_geom, point_geom


def _sanitize_overlay_geometry_object(geometry_obj: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(geometry_obj, dict):
        return None

    geom_type = str(geometry_obj.get("type", "")).strip()
    coordinates = geometry_obj.get("coordinates")

    if geom_type == "Point":
        point = _sanitize_overlay_coord_pair(coordinates)
        return {"type": "Point", "coordinates": point} if point is not None else None

    if geom_type == "MultiPoint":
        if not isinstance(coordinates, list):
            return None
        points = [point for point in (_sanitize_overlay_coord_pair(item) for item in coordinates) if point is not None]
        return {"type": "MultiPoint", "coordinates": points} if points else None

    if geom_type == "Polygon":
        rings = _sanitize_overlay_polygon_rings(coordinates)
        return {"type": "Polygon", "coordinates": rings} if rings else None

    if geom_type == "MultiPolygon":
        if not isinstance(coordinates, list):
            return None
        polygons: list[list[list[list[float]]]] = []
        for polygon_coords in coordinates:
            rings = _sanitize_overlay_polygon_rings(polygon_coords)
            if rings:
                polygons.append(rings)
        return {"type": "MultiPolygon", "coordinates": polygons} if polygons else None

    if geom_type == "GeometryCollection":
        raw_geoms = geometry_obj.get("geometries")
        if not isinstance(raw_geoms, list):
            return None
        geoms = [geom for geom in (_sanitize_overlay_geometry_object(item) for item in raw_geoms) if geom is not None]
        return {"type": "GeometryCollection", "geometries": geoms} if geoms else None

    return None


def _sanitize_overlay_polygon_rings(raw_rings: Any) -> list[list[list[float]]]:
    if not isinstance(raw_rings, list):
        return []

    sanitized_rings: list[list[list[float]]] = []
    for raw_ring in raw_rings:
        ring = _sanitize_overlay_linear_ring(raw_ring)
        if ring is not None:
            sanitized_rings.append(ring)

    if not sanitized_rings:
        return []
    return sanitized_rings


def _sanitize_overlay_linear_ring(raw_ring: Any) -> list[list[float]] | None:
    if not isinstance(raw_ring, list):
        return None

    points: list[list[float]] = []
    for raw_point in raw_ring:
        point = _sanitize_overlay_coord_pair(raw_point)
        if point is None:
            continue
        if points and points[-1] == point:
            continue
        points.append(point)

    if len(points) < 3:
        return None
    if points[0] != points[-1]:
        points.append(points[0])
    if len(points) < 4:
        return None
    return points


def _sanitize_overlay_coord_pair(raw_coord: Any) -> list[float] | None:
    if not isinstance(raw_coord, (list, tuple)) or len(raw_coord) < 2:
        return None
    x = _to_finite_float(raw_coord[0])
    y = _to_finite_float(raw_coord[1])
    if x is None or y is None:
        return None
    return [x, y]


def _to_finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not np.isfinite(number):
        return None
    return number


def _split_overlay_geometry_parts(geom: BaseGeometry) -> tuple[BaseGeometry | None, BaseGeometry | None]:
    polygon_parts: list[BaseGeometry] = []
    point_parts: list[BaseGeometry] = []

    def _collect(value: BaseGeometry) -> None:
        if value.is_empty:
            return
        if isinstance(value, Polygon):
            polygon_parts.append(value)
            return
        if isinstance(value, MultiPolygon):
            polygon_parts.extend(part for part in value.geoms if not part.is_empty)
            return
        if isinstance(value, Point):
            point_parts.append(value)
            return
        if isinstance(value, MultiPoint):
            point_parts.extend(part for part in value.geoms if not part.is_empty)
            return
        if hasattr(value, "geoms"):
            for part in value.geoms:
                if isinstance(part, BaseGeometry):
                    _collect(part)

    _collect(geom)

    polygon_geom: BaseGeometry | None = unary_union(polygon_parts) if polygon_parts else None
    point_geom: BaseGeometry | None = unary_union(point_parts) if point_parts else None
    return polygon_geom, point_geom


def _repair_overlay_geometry(geom: BaseGeometry) -> BaseGeometry:
    if geom.is_empty:
        return geom

    try:
        repaired = geom.buffer(0)
    except Exception:
        return geom

    if repaired.is_empty:
        return geom
    return repaired


def _resolve_overlay_geometry_kind(geom: BaseGeometry) -> str:
    if isinstance(geom, (Point, MultiPoint)):
        return "point"
    if isinstance(geom, (Polygon, MultiPolygon)):
        return "polygon"

    if hasattr(geom, "geoms"):
        parts = list(getattr(geom, "geoms", []))
        point_like = any(isinstance(part, (Point, MultiPoint)) for part in parts)
        polygon_like = any(isinstance(part, (Polygon, MultiPolygon)) for part in parts)
        if point_like and not polygon_like:
            return "point"
        if polygon_like and not point_like:
            return "polygon"

    raise ServiceValidationError("Overlay layer geometry must be point or polygon")


def _resolve_overlay_style(layer: ThreatMapOverlayLayer | None, *, kind: str) -> OverlayRenderStyle:
    stroke_default = (255, 242, 0) if kind == "polygon" else (255, 78, 78)
    fill_default = stroke_default
    marker_default = (255, 78, 78)

    style = layer.style if layer is not None else None

    stroke = _resolve_color_value(style.strokeColor if style else None, stroke_default)
    fill = _resolve_color_value(style.fillColor if style else None, fill_default)
    marker = _resolve_color_value(style.markerColor if style else None, marker_default)
    marker_outline = _resolve_color_value(style.markerOutlineColor if style else None, (255, 255, 255))
    label_color = _resolve_color_value(style.labelColor if style else None, (33, 39, 48))
    label_bg = _resolve_color_value(style.labelBgColor if style else None, (255, 255, 255))

    return OverlayRenderStyle(
        stroke_color=stroke,
        stroke_width=(style.strokeWidth if style and style.strokeWidth is not None else 2),
        fill_color=fill,
        fill_opacity=(style.fillOpacity if style and style.fillOpacity is not None else 0.15),
        marker_color=marker,
        marker_outline_color=marker_outline,
        marker_size=(style.markerSize if style and style.markerSize is not None else 8),
        label_color=label_color,
        label_bg_color=label_bg,
    )


def _resolve_color_value(raw: str | None, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if raw is None:
        return default
    parsed = _parse_hex_color(raw)
    if parsed is None:
        raise ServiceValidationError(f"Invalid hex color: {raw}")
    return parsed


def _build_overlay_legend_entries(overlay_layers: list[OverlayRenderLayer]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    sorted_layers = sorted(overlay_layers, key=lambda item: (item.legend_order, item.label.lower(), item.id))
    for layer in sorted_layers:
        if not layer.show_in_legend:
            continue
        color = layer.style.marker_color if layer.kind == "point" else layer.style.stroke_color
        class_code = "PT" if layer.kind == "point" else "OVL"
        entries.append(
            {
                "class_code": class_code,
                "label": layer.label,
                "color": f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}",
            }
        )
    return entries


def _draw_overlay_layers(
    frame: np.ndarray,
    *,
    overlay_layers: list[OverlayRenderLayer],
    map_h: int,
    map_bounds_mercator: tuple[float, float, float, float],
) -> None:
    if not overlay_layers:
        return

    map_min_x, map_min_y, map_max_x, map_max_y = map_bounds_mercator
    if not np.isfinite([map_min_x, map_min_y, map_max_x, map_max_y]).all():
        return
    if map_max_x <= map_min_x or map_max_y <= map_min_y:
        return

    height, width, _ = frame.shape
    base_image = Image.fromarray(frame, mode="RGB")

    def _to_pixel(x_m: float, y_m: float) -> tuple[int, int]:
        px = int(round(((x_m - map_min_x) / (map_max_x - map_min_x)) * (width - 1)))
        py = int(round(((map_max_y - y_m) / (map_max_y - map_min_y)) * max(0, map_h - 1)))
        return px, py

    overlay_rgba = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay_rgba, "RGBA")
    label_draw = ImageDraw.Draw(base_image)

    sorted_layers = sorted(overlay_layers, key=lambda item: (item.legend_order, item.id))
    for layer in sorted_layers:
        if layer.kind == "polygon":
            _draw_overlay_polygon_layer(
                overlay_draw,
                layer=layer,
                map_h=map_h,
                to_pixel=_to_pixel,
            )
        elif layer.kind == "point":
            _draw_overlay_point_layer(
                base_image,
                label_draw,
                layer=layer,
                map_h=map_h,
                map_bounds_mercator=map_bounds_mercator,
            )

    composed = Image.alpha_composite(base_image.convert("RGBA"), overlay_rgba).convert("RGB")
    frame[:, :, :] = np.asarray(composed, dtype=np.uint8)


def _draw_overlay_polygon_layer(
    overlay_draw: ImageDraw.ImageDraw,
    *,
    layer: OverlayRenderLayer,
    map_h: int,
    to_pixel: Callable[[float, float], tuple[int, int]],
) -> None:
    overlay_mercator = _transform_geometry_to_crs(layer.geometry, "EPSG:3857")
    if overlay_mercator.is_empty:
        return

    polygons: list[Polygon] = []
    if isinstance(overlay_mercator, Polygon):
        polygons = [overlay_mercator]
    elif isinstance(overlay_mercator, MultiPolygon):
        polygons = list(overlay_mercator.geoms)
    elif hasattr(overlay_mercator, "geoms"):
        polygons = [geom for geom in overlay_mercator.geoms if isinstance(geom, Polygon)]

    fill_rgba = (
        layer.style.fill_color[0],
        layer.style.fill_color[1],
        layer.style.fill_color[2],
        int(round(max(0.0, min(layer.style.fill_opacity, 1.0)) * 255)),
    )
    stroke_rgba = (
        layer.style.stroke_color[0],
        layer.style.stroke_color[1],
        layer.style.stroke_color[2],
        255,
    )

    for polygon in polygons:
        outer = [to_pixel(float(x), float(y)) for x, y in polygon.exterior.coords]
        if len(outer) >= 3:
            overlay_draw.polygon(outer, fill=fill_rgba, outline=stroke_rgba)
            if layer.style.stroke_width > 1:
                overlay_draw.line(outer + [outer[0]], fill=stroke_rgba, width=layer.style.stroke_width)

        for interior in polygon.interiors:
            inner = [to_pixel(float(x), float(y)) for x, y in interior.coords]
            if len(inner) >= 3:
                overlay_draw.polygon(inner, fill=(0, 0, 0, 0), outline=(0, 0, 0, 0))


def _draw_overlay_point_layer(
    image: Image.Image,
    label_draw: ImageDraw.ImageDraw,
    *,
    layer: OverlayRenderLayer,
    map_h: int,
    map_bounds_mercator: tuple[float, float, float, float],
) -> None:
    point_mercator = _transform_geometry_to_crs(layer.geometry, "EPSG:3857")
    if not isinstance(point_mercator, Point) or point_mercator.is_empty:
        return

    map_min_x, map_min_y, map_max_x, map_max_y = map_bounds_mercator
    if map_max_x <= map_min_x or map_max_y <= map_min_y:
        return

    width, _height = image.size
    point_x = int(round(((float(point_mercator.x) - map_min_x) / (map_max_x - map_min_x)) * (width - 1)))
    point_y = int(round(((map_max_y - float(point_mercator.y)) / (map_max_y - map_min_y)) * max(0, map_h - 1)))

    if point_x < 0 or point_x >= width or point_y < 0 or point_y >= map_h:
        return

    marker_radius = max(3, layer.style.marker_size)
    label_draw.ellipse(
        [point_x - marker_radius, point_y - marker_radius, point_x + marker_radius, point_y + marker_radius],
        fill=layer.style.marker_color,
        outline=layer.style.marker_outline_color,
        width=max(1, marker_radius // 3),
    )

    label = _truncate(layer.label or "Point", 40)
    label_pad_x = 8
    label_pad_y = 4
    label_w, label_h = _measure_text(label_draw, label)
    bubble_w = label_w + (label_pad_x * 2)
    bubble_h = label_h + (label_pad_y * 2)

    label_x = min(max(0, point_x + marker_radius + 8), max(0, width - bubble_w - 2))
    label_y = point_y - bubble_h - 8
    if label_y < 0:
        label_y = min(map_h - bubble_h - 2, point_y + marker_radius + 8)
    label_y = max(0, min(label_y, max(0, map_h - bubble_h - 2)))

    label_draw.rounded_rectangle(
        [label_x, label_y, label_x + bubble_w, label_y + bubble_h],
        radius=max(4, bubble_h // 3),
        fill=layer.style.label_bg_color,
        outline=layer.style.marker_color,
        width=1,
    )
    label_draw.text((label_x + label_pad_x, label_y + label_pad_y), label, fill=layer.style.label_color)


def _draw_overlay_geometry(
    frame: np.ndarray,
    *,
    overlay_geometry: BaseGeometry | None,
    overlay_point_geometry: BaseGeometry | None,
    overlay_point_name: str | None,
    map_h: int,
    map_bounds_mercator: tuple[float, float, float, float],
) -> None:
    layers: list[OverlayRenderLayer] = []
    if overlay_geometry is not None:
        layers.append(
            OverlayRenderLayer(
                id="compat-overlay",
                label="Overlay",
                kind="polygon",
                geometry=overlay_geometry,
                style=_resolve_overlay_style(None, kind="polygon"),
                show_in_legend=False,
                legend_order=1000,
            )
        )
    if overlay_point_geometry is not None:
        layers.append(
            OverlayRenderLayer(
                id="compat-point",
                label=_resolve_overlay_point_name(overlay_point_name),
                kind="point",
                geometry=overlay_point_geometry,
                style=_resolve_overlay_style(None, kind="point"),
                show_in_legend=False,
                legend_order=1001,
            )
        )
    _draw_overlay_layers(
        frame,
        overlay_layers=layers,
        map_h=map_h,
        map_bounds_mercator=map_bounds_mercator,
    )


def _normalize_input_geometry(geojson_obj: dict, source_crs: str, settings: Settings) -> BaseGeometry:
    geom = _extract_geometry(geojson_obj)
    source = source_crs.upper()
    if source not in {"EPSG:4326", "EPSG:3857"}:
        raise ServiceValidationError(f"Unsupported CRS: {source_crs}")

    if source == "EPSG:3857":
        geom = _transform_geometry_between_crs(geom, source_crs="EPSG:3857", target_crs="EPSG:4326")

    _validate_geometry(geom, settings)
    return geom


def _normalize_overlay_point_geometry(geojson_obj: dict, source_crs: str, settings: Settings) -> BaseGeometry:
    geom = _extract_geometry(geojson_obj)
    if isinstance(geom, MultiPoint):
        if len(geom.geoms) == 0:
            raise ServiceValidationError("Overlay point is empty")
        geom = geom.geoms[0]
    elif hasattr(geom, "geoms") and not isinstance(geom, Point):
        point_candidates = [part for part in geom.geoms if isinstance(part, Point)]
        if point_candidates:
            geom = point_candidates[0]

    if not isinstance(geom, Point):
        raise ServiceValidationError("Overlay point must be a Point geometry")

    source = source_crs.upper()
    if source not in {"EPSG:4326", "EPSG:3857"}:
        raise ServiceValidationError(f"Unsupported CRS: {source_crs}")

    if source == "EPSG:3857":
        geom = _transform_geometry_between_crs(geom, source_crs="EPSG:3857", target_crs="EPSG:4326")

    if geom.is_empty:
        raise ServiceValidationError("Overlay point is empty")

    # Reuse the existing spatial guardrails to keep the point inside the service coverage area.
    _validate_geometry(geom.buffer(1e-9).envelope, settings)
    return geom


def _resolve_overlay_point_name(value: str | None) -> str:
    if value is None:
        return "Point"
    cleaned = value.strip()
    return cleaned or "Point"


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


def _to_rgb(image: np.ndarray) -> np.ndarray:
    # Threat-map PMTiles are already colorized; preserve source RGB values as-is.
    return np.array(_to_rgb_raw(image), copy=True)


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
    panel_y0 = map_h + 8
    panel_x1 = width - outer_pad
    panel_y1 = height - 8

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

    # Keep year prominent but neutral for better readability in shared frames.
    year_text = f"Year {year}"
    title_text = "Landcover Legend"
    title_h = _measure_text(draw, title_text)[1]
    year_h = _measure_text(draw, year_text)[1]
    header_h = max(title_h, year_h)

    draw.text((header_pad_x, header_y), title_text, fill=(32, 44, 58))
    year_w, _ = _measure_text(draw, year_text)
    year_x = max(header_pad_x, panel_x1 - 12 - year_w)
    draw.text((year_x, header_y), year_text, fill=(24, 37, 52))

    section_y = header_y + header_h + 7
    draw.line([(panel_x0 + 10, section_y), (panel_x1 - 10, section_y)], fill=(191, 201, 214), width=1)

    content_x0 = panel_x0 + 10
    content_x1 = panel_x1 - 10
    content_y0 = section_y + 8
    row_h = 24
    swatch = 14
    gutter_x = 8
    label_pad = 6

    valid_entries = _collect_valid_legend_entries(legend_entries)

    if not valid_entries:
        draw.text((content_x0, content_y0), "No legend entries available", fill=(111, 123, 138))
        frame[:, :, :] = np.asarray(image, dtype=np.uint8)
        return

    available_w = max(1, content_x1 - content_x0)
    col_count = 2 if len(valid_entries) >= 2 else 1

    col_w = max(160, (available_w - ((col_count - 1) * gutter_x)) // col_count)
    for idx, (class_code, label, color) in enumerate(valid_entries):
        row = idx // col_count
        col = idx % col_count
        item_x = content_x0 + (col * (col_w + gutter_x))
        item_y = content_y0 + (row * row_h)

        swatch_y0 = item_y + ((row_h - swatch) // 2)
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
        draw.text((text_x, item_y + 4), clipped, fill=(41, 53, 67))

    frame[:, :, :] = np.asarray(image, dtype=np.uint8)


def _compute_legend_band_height(*, width: int, legend_entries: list[dict[str, str]]) -> int:
    valid_entries = _collect_valid_legend_entries(legend_entries)
    if not valid_entries:
        return 90

    col_count = 2 if len(valid_entries) >= 2 else 1

    row_h = 24
    rows = (len(valid_entries) + col_count - 1) // col_count

    # 84px accounts for header, separators, and panel paddings; rows add vertical scale.
    return max(100, 84 + (rows * row_h))


def _collect_valid_legend_entries(legend_entries: list[dict[str, str]]) -> list[tuple[str, str, tuple[int, int, int]]]:
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
    return valid_entries


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


def _resolve_repo_path(path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute() or path.exists():
        return path

    repo_root = Path(__file__).resolve().parents[2]
    candidate = repo_root / path
    if candidate.exists():
        return candidate
    return path


@lru_cache(maxsize=4)
def _load_legend_entries_from_path(path_text: str) -> list[dict[str, str]]:
    path = _resolve_repo_path(path_text)
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
    path = _resolve_repo_path(path_text)
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


@lru_cache(maxsize=4)
def _load_legend_entries_from_mapbiomas_colors_path(path_text: str) -> list[dict[str, str]]:
    path = _resolve_repo_path(path_text)
    if not path.exists():
        return []

    entries: list[dict[str, str]] = []
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            comment_index = line.find("#")
            if comment_index >= 0:
                line = line[:comment_index].strip()
            if not line:
                continue

            tokens = line.split()
            if len(tokens) < 4:
                continue

            try:
                class_code = tokens[0]
                red = int(tokens[1])
                green = int(tokens[2])
                blue = int(tokens[3])
            except ValueError:
                continue

            label = raw_line[comment_index + 1 :].strip() if comment_index >= 0 else ""
            entries.append(
                {
                    "class_code": class_code,
                    "label": label,
                    "color": f"#{red:02x}{green:02x}{blue:02x}",
                }
            )
    except OSError:
        return []

    return entries


def _load_legend_entries(settings: Settings) -> list[dict[str, str]]:
    mapbiomas_entries = _load_legend_entries_from_mapbiomas_colors_path(str(settings.threat_map_legend_colors_path))
    if mapbiomas_entries:
        return mapbiomas_entries

    manifest_entries = _load_legend_entries_from_path(str(settings.threat_map_legend_manifest_path))
    if manifest_entries:
        return manifest_entries

    # Fallback for local workflows where the extracted manifest file is absent.
    fallback_candidates = [
        "mekar_raya.qgz",
        str(Path(__file__).resolve().parents[2] / "mekar_raya.qgz"),
    ]
    for candidate in fallback_candidates:
        qgz_entries = _load_legend_entries_from_qgz_path(candidate)
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
