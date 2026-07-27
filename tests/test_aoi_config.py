from app.config import Settings


def test_default_aoi_square_side_is_20_km() -> None:
    settings = Settings()

    assert settings.aoi_square_side_km == 20.0
