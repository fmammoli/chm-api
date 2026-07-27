from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import os
import tempfile
import time
from threading import Lock
from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import cast

import geopandas as gpd
import numpy as np
import rasterio
import rasterio.mask
from pyproj import Transformer
from rasterio.transform import array_bounds
from rasterio.enums import Resampling
from rasterio.io import DatasetReader, MemoryFile
from rasterio.merge import merge
from rasterio.shutil import copy as rio_copy
from rasterio.warp import calculate_default_transform, reproject
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform
from shapely.strtree import STRtree

from app.config import Settings

# Enable fast remote COG access with GDAL
os.environ["GDAL_DISABLE_READDIR_ON_OPEN"] = "YES"
os.environ["CPL_VSIL_CURL_ALLOWED_EXTENSIONS"] = ".tif"
os.environ["CPL_VSIL_CURL_CHUNK_SIZE"] = "8388608"  # 8MB chunks
os.environ["GDAL_HTTP_TIMEOUT"] = "30"

logger = logging.getLogger("chm_api")


class ServiceValidationError(ValueError):
    pass


@dataclass
class CropResult:
    output_path: Path
    crs: str
    bounds: tuple[float, float, float, float]


_TILE_INDEX_CACHE: dict[str, object] = {"gdf": None, "loaded_at": 0.0}
_INDONESIA_GEOM_CACHE: dict[str, object] = {"geom": None}
_TRANSFORMER_CACHE: dict[tuple[str, str], Transformer] = {}
TILE_CACHE_TTL_SECONDS = 300.0
DEFAULT_OVERVIEW_LEVELS = (2, 4, 8, 16, 32)
DEFAULT_BLOCK_SIZE = 512


def _get_remote_tile_url(settings: Settings, tile_name: str) -> str:
    """Generate HTTPS URL for COG in S3."""
    return f"https://{settings.s3_bucket}.s3.amazonaws.com/{settings.s3_path}chm/{tile_name}.tif"


def _extract_geometry(geojson_obj: dict) -> BaseGeometry:
    try:
        geo_type = geojson_obj.get("type")
        if geo_type == "FeatureCollection":
            features = geojson_obj.get("features", [])
            if not features:
                raise ServiceValidationError("GeoJSON FeatureCollection is empty")
            first_geometry = features[0].get("geometry")
            if not first_geometry:
                raise ServiceValidationError("GeoJSON feature is missing geometry")
            geom = shape(first_geometry)
            for feature in features[1:]:
                feature_geom = feature.get("geometry")
                if not feature_geom:
                    raise ServiceValidationError("GeoJSON feature is missing geometry")
                geom = geom.union(shape(feature_geom))
            return geom
        if geo_type == "Feature":
            geom = geojson_obj.get("geometry")
            if not geom:
                raise ServiceValidationError("GeoJSON feature is missing geometry")
            return shape(geom)
        return shape(geojson_obj)
    except ServiceValidationError:
        raise
    except Exception as exc:
        raise ServiceValidationError("Invalid GeoJSON geometry") from exc


def _count_vertices(geom) -> int:
    if geom.geom_type == "Polygon":
        exterior = getattr(geom, "exterior", None)
        return len(exterior.coords) if exterior is not None else 0
    if geom.geom_type == "MultiPolygon":
        geoms = getattr(geom, "geoms", [])
        return sum(len(getattr(p, "exterior").coords) for p in geoms if getattr(p, "exterior", None) is not None)
    return 0


def _validate_square_size(geom: BaseGeometry, side_km: float) -> None:
    if side_km <= 0:
        raise ServiceValidationError("AOI square side must be positive")

    # Validate in meters to avoid degree-based distortion in EPSG:4326.
    geom_3857 = _transform_geometry_to_crs(geom, "EPSG:3857")
    minx, miny, maxx, maxy = geom_3857.bounds
    width_km = (maxx - minx) / 1000.0
    height_km = (maxy - miny) / 1000.0

    # Keep tolerance practical for frontend-generated polygons and projection effects.
    tolerance_km = max(0.5, side_km * 0.15)
    if abs(width_km - side_km) > tolerance_km or abs(height_km - side_km) > tolerance_km:
        raise ServiceValidationError(
            f"AOI square side must be {side_km:.1f} km (+/- {tolerance_km:.1f} km)"
        )

    square_delta_km = abs(width_km - height_km)
    if square_delta_km > max(0.5, side_km * 0.1):
        raise ServiceValidationError("AOI must be a square")


