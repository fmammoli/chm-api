from pyproj import Transformer
from shapely.geometry import box
from shapely.ops import transform as shapely_transform

from app.services.chm_service import ServiceValidationError, _validate_square_size


def _square_wgs84(side_km: float):
    half_side_m = (side_km * 1000.0) / 2.0
    square_3857 = box(-half_side_m, -half_side_m, half_side_m, half_side_m)
    to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    return shapely_transform(to_4326.transform, square_3857)


def test_validate_square_size_accepts_smaller_square() -> None:
    geom = _square_wgs84(20.0)

    _validate_square_size(geom, side_km=60.0)


def test_validate_square_size_rejects_oversized_square() -> None:
    geom = _square_wgs84(72.0)

    try:
        _validate_square_size(geom, side_km=60.0)
    except ServiceValidationError as exc:
        message = str(exc)
        assert "AOI square side is" in message
        assert "Maximum allowed is 60.0 km" in message
    else:
        raise AssertionError("expected AOI side validation to fail")


def test_validate_square_size_rejects_non_square() -> None:
    rect_3857 = box(-10000.0, -5000.0, 10000.0, 5000.0)
    to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    geom = shapely_transform(to_4326.transform, rect_3857)

    try:
        _validate_square_size(geom, side_km=60.0)
    except ServiceValidationError as exc:
        assert "AOI must be a square" in str(exc)
    else:
        raise AssertionError("expected AOI square-shape validation to fail")
