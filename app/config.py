from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", ".env.local"), env_file_encoding="utf-8", extra="ignore")

    app_name: str = "CHM Crop API"
    app_version: str = "0.1.0"

    # Security
    api_key: str = Field(default="", validation_alias=AliasChoices("CHM_API_KEY", "API_KEY", "CANOPY_API_KEY"))
    require_api_key: bool = True
    cors_origins: list[str] = ["http://localhost:3000"]
    trusted_hosts: list[str] = ["localhost", "127.0.0.1", "178.104.153.106", "chm-api.local"]

    # Data source
    s3_bucket: str = "dataforgood-fb-data"
    s3_path: str = "forests/v2/global/dinov3_global_chm_v2_ml3/"

    # CTrees AGB COG source
    ctrees_agb_s3_bucket: str = "ctrees-agb-100m-global"
    ctrees_agb_s3_prefix: str = "cogs/"

    # Operational limits
    max_geojson_bytes: int = 1_000_000
    max_aoi_area_km2: float = 1_000.0
    max_vertices: int = 50_000
    max_tiles_per_request: int = 8
    tile_index_ttl_seconds: int = 86_400
    download_workers: int = 2
    rate_limit_per_minute: int = 30
    aoi_square_side_km: float = Field(default=40.0, gt=0, description="Centroid AOI square side length in kilometers")

    # Local country boundary file used for exact Indonesia-only validation.
    indonesia_boundary_path: Path = Path("app/data/indonesia.geojson")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