def _transform_geometry_to_crs(geom: BaseGeometry, dst_crs) -> BaseGeometry:
    dst_crs_str = str(dst_crs)
    if not dst_crs_str:
        raise ServiceValidationError("Destination CRS is missing")

    if dst_crs_str.upper() == "EPSG:4326":
        return geom

    key = ("EPSG:4326", dst_crs_str)
    transformer = _TRANSFORMER_CACHE.get(key)
    if transformer is None:
        transformer = Transformer.from_crs("EPSG:4326", dst_crs_str, always_xy=True)
        _TRANSFORMER_CACHE[key] = transformer
    return shapely_transform(transformer.transform, geom)


def _load_indonesia_geometry(settings: Settings):
    cached = _INDONESIA_GEOM_CACHE["geom"]
    if cached is not None:
        return cached

    boundary_path = settings.indonesia_boundary_path
    if not boundary_path.exists():
        raise ServiceValidationError("Indonesia boundary file is missing on server")

    boundary_gdf = gpd.read_file(boundary_path)
    if boundary_gdf.empty:
        raise ServiceValidationError("Indonesia boundary file is empty")

    # Ensure geometry is in EPSG:4326 before spatial checks with input GeoJSON.
    if boundary_gdf.crs is None:
        boundary_gdf = boundary_gdf.set_crs("EPSG:4326")
    elif str(boundary_gdf.crs).upper() != "EPSG:4326":
        boundary_gdf = boundary_gdf.to_crs("EPSG:4326")

    indonesia_geom = boundary_gdf.geometry.unary_union
    _INDONESIA_GEOM_CACHE["geom"] = indonesia_geom
    return indonesia_geom


def _validate_indonesia_only(geom: BaseGeometry, settings: Settings) -> None:
    indonesia_geom = _load_indonesia_geometry(settings)
    if not isinstance(indonesia_geom, BaseGeometry):
        raise ServiceValidationError("Indonesia boundary geometry cache is invalid")
    if not geom.intersects(indonesia_geom):
        raise ServiceValidationError("it only shows data from indonesia")


def _validate_geometry(geom: BaseGeometry, settings: Settings) -> None:
    if geom.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ServiceValidationError("GeoJSON must be a Polygon or MultiPolygon")
    if not geom.is_valid:
        raise ServiceValidationError("GeoJSON geometry is invalid")

    minx, miny, maxx, maxy = geom.bounds
    if minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
        raise ServiceValidationError("GeoJSON coordinates must be EPSG:4326 lon/lat")

    vertices = _count_vertices(geom)
    if vertices > settings.max_vertices:
        raise ServiceValidationError("GeoJSON has too many vertices")

    # Approximate AOI area in km^2 by projecting to EPSG:3857.
    area_geom = _transform_geometry_to_crs(geom, "EPSG:3857")
    area_km2 = float(area_geom.area) / 1e6
    if area_km2 > settings.max_aoi_area_km2:
        raise ServiceValidationError("AOI is too large")

    _validate_indonesia_only(geom, settings)


