#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any

import pmtiles.reader as pm_reader
import requests

try:
    from app.config import get_settings
except Exception:
    get_settings = None  # type: ignore[assignment]


@dataclass
class RangeEvent:
    offset: int
    length: int
    source: str


def _resolve_year_url(
    *,
    year: int,
    base_url: str,
    url_template: str,
    year_1990_url: str,
    year_2024_url: str,
) -> str:
    if year == 1990 and year_1990_url:
        return year_1990_url
    if year == 2024 and year_2024_url:
        return year_2024_url
    return url_template.format(base_url=base_url.rstrip("/"), year=year)


class MetadataRangeFetcher:
    def __init__(
        self,
        *,
        url: str,
        cache_root: Path,
        timeout_seconds: float,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        self.cache_dir = cache_root / "ranges" / self.url_hash
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.events: list[RangeEvent] = []
        self.bytes_downloaded = 0
        self.bytes_from_cache = 0

    def _range_path(self, offset: int, length: int) -> Path:
        return self.cache_dir / f"{offset}_{length}.bin"

    def _read_cache(self, path: Path, expected_length: int) -> bytes | None:
        try:
            payload = path.read_bytes()
        except OSError:
            return None
        if len(payload) != expected_length:
            return None
        return payload

    def _write_cache(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(delete=False, dir=str(path.parent)) as tmp_file:
            tmp_file.write(payload)
            tmp_path = Path(tmp_file.name)
        tmp_path.replace(path)

    def get_bytes(self, offset: int, length: int) -> bytes:
        cached_path = self._range_path(offset, length)
        cached = self._read_cache(cached_path, length)
        if cached is not None:
            self.events.append(RangeEvent(offset=offset, length=length, source="disk_cache"))
            self.bytes_from_cache += len(cached)
            return cached

        headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
        response = self.session.get(self.url, headers=headers, timeout=self.timeout_seconds)
        response.raise_for_status()
        payload = response.content
        if len(payload) != length:
            raise RuntimeError(
                f"PMTiles range read length mismatch for {self.url}: "
                f"requested={length} received={len(payload)}"
            )

        self._write_cache(cached_path, payload)
        self.events.append(RangeEvent(offset=offset, length=length, source="network"))
        self.bytes_downloaded += len(payload)
        return payload


def _unique_ranges(events: list[RangeEvent]) -> list[dict[str, int]]:
    seen: set[tuple[int, int]] = set()
    out: list[dict[str, int]] = []
    for event in events:
        key = (event.offset, event.length)
        if key in seen:
            continue
        seen.add(key)
        out.append({"offset": event.offset, "length": event.length})
    out.sort(key=lambda item: (item["offset"], item["length"]))
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prewarm PMTiles metadata/index byte-range cache (header + metadata) "
            "to speed first-read discovery for immutable bucket sources."
        )
    )
    parser.add_argument("--cache-root", default="outputs/pmtiles-range-cache")
    parser.add_argument("--year-start", type=int, default=1990)
    parser.add_argument("--year-end", type=int, default=2024)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--url-template", default=None)
    parser.add_argument("--year-1990-url", default=None)
    parser.add_argument("--year-2024-url", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--manifest-out", default=None)
    parser.add_argument(
        "--urls-json",
        default=None,
        help="Optional JSON file containing an array of PMTiles URLs to prewarm directly.",
    )
    return parser.parse_args()


def _resolve_urls(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.urls_json:
        urls_path = Path(args.urls_json)
        raw = json.loads(urls_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise SystemExit("--urls-json must contain a JSON array of URL strings")
        urls = [str(item).strip() for item in raw if str(item).strip()]
        return [{"year": None, "url": url} for url in urls]

    settings = get_settings() if get_settings is not None else None
    base_url = args.base_url or (settings.threat_map_landcover_base_url if settings else "")
    url_template = args.url_template or (
        settings.threat_map_landcover_url_template if settings else "{base_url}/{year}_landcover.pmtiles"
    )
    year_1990_url = args.year_1990_url
    if year_1990_url is None and settings is not None:
        year_1990_url = settings.threat_map_landcover_year_1990_url
    year_2024_url = args.year_2024_url
    if year_2024_url is None and settings is not None:
        year_2024_url = settings.threat_map_landcover_year_2024_url

    if not base_url and "{base_url}" in url_template:
        raise SystemExit("No base URL configured. Pass --base-url or provide --urls-json.")

    start_year = min(args.year_start, args.year_end)
    end_year = max(args.year_start, args.year_end)
    years = list(range(start_year, end_year + 1))

    output: list[dict[str, Any]] = []
    for year in years:
        output.append(
            {
                "year": year,
                "url": _resolve_year_url(
                    year=year,
                    base_url=base_url,
                    url_template=url_template,
                    year_1990_url=year_1990_url or "",
                    year_2024_url=year_2024_url or "",
                ),
            }
        )
    return output


def main() -> int:
    args = parse_args()
    cache_root = Path(args.cache_root)
    cache_root.mkdir(parents=True, exist_ok=True)

    targets = _resolve_urls(args)
    if not targets:
        raise SystemExit("No PMTiles URLs resolved.")

    started = time.time()
    per_url: list[dict[str, Any]] = []

    print(f"Prewarming metadata byte-ranges for {len(targets)} PMTiles URL(s)")
    print(f"Cache root: {cache_root}")

    for idx, target in enumerate(targets, start=1):
        year = target["year"]
        url = target["url"]
        label = f"year={year}" if year is not None else f"url#{idx}"
        print(f"[{idx}/{len(targets)}] {label} -> {url}")

        fetcher = MetadataRangeFetcher(
            url=url,
            cache_root=cache_root,
            timeout_seconds=args.timeout_seconds,
        )
        reader = pm_reader.Reader(fetcher.get_bytes)

        header = reader.header()
        metadata = reader.metadata()

        event_counts: dict[str, int] = {"network": 0, "disk_cache": 0}
        for event in fetcher.events:
            event_counts[event.source] = event_counts.get(event.source, 0) + 1

        url_result = {
            "year": year,
            "url": url,
            "urlHash": fetcher.url_hash,
            "pmtiles": {
                "minZoom": int(header.get("min_zoom", 0)),
                "maxZoom": int(header.get("max_zoom", 0)),
                "tileType": str(getattr(header.get("tile_type"), "name", header.get("tile_type"))),
            },
            "metadataKeys": sorted(list(metadata.keys())) if isinstance(metadata, dict) else [],
            "rangeEvents": [{"offset": e.offset, "length": e.length, "source": e.source} for e in fetcher.events],
            "uniqueRanges": _unique_ranges(fetcher.events),
            "rangeEventCounts": event_counts,
            "bytesDownloaded": fetcher.bytes_downloaded,
            "bytesFromCache": fetcher.bytes_from_cache,
        }
        per_url.append(url_result)

        print(
            f"  ranges(unique)={len(url_result['uniqueRanges'])} "
            f"network={event_counts.get('network', 0)} "
            f"disk={event_counts.get('disk_cache', 0)} "
            f"downloaded={fetcher.bytes_downloaded}B"
        )

    totals = {
        "bytesDownloaded": sum(item["bytesDownloaded"] for item in per_url),
        "bytesFromCache": sum(item["bytesFromCache"] for item in per_url),
        "uniqueRangesTotal": sum(len(item["uniqueRanges"]) for item in per_url),
    }

    manifest = {
        "version": 1,
        "purpose": "pmtiles_metadata_discovery_cache",
        "createdAtEpoch": started,
        "elapsedSeconds": round(time.time() - started, 3),
        "cacheRoot": str(cache_root),
        "targets": per_url,
        "totals": totals,
    }

    manifest_path = Path(args.manifest_out) if args.manifest_out else (cache_root / "metadata_prewarm_manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Manifest written: {manifest_path}")
    print(
        "Totals: "
        f"downloaded={totals['bytesDownloaded']}B "
        f"from_cache={totals['bytesFromCache']}B "
        f"unique_ranges={totals['uniqueRangesTotal']}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
