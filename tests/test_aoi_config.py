from app.config import Settings


def test_default_aoi_square_side_is_40_km() -> None:
    settings = Settings()

    assert settings.aoi_square_side_km == 40.0