def _load_tiles_index(settings: Settings) -> gpd.GeoDataFrame:
    now = time.time()
    cached_gdf = _TILE_INDEX_CACHE["gdf"]
    cached_time = _TILE_INDEX_CACHE["loaded_at"]
    if cached_gdf is not None and isinstance(cached_time, (int, float)) and now - float(cached_time) < settings.tile_index_ttl_seconds:
        return cached_gdf  # type: ignore[return-value]

    # Download tiles index from S3 public URL
    tiles_url = f"https://{settings.s3_bucket}.s3.amazonaws.com/{settings.s3_path}tiles.geojson"
    logger.info("📥 Downloading tiles index from %s", tiles_url)
    
    with tempfile.NamedTemporaryFile(suffix=".geojson", delete=False) as temp_file:
        temp_path = Path(temp_file.name)

    try:
        import urllib.request
        urllib.request.urlretrieve(tiles_url, str(temp_path))
        gdf = gpd.read_file(temp_path)
        _TILE_INDEX_CACHE["gdf"] = gdf
        _TILE_INDEX_CACHE["loaded_at"] = now
        logger.info("✅ Tiles index loaded with %d tiles", len(gdf))
        return gdf
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _load_tiles_spatial_index(settings: Settings) -> tuple[STRtree, list[BaseGeometry], list[str], object]:
    now = time.time()
    cached_tree = _TILE_INDEX_CACHE.get("tree")
    cached_geoms = _TILE_INDEX_CACHE.get("geoms")
    cached_tiles = _TILE_INDEX_CACHE.get("tiles")
    cached_crs = _TILE_INDEX_CACHE.get("crs")
    cached_time = _TILE_INDEX_CACHE.get("loaded_at", 0.0)

    if (
        cached_tree is not None
        and cached_geoms is not None
        and cached_tiles is not None
        and cached_crs is not None
        and isinstance(cached_time, (int, float))
        and isinstance(cached_tree, STRtree)
        and isinstance(cached_geoms, list)
        and isinstance(cached_tiles, list)
        and now - float(cached_time) < settings.tile_index_ttl_seconds
    ):
        return cached_tree, cast(list[BaseGeometry], cached_geoms), cast(list[str], cached_tiles), cached_crs

    gdf = _load_tiles_index(settings)
    geoms = [geom for geom in gdf.geometry.tolist() if geom is not None]
    if not geoms:
        raise ServiceValidationError("Tiles index has no geometries")

    # Keep tiles and geoms aligned by index for STRtree query results.
    tiles = gdf["tile"].astype(str).tolist()
    if len(tiles) != len(geoms):
        raise ServiceValidationError("Tiles index is inconsistent")

    tree = STRtree(geoms)
    tiles_crs = gdf.crs if gdf.crs is not None else "EPSG:4326"

    _TILE_INDEX_CACHE["tree"] = tree
    _TILE_INDEX_CACHE["geoms"] = geoms
    _TILE_INDEX_CACHE["tiles"] = tiles
    _TILE_INDEX_CACHE["crs"] = tiles_crs
    _TILE_INDEX_CACHE["loaded_at"] = now
    return tree, geoms, tiles, tiles_crs


def _ctrees_agb_remote_cog_url(settings: Settings, year: int, variable: str) -> str:
    base = f"global_agb_100m_landsat0024_all_{year}_densenet_l1_agb_mosaic_100m_base_cd_ts"
    suffix = "_uncertainty_sem.tif" if variable == "uncertainty" else ".tif"
    prefix = settings.ctrees_agb_s3_prefix.lstrip("/")
    return f"https://{settings.ctrees_agb_s3_bucket}.s3.amazonaws.com/{prefix}{base}{suffix}"

def _resolve_nodata(src: DatasetReader) -> float:
    if src.nodata is not None:
        return float(src.nodata)

    if np.issubdtype(src.dtypes[0], np.integer):
        return float(np.iinfo(src.dtypes[0]).max)
    return -9999.0


