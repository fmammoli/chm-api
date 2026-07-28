from __future__ import annotations

import json
import os
from pathlib import Path
import time

import geopandas as gpd
import pytest
import requests
from shapely.geometry import Polygon


def _build_square_geojson_from_mekar_raya(side_km: float = 30.0) -> dict:
    shp_path = Path("Mekar Raya GIS Data") / "Merge_Desa_AKKM.shp"
    if not shp_path.exists():
        raise FileNotFoundError(f"Missing shapefile: {shp_path}")

    gdf = gpd.read_file(shp_path)
    if gdf.empty:
        raise RuntimeError("Mekar Raya shapefile is empty")
    if gdf.crs is None:
        raise RuntimeError("Mekar Raya shapefile has no CRS")

    gdf_3857 = gdf.to_crs(epsg=3857)
    center = gdf_3857.geometry.union_all().centroid

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
    coordinates = [[float(x), float(y)] for x, y in list(square_wgs84.exterior.coords)]

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "source": "Mekar Raya GIS Data",
                    "sideKm": side_km,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [coordinates],
                },
            }
        ],
    }


@pytest.mark.skipif(
    os.environ.get("RUN_LOCAL_E2E") != "1",
    reason="Set RUN_LOCAL_E2E=1 to run local E2E test",
)
def test_threat_map_local_server_mekar_raya_30km_e2e():
    base_url = os.environ.get("THREAT_MAP_API_BASE_URL", "http://127.0.0.1:8000")
    api_key = os.environ.get("CHM_API_KEY") or os.environ.get("API_KEY")
    if not api_key:
        pytest.skip("Set CHM_API_KEY or API_KEY to run local E2E test")

    geojson = _build_square_geojson_from_mekar_raya(side_km=30.0)

    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }

    create_response = requests.post(
        f"{base_url.rstrip('/')}/api/v1/threat-map/jobs",
        headers=headers,
        json={"geojson": geojson, "preset": "balanced"},
        timeout=120,
    )
    assert create_response.status_code == 202, create_response.text

    created = create_response.json()
    job_id = created.get("jobId")
    assert job_id

    deadline = time.monotonic() + 1800
    terminal_payload = None
    while time.monotonic() < deadline:
        status_response = requests.get(
            f"{base_url.rstrip('/')}/api/v1/threat-map/jobs/{job_id}",
            headers=headers,
            timeout=120,
        )
        assert status_response.status_code == 200, status_response.text
        payload = status_response.json()

        if payload.get("status") in {"succeeded", "partial_success", "failed", "cancelled"}:
            terminal_payload = payload
            break

        time.sleep(2.0)

    assert terminal_payload is not None, "Timed out waiting for terminal job status"
    assert terminal_payload["status"] in {"succeeded", "partial_success"}, json.dumps(terminal_payload, indent=2)

    download_response = requests.get(
        f"{base_url.rstrip('/')}/api/v1/threat-map/jobs/{job_id}/download",
        headers={"X-API-Key": api_key},
        timeout=600,
    )
    assert download_response.status_code == 200, download_response.text
    assert download_response.headers.get("Content-Type", "").startswith(("video/mp4", "application/zip"))
    assert len(download_response.content) > 0
