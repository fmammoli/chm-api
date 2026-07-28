#!/usr/bin/env python3
"""End-to-end smoke test for Threat Map API using Mekar Raya shapefile center.

This script:
1. Reads the Mekar Raya shapefile.
2. Computes the centroid.
3. Builds a 20 km x 20 km square AOI centered on that centroid.
4. Submits POST /api/v1/threat-map/jobs.
5. Polls GET /api/v1/threat-map/jobs/{job_id} until terminal status.
6. Downloads the final artifact from /download when succeeded or partial_success.

Examples:
    export CHM_API_KEY="your-api-key"
    python scripts/test_threat_map_api_mekar_raya.py

    python scripts/test_threat_map_api_mekar_raya.py \
      --base-url http://127.0.0.1:8000 \
      --api-key your-api-key \
    --side-km 20
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
from typing import Any

import geopandas as gpd
from shapely.geometry import Polygon


DEFAULT_BASE_URL = os.environ.get("THREAT_MAP_API_BASE_URL", "http://127.0.0.1:8000")
DEFAULT_API_KEY = os.environ.get("CHM_API_KEY") or os.environ.get("API_KEY") or ""
DEFAULT_SHAPEFILE = Path("Mekar Raya GIS Data") / "Merge_Desa_AKKM.shp"
DEFAULT_SIDE_KM = 20.0


def request_json(method: str, url: str, *, api_key: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": api_key,
    }
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            detail = json.loads(raw)
        except json.JSONDecodeError:
            detail = {"raw": raw}
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def download_file(url: str, *, api_key: str, destination: Path) -> tuple[int, str]:
    headers = {
        "X-API-Key": api_key,
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=600) as response:
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            payload = response.read()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            return response.status, content_type
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {exc.code} on download: {raw}") from exc


def build_square_geojson_from_shapefile(shapefile_path: Path, side_km: float) -> dict[str, Any]:
    if side_km <= 0:
        raise ValueError("side_km must be > 0")

    gdf = gpd.read_file(shapefile_path)
    if gdf.empty:
        raise RuntimeError(f"Shapefile has no features: {shapefile_path}")

    if gdf.crs is None:
        raise RuntimeError(f"Shapefile has no CRS: {shapefile_path}")

    # Use Web Mercator meters for a simple meter-based square construction.
    gdf_3857 = gdf.to_crs(epsg=3857)
    geom_union = gdf_3857.geometry.union_all()
    center = geom_union.centroid

    half_m = (side_km * 1000.0) / 2.0
    cx = float(center.x)
    cy = float(center.y)

    square_3857 = Polygon(
        [
            (cx - half_m, cy - half_m),
            (cx + half_m, cy - half_m),
            (cx + half_m, cy + half_m),
            (cx - half_m, cy + half_m),
            (cx - half_m, cy - half_m),
        ]
    )

    square_wgs84 = gpd.GeoSeries([square_3857], crs="EPSG:3857").to_crs(epsg=4326).iloc[0]
    coords = [[float(x), float(y)] for x, y in list(square_wgs84.exterior.coords)]

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "source": "Mekar Raya GIS Data",
                    "centerMethod": "shapefile_union_centroid",
                    "sideKm": side_km,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [coords],
                },
            }
        ],
    }


def create_job(base_url: str, api_key: str, geojson: dict[str, Any], preset: str, output_format: str) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/v1/threat-map/jobs"
    payload = {
        "geojson": geojson,
        "geojsonCrs": "EPSG:4326",
        "preset": preset,
        "outputFormat": output_format,
    }
    status, body = request_json("POST", url, api_key=api_key, payload=payload)
    print(f"Create job response [{status}]:")
    print(json.dumps(body, indent=2))
    return body


def poll_job(base_url: str, api_key: str, job_id: str, *, timeout_seconds: int, poll_interval: float) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/v1/threat-map/jobs/{job_id}"
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        status, body = request_json("GET", url, api_key=api_key)
        status_value = str(body.get("status", "")).lower()
        progress = body.get("progress")
        current_year = body.get("currentYear")
        message = body.get("message")
        print(f"Poll [{status}] status={status_value} progress={progress} year={current_year} message={message}")

        if status_value in {"succeeded", "partial_success", "failed", "cancelled"}:
            print("Final job payload:")
            print(json.dumps(body, indent=2))
            return body

        time.sleep(poll_interval)

    raise TimeoutError(f"Timed out waiting for job {job_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="E2E Threat Map smoke test using Mekar Raya shapefile center")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API base URL (default: %(default)s)")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="API key for X-API-Key header")
    parser.add_argument("--shapefile", default=str(DEFAULT_SHAPEFILE), help="Path to Mekar Raya shapefile")
    parser.add_argument("--side-km", type=float, default=DEFAULT_SIDE_KM, help="Square side length in km")
    parser.add_argument("--preset", choices=["balanced", "high"], default="balanced", help="Threat-map preset")
    parser.add_argument(
        "--output-format",
        choices=["mp4", "frames_tar_gz"],
        default="frames_tar_gz",
        help="Threat-map artifact mode: mp4 (server encode) or frames_tar_gz (client encode)",
    )
    parser.add_argument("--timeout-seconds", type=int, default=1800, help="Max wait for job completion")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Poll interval seconds")
    parser.add_argument(
        "--download-dir",
        default="outputs",
        help="Directory to store downloaded result (default: outputs)",
    )
    args = parser.parse_args()

    if not args.api_key:
        print("Missing API key. Provide --api-key or set CHM_API_KEY/API_KEY.", file=sys.stderr)
        return 2

    shapefile_path = Path(args.shapefile)
    if not shapefile_path.exists():
        print(f"Shapefile not found: {shapefile_path}", file=sys.stderr)
        return 2

    try:
        geojson = build_square_geojson_from_shapefile(shapefile_path, args.side_km)
        print("Constructed AOI GeoJSON (20 km square around shapefile centroid):")
        print(json.dumps(geojson, indent=2))

        created = create_job(args.base_url, args.api_key, geojson, args.preset, args.output_format)
        job_id = created.get("jobId")
        if not job_id:
            print("No jobId returned from create endpoint", file=sys.stderr)
            return 1

        terminal = poll_job(
            args.base_url,
            args.api_key,
            job_id,
            timeout_seconds=args.timeout_seconds,
            poll_interval=args.poll_interval,
        )

        status_value = str(terminal.get("status", "")).lower()
        if status_value not in {"succeeded", "partial_success"}:
            print(f"Job ended with non-downloadable status: {status_value}", file=sys.stderr)
            return 1

        result = terminal.get("result") or {}
        artifact_type = str(result.get("artifactType", "mp4"))
        if artifact_type == "zip":
            ext = "zip"
        elif artifact_type == "frames_tar_gz":
            ext = "tar.gz"
        else:
            ext = "mp4"

        download_url = f"{args.base_url.rstrip('/')}/api/v1/threat-map/jobs/{job_id}/download"
        destination = Path(args.download_dir) / f"threat_map_{job_id}.{ext}"
        status_code, content_type = download_file(download_url, api_key=args.api_key, destination=destination)

        print(f"Downloaded [{status_code}] content-type={content_type} to {destination}")
        return 0
    except Exception as exc:  # pragma: no cover - CLI error path
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
