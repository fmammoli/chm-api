#!/usr/bin/env python3
"""Smoke-test the landcover stats API with a sample GeoJSON AOI.

Examples:
    export CHM_API_KEY="your-api-key"
    python scripts/test_landcover_stats_api.py

    python scripts/test_landcover_stats_api.py --base-url http://127.0.0.1:8000 --api-key your-api-key
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any


DEFAULT_BASE_URL = os.environ.get("LANDCOVER_API_BASE_URL", "http://127.0.0.1:8000")
DEFAULT_API_KEY = os.environ.get("CHM_API_KEY") or os.environ.get("API_KEY") or ""


SAMPLE_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [106.7, -6.4],
                        [106.8, -6.4],
                        [106.8, -6.3],
                        [106.7, -6.3],
                        [106.7, -6.4],
                    ]
                ],
            },
        }
    ],
}


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


def create_job(base_url: str, api_key: str) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/v1/landcover/stats/jobs"
    payload = {
        "geojson": SAMPLE_GEOJSON,
        "baselineYear": 1990,
        "comparisonYear": 2024,
    }
    status, body = request_json("POST", url, api_key=api_key, payload=payload)
    print(f"Create job response [{status}]:")
    print(json.dumps(body, indent=2))
    return body


def poll_job(base_url: str, api_key: str, job_id: str, *, timeout_seconds: int = 600, poll_interval: float = 2.0) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/v1/landcover/stats/jobs/{job_id}"
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        status, body = request_json("GET", url, api_key=api_key)
        print(f"Poll status [{status}]:")
        print(json.dumps(body, indent=2))

        status_value = str(body.get("status", "")).lower()
        if status_value in {"succeeded", "failed"}:
            return body

        time.sleep(poll_interval)

    raise TimeoutError(f"Timed out waiting for job {job_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the landcover stats API")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API base URL (default: %(default)s)")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="API key for X-API-Key header")
    parser.add_argument("--timeout-seconds", type=int, default=600, help="Max wait for completion")
    args = parser.parse_args()

    if not args.api_key:
        print("Missing API key. Provide --api-key or set CHM_API_KEY/API_KEY.", file=sys.stderr)
        return 2

    try:
        created = create_job(args.base_url, args.api_key)
        job_id = created.get("jobId")
        if not job_id:
            print("No jobId returned from create endpoint", file=sys.stderr)
            return 1

        result = poll_job(args.base_url, args.api_key, job_id, timeout_seconds=args.timeout_seconds)
        print("Final job status:")
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:  # pragma: no cover - CLI error path
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