def _overview_levels(width: int, height: int) -> list[int]:
    min_dim = min(width, height)
    levels = [level for level in DEFAULT_OVERVIEW_LEVELS if min_dim // level >= 1]
    return levels or [2]


def _compute_band_stats(data: np.ndarray, nodata: float) -> tuple[float, float] | None:
    if np.issubdtype(data.dtype, np.floating):
        valid = data[np.isfinite(data) & ~np.isclose(data, nodata)]
    else:
        valid = data[data != nodata]

    if valid.size == 0:
        return None
    return float(valid.min()), float(valid.max())


def _export_cog(
    data: np.ndarray,
    transform,
    crs,
    dtype: str,
    nodata: float,
    output_path: Path,
) -> None:
    base_tif = output_path.with_name("output_base.tif")
    profile = {
        "driver": "GTiff",
        "count": data.shape[0],
        "dtype": dtype,
        "height": data.shape[1],
        "width": data.shape[2],
        "transform": transform,
        "crs": crs,
        "nodata": nodata,
        "tiled": True,
        "blockxsize": DEFAULT_BLOCK_SIZE,
        "blockysize": DEFAULT_BLOCK_SIZE,
        "compress": "DEFLATE",
        "BIGTIFF": "IF_SAFER",
        "NUM_THREADS": "ALL_CPUS",
    }

    if np.issubdtype(np.dtype(dtype), np.floating):
        profile["predictor"] = 3
    elif np.issubdtype(np.dtype(dtype), np.integer):
        profile["predictor"] = 2

    with rasterio.open(base_tif, "w", **profile) as dst:
        dst.write(data.astype(dtype, copy=False))

    levels = _overview_levels(profile["width"], profile["height"])
    with rasterio.open(base_tif, "r+") as dst:
        dst.nodata = nodata
        dst.build_overviews(levels, Resampling.average)
        dst.update_tags(ns="rio_overview", resampling="average")
        stats = _compute_band_stats(data[0], nodata)
        if stats is not None:
            min_val, max_val = stats
            dst.update_tags(1, STATISTICS_MINIMUM=f"{min_val:.6f}", STATISTICS_MAXIMUM=f"{max_val:.6f}")

    # Prefer COG driver for deterministic layout and HTTP range-read friendliness.
    try:
        rio_copy(
            base_tif,
            output_path,
            driver="COG",
            COMPRESS="DEFLATE",
            BLOCKSIZE=DEFAULT_BLOCK_SIZE,
            BIGTIFF="IF_SAFER",
            RESAMPLING="AVERAGE",
            OVERVIEWS="AUTO",
            NUM_THREADS="ALL_CPUS",
        )
    except Exception as exc:
        logger.warning("COG translation failed, falling back to tiled GeoTIFF: %s", str(exc))
        shutil.move(str(base_tif), str(output_path))
    else:
        base_tif.unlink(missing_ok=True)


def build_cropped_ctrees_agb_raster(
    geojson_obj: dict,
    year: int,
    variable: str,
    settings: Settings,
    workdir: Path,
) -> CropResult:
    t_start = time.perf_counter()
    try:
        payload_len = len(json.dumps(geojson_obj).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ServiceValidationError("Invalid JSON payload") from exc

    if payload_len > settings.max_geojson_bytes:
        raise ServiceValidationError("GeoJSON payload too large")

    geom = _extract_geometry(geojson_obj)
    _validate_geometry(geom, settings)
    _validate_square_size(geom, side_km=settings.aoi_square_side_km)

    logger.info("📐 Using provided AOI square with side target=%.1f km", settings.aoi_square_side_km)

    remote_cog_url = _ctrees_agb_remote_cog_url(settings, year=year, variable=variable)
    logger.info("📍 Cropping CTrees AGB COG: %s", remote_cog_url)

    t_crop_start = time.perf_counter()
    try:
        with rasterio.open(remote_cog_url) as src:
            nodata = _resolve_nodata(src)
            target_dtype = src.dtypes[0]
            src_crs = src.crs
            geom_src_crs = _transform_geometry_to_crs(geom, src.crs)
            image, transform = rasterio.mask.mask(
                src,
                [mapping(geom_src_crs)],
                crop=True,
                nodata=nodata,
                all_touched=False,
                filled=True,
            )
    except Exception as exc:
        raise ServiceValidationError("Failed to crop CTrees AGB data") from exc

    if image.size == 0:
        raise ServiceValidationError("Requested AOI does not intersect CTrees AGB coverage")
    if src_crs is None:
        raise ServiceValidationError("Source raster CRS is missing")
    t_crop_end = time.perf_counter()

    # Reproject only when needed; if source is already Web Mercator, keep native grid.
    t_reproject_start = time.perf_counter()
    dst_crs = "EPSG:3857"
    src_crs_str = str(src_crs).upper()
    if src_crs_str in {"EPSG:3857", "WGS 84 / PSEUDO-MERCATOR"}:
        reprojected = image
        dst_transform = transform
    else:
        src_bounds = array_bounds(image.shape[1], image.shape[2], transform)
        dst_transform, dst_width, dst_height = calculate_default_transform(
            src_crs,
            dst_crs,
            image.shape[2],
            image.shape[1],
            *src_bounds,
        )
        if dst_width is None or dst_height is None:
            raise ServiceValidationError("Failed to compute target raster shape for EPSG:3857")
        dst_width = int(dst_width)
        dst_height = int(dst_height)
        reprojected = np.full((image.shape[0], dst_height, dst_width), nodata, dtype=np.dtype(target_dtype))

        for band_idx in range(image.shape[0]):
            reproject(
                source=image[band_idx],
                destination=reprojected[band_idx],
                src_transform=transform,
                src_crs=src_crs,
                src_nodata=nodata,
                dst_transform=dst_transform,
                dst_crs=dst_crs,
                dst_nodata=nodata,
                resampling=Resampling.nearest,
            )
    t_reproject_end = time.perf_counter()

    t_export_start = time.perf_counter()
    output_path = workdir / f"ctrees_agb_{variable}_{year}.tif"
    _export_cog(
        data=reprojected,
        transform=dst_transform,
        crs=dst_crs,
        dtype=target_dtype,
        nodata=nodata,
        output_path=output_path,
    )

    with rasterio.open(output_path) as src:
        bounds = array_bounds(src.height, src.width, src.transform)
        crs = str(src.crs)

    logger.info(
        "✅ CTrees AGB crop completed. variable=%s year=%s size=%s",
        variable,
        year,
        output_path.stat().st_size,
    )
    logger.info(
        "⏱️ CTrees timings seconds: crop=%.3f reproject=%.3f export=%.3f total=%.3f",
        t_crop_end - t_crop_start,
        t_reproject_end - t_reproject_start,
        time.perf_counter() - t_export_start,
        time.perf_counter() - t_start,
    )
    return CropResult(output_path=output_path, crs=crs, bounds=bounds)


def build_cropped_raster(geojson_obj: dict, settings: Settings, workdir: Path) -> CropResult:
    t_start = time.perf_counter()
    try:
        payload_len = len(json.dumps(geojson_obj).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ServiceValidationError("Invalid JSON payload") from exc

    if payload_len > settings.max_geojson_bytes:
        raise ServiceValidationError("GeoJSON payload too large")

    geom = _extract_geometry(geojson_obj)
    _validate_geometry(geom, settings)
    _validate_square_size(geom, side_km=settings.aoi_square_side_km)

    logger.info("📐 Using provided AOI square with side target=%.1f km", settings.aoi_square_side_km)

    tree, _, tiles, tiles_crs = _load_tiles_spatial_index(settings)
    geom_tiles_crs = _transform_geometry_to_crs(geom, tiles_crs)
    intersect_indexes = tree.query(geom_tiles_crs, predicate="intersects")
    if len(intersect_indexes) == 0:
        raise ServiceValidationError("it only shows data from indonesia")

    tile_names = [tiles[int(i)] for i in intersect_indexes]
    if len(tile_names) > settings.max_tiles_per_request:
        raise ServiceValidationError("AOI intersects too many tiles")

    logger.info("🎯 Cropping %d remote COG tiles from S3...", len(tile_names))
    tile_urls = [_get_remote_tile_url(settings, t) for t in tile_names]

    # Crop remote tiles in parallel and keep them as in-memory datasets for deterministic merge.
    cropped_datasets: list[DatasetReader] = []
    memfiles: list[MemoryFile] = []
    target_crs = None
    target_dtype = None
    nodata = None

    geom_mapping_cache: dict[str, dict] = {}
    geom_cache_lock = Lock()

    def _crop_tile_part(tile_idx: int, tile_url: str):
        with rasterio.open(tile_url) as src:
            if src.crs is None:
                raise ServiceValidationError("Source tile CRS is missing")

            src_crs_key = str(src.crs)
            geom_mapping = geom_mapping_cache.get(src_crs_key)
            if geom_mapping is None:
                geom_src_crs = _transform_geometry_to_crs(geom, src.crs)
                geom_mapping = mapping(geom_src_crs)
                with geom_cache_lock:
                    geom_mapping_cache.setdefault(src_crs_key, geom_mapping)

            tile_nodata = _resolve_nodata(src)
            image, transform = rasterio.mask.mask(
                src,
                [geom_mapping],
                crop=True,
                nodata=tile_nodata,
                all_touched=False,
                filled=True,
            )

            mem = MemoryFile()
            with mem.open(
                driver="GTiff",
                count=image.shape[0],
                dtype=src.dtypes[0],
                height=image.shape[1],
                width=image.shape[2],
                crs=src.crs,
                transform=transform,
                nodata=tile_nodata,
            ) as tmp_ds:
                tmp_ds.write(image)

            return tile_idx, tile_url, mem.open(), mem, src.crs, src.dtypes[0], tile_nodata

    t_crop_start = time.perf_counter()
    crop_results: list[tuple[int, str, DatasetReader, MemoryFile, object, str, float]] = []
    max_workers = max(1, settings.download_workers)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_tile = {
            executor.submit(_crop_tile_part, i, tile_url): (i, tile_url)
            for i, tile_url in enumerate(tile_urls)
        }
        for future in as_completed(future_to_tile):
            i, tile_url = future_to_tile[future]
            logger.info("📍 Cropped tile %d/%d: %s", i + 1, len(tile_urls), tile_url.split("/")[-1])
            try:
                crop_results.append(future.result())
            except Exception as e:
                logger.warning("⚠️ Failed to crop tile %s: %s", tile_url, str(e))

    crop_results.sort(key=lambda item: item[0])
    for _, _, ds, mem, src_crs, src_dtype, tile_nodata in crop_results:
        if nodata is None:
            nodata = tile_nodata
        if target_crs is None:
            target_crs = src_crs
        if target_dtype is None:
            target_dtype = src_dtype
        cropped_datasets.append(ds)
        memfiles.append(mem)
    t_crop_end = time.perf_counter()

    if not cropped_datasets:
        raise ServiceValidationError("Failed to process any tiles")

    try:
        if target_crs is None or target_dtype is None or nodata is None:
            raise ServiceValidationError("Failed to resolve raster metadata for export")

        t_merge_start = time.perf_counter()
        if len(cropped_datasets) == 1:
            logger.info("🔀 Single tile crop; skipping merge step")
            only_ds = cropped_datasets[0]
            mosaic = only_ds.read()
            transform = only_ds.transform
        else:
            logger.info("🔀 Merging %d cropped tile parts with nodata=%s", len(cropped_datasets), nodata)
            mosaic, transform = merge(
                cropped_datasets,
                nodata=nodata,
                dtype=target_dtype,
                method="first",
            )
        t_merge_end = time.perf_counter()

        t_export_start = time.perf_counter()
        cropped_path = workdir / "output.tif"
        _export_cog(
            data=mosaic,
            transform=transform,
            crs=target_crs,
            dtype=target_dtype,
            nodata=nodata,
            output_path=cropped_path,
        )

        with rasterio.open(cropped_path) as src:
            bounds = array_bounds(src.height, src.width, src.transform)
            logger.info(
                "🧭 Output metadata: size=%dx%d pixel_size=(%.10f, %.10f) nodata=%s overviews=%s",
                src.width,
                src.height,
                src.transform.a,
                src.transform.e,
                src.nodata,
                src.overviews(1),
            )
            crs = str(src.crs)
        t_export_end = time.perf_counter()
    finally:
        for ds in cropped_datasets:
            ds.close()
        for mem in memfiles:
            mem.close()

    logger.info("✅ Raster processing completed. size=%s crs=%s", cropped_path.stat().st_size, crs)
    logger.info(
        "⏱️ CHM timings seconds: crop_parallel=%.3f merge=%.3f export=%.3f total=%.3f",
        t_crop_end - t_crop_start,
        t_merge_end - t_merge_start,
        t_export_end - t_export_start,
        t_export_end - t_start,
    )
    return CropResult(output_path=cropped_path, crs=crs, bounds=bounds)


def stream_file_chunks(file_path: Path, chunk_size: int = 1024 * 1024):
    with open(file_path, "rb") as file_obj:
        while True:
            chunk = file_obj.read(chunk_size)
            if not chunk:
                break
            yield chunk


def safe_rmtree(path: Path) -> None:
    if not path.exists() or not path.is_dir():
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            Path(root, name).unlink(missing_ok=True)
        for name in dirs:
            Path(root, name).rmdir()
    path.rmdir()
