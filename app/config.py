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

    # MapBiomas landcover source for annual stats.
    landcover_base_url: str = "https://pub-b35b693f4e7a4112af656d6983f8adc2.r2.dev/landcover-mapbiomas-pmtiles-values"
    landcover_url_template: str = "{base_url}/{year}_landcover.pmtiles"
    landcover_year_1990_url: str = ""
    landcover_year_2024_url: str = ""
    landcover_forest_classes: list[int] = [3, 5, 76]
    landcover_planted_forest_classes: list[int] = []
    landcover_forest_colors: list[str] = ["1f8d49"]
    landcover_pmtiles_zoom: int = Field(default=12, ge=0, le=30)

    # Operational limits
    max_geojson_bytes: int = 1_000_000
    max_aoi_area_km2: float = 1_600.0
    max_vertices: int = 50_000
    max_tiles_per_request: int = 16
    tile_index_ttl_seconds: int = 86_400
    download_workers: int = 2
    rate_limit_per_minute: int = 30
    aoi_square_side_km: float = Field(default=30.0, gt=0, description="Maximum AOI square side length in kilometers")
    max_concurrent_chm_jobs: int = Field(default=1, ge=1, description="Maximum CHM jobs processed concurrently per API process")
    max_pending_chm_jobs: int = Field(default=6, ge=1, description="Maximum queued+running CHM jobs before rejecting new submissions")

    # Threat-map low-resource export pipeline defaults (1 vCPU / 4GB RAM target).
    threat_map_enabled: bool = True
    threat_map_max_active_jobs: int = Field(default=1, ge=1, le=1)
    threat_map_max_queue_length: int = Field(default=4, ge=1, le=32)
    threat_map_tile_fetch_concurrency: int = Field(default=2, ge=1, le=2)
    threat_map_default_preset: str = "balanced"
    threat_map_allow_high_preset: bool = False
    threat_map_enable_server_mp4_generation: bool = Field(default=False, description="Disable server-side MP4 generation so the frontend can own video encoding")
    threat_map_balanced_size: int = Field(default=768, ge=256, le=1024)
    threat_map_high_size: int = Field(default=1024, ge=256, le=1024)
    threat_map_max_size: int = Field(default=1024, ge=256, le=1024)
    threat_map_default_fps: float = Field(default=0.67, gt=0.05, le=2.0)
    threat_map_default_frame_duration_seconds: float = Field(default=1.5, gt=0.25, le=5.0)
    threat_map_ffmpeg_preset: str = "ultrafast"
    threat_map_ffmpeg_crf: int = Field(default=32, ge=18, le=40)
    threat_map_request_timeout_seconds: int = Field(default=240, ge=10, le=3600)
    threat_map_year_timeout_seconds: int = Field(default=90, ge=5, le=1200)
    threat_map_retry_max_attempts: int = Field(default=5, ge=1, le=10)
    threat_map_retry_base_delay_seconds: float = Field(default=0.5, gt=0.0, le=10.0)
    threat_map_retry_max_delay_seconds: float = Field(default=8.0, gt=0.0, le=120.0)
    threat_map_memory_rss_limit_mb: int = Field(default=2560, ge=256, le=3584)
    threat_map_low_resource_mode: bool = Field(default=True, description="Reduce output size and PMTiles zoom for low-resource deployments")
    threat_map_low_resource_zoom: int = Field(default=10, ge=0, le=20)
    threat_map_low_resource_width: int = Field(default=512, ge=256, le=1024)
    threat_map_low_resource_height: int = Field(default=512, ge=256, le=1024)
    threat_map_low_resource_max_size: int = Field(default=512, ge=256, le=1024)
    threat_map_low_resource_fps: float = Field(default=0.5, gt=0.05, le=2.0)
    threat_map_low_resource_frame_duration_seconds: float = Field(default=1.5, gt=0.25, le=5.0)
    threat_map_landcover_base_url: str = "https://pub-b35b693f4e7a4112af656d6983f8adc2.r2.dev/landcover-mapbiomas-pmtiles"
    threat_map_landcover_url_template: str = "{base_url}/{year}_landcover.pmtiles"
    threat_map_landcover_year_1990_url: str = ""
    threat_map_landcover_year_2024_url: str = ""
    threat_map_temp_root: Path = Path("/tmp/chm-api-threat-map")
    threat_map_legend_manifest_path: Path = Path("app/data/legends/mekar_raya_legend_manifest.json")
    threat_map_legend_colors_path: Path = Path("app/data/legends/mapbiomas-colors.txt")

    # Local durable job storage for async CHM extraction.
    jobs_dir: Path = Path("jobs")
    outputs_dir: Path = Path("outputs")

    # Local country boundary file used for exact Indonesia-only validation.
    indonesia_boundary_path: Path = Path("app/data/indonesia.geojson")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
