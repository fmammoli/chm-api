#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import rasterio


def read_metadata(path: Path) -> dict[str, Any]:
    with rasterio.open(path) as ds:
        arr = ds.read(1, masked=True)
        if np.ma.isMaskedArray(arr) and arr.count() > 0:
            min_val = float(arr.min())
            max_val = float(arr.max())
        else:
            min_val = None
            max_val = None

        compression = ds.profile.get("compress")
        block_shapes = ds.block_shapes[0] if ds.block_shapes else None
        transform = ds.transform

        return {
            "path": str(path),
            "width": ds.width,
            "height": ds.height,
            "dtype": ds.dtypes[0],
            "nodata": ds.nodata,
            "count": ds.count,
            "crs": str(ds.crs),
            "bounds": [float(v) for v in ds.bounds],
            "pixel_size": [float(transform.a), float(transform.e)],
            "tiled": bool(ds.is_tiled),
            "block_shape": list(block_shapes) if block_shapes else None,
            "compression": compression,
            "overviews": ds.overviews(1),
            "band_min": min_val,
            "band_max": max_val,
        }


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "width",
        "height",
        "dtype",
        "nodata",
        "compression",
        "tiled",
        "block_shape",
        "overviews",
        "band_min",
        "band_max",
        "crs",
        "pixel_size",
    ]
    return {key: {"before": before.get(key), "after": after.get(key)} for key in keys}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate CHM GeoTIFF metadata and optionally compare before/after outputs."
    )
    parser.add_argument("after", type=Path, help="Path to output raster after backend fixes")
    parser.add_argument(
        "--before",
        type=Path,
        default=None,
        help="Optional path to baseline raster before backend fixes",
    )
    args = parser.parse_args()

    if not args.after.exists():
        raise SystemExit(f"After file not found: {args.after}")

    after_meta = read_metadata(args.after)
    print("AFTER")
    print(json.dumps(after_meta, indent=2))

    checks = {
        "has_tiling": after_meta["tiled"],
        "has_compression": bool(after_meta["compression"]),
        "has_nodata": after_meta["nodata"] is not None,
        "has_overviews": len(after_meta["overviews"]) > 0,
        "has_valid_stats": after_meta["band_min"] is not None and after_meta["band_max"] is not None,
        "has_crs": after_meta["crs"] not in {"", "None"},
    }
    print("\nCHECKS")
    print(json.dumps(checks, indent=2))

    if args.before is not None:
        if not args.before.exists():
            raise SystemExit(f"Before file not found: {args.before}")
        before_meta = read_metadata(args.before)
        print("\nBEFORE")
        print(json.dumps(before_meta, indent=2))

        print("\nCOMPARISON")
        print(json.dumps(compare(before_meta, after_meta), indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
