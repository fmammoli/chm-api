from pyproj import Transformer
from shapely.geometry import box
from shapely.ops import transform as shapely_transform

from app.services.chm_service import ServiceValidationError, _assert_output_extent_matches_aoi


def _square_wgs84(side_km: float):
    half_side_m = (side_km * 1000.0) / 2.0
    square_3857 = box(-half_side_m, -half_side_m, half_side_m, half_side_m)
    to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    return shapely_transform(to_4326.transform, square_3857)


def test_output_extent_matches_aoi_accepts_expected_bounds() -> None:
    geom = _square_wgs84(20.0)
    geom_3857 = shapely_transform(Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform, geom)

    _assert_output_extent_matches_aoi(
        geom=geom,
        output_crs="EPSG:3857",
        output_bounds=geom_3857.bounds,
        pixel_size_x=10.0,
        pixel_size_y=-10.0,
    )


def test_output_extent_matches_aoi_rejects_narrow_strip_extent() -> None:
    geom = _square_wgs84(20.0)
    geom_3857 = shapely_transform(Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform, geom)
    minx, miny, maxx, maxy = geom_3857.bounds

    # Simulate a collapsed strip by reducing width drastically.
    strip_bounds = (minx, miny, minx + (maxx - minx) * 0.1, maxy)

    try:
        _assert_output_extent_matches_aoi(
            geom=geom,
            output_crs="EPSG:3857",
            output_bounds=strip_bounds,
            pixel_size_x=10.0,
            pixel_size_y=-10.0,
        )
    except ServiceValidationError as exc:
        assert "does not fully cover requested AOI" in str(exc)
    else:
        raise AssertionError("expected extent validation to fail for strip-like output")
