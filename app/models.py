from datetime import datetime
from enum import Enum
from typing import Any
from typing import Literal

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class CropRequest(BaseModel):
    geojson: dict[str, Any] = Field(description="GeoJSON Feature or FeatureCollection")


class ChmJobCreateRequest(BaseModel):
    geojson: dict[str, Any] = Field(description="GeoJSON FeatureCollection")


class ChmJobCreateResponse(BaseModel):
    jobId: str
    status: JobStatus
    message: str


class ChmJobResult(BaseModel):
    downloadUrl: str
    contentType: str = "image/tiff"


class ChmJobError(BaseModel):
    code: str
    message: str


class ChmJobStatusResponse(BaseModel):
    jobId: str
    status: JobStatus
    createdAt: datetime
    startedAt: datetime | None = None
    finishedAt: datetime | None = None
    progress: int | None = None
    etaSeconds: int | None = None
    message: str | None = None
    result: ChmJobResult | None = None
    error: ChmJobError | None = None


class CtreesAgbCropRequest(BaseModel):
    geojson: dict[str, Any] = Field(description="GeoJSON Feature or FeatureCollection")
    year: int = Field(ge=2000, le=2025, description="AGB year to crop (2000-2025)")
    variable: Literal["agb", "uncertainty"] = Field(default="agb", description="Dataset variable to crop")


class LandcoverStatsJobCreateRequest(BaseModel):
    geojson: dict[str, Any] = Field(description="GeoJSON Feature or FeatureCollection")
    baselineYear: int = Field(default=1990, ge=1985, le=2024, description="Baseline landcover year")
    comparisonYear: int = Field(default=2024, ge=1985, le=2024, description="Comparison landcover year")


class LandcoverStatsResult(BaseModel):
    baselineYear: int
    comparisonYear: int
    forestLossHa: float
    forestGainHa: float
    forestLossPct: float | None = None
    forestGainPct: float | None = None
    netForestChangeHa: float
    baselineForestAreaHa: float
    comparisonForestAreaHa: float
    analyzedAreaHa: float
    aoiAreaHa: float
    coverageFraction: float
    validPixelCount: int
    metadata: dict[str, Any] | None = None


class LandcoverStatsJobStatusResponse(BaseModel):
    jobId: str
    status: JobStatus
    createdAt: datetime
    startedAt: datetime | None = None
    finishedAt: datetime | None = None
    progress: int | None = None
    etaSeconds: int | None = None
    message: str | None = None
    result: LandcoverStatsResult | None = None
    error: ChmJobError | None = None


class ErrorBody(BaseModel):
    message: str


class HealthResponse(BaseModel):
    status: str = "ok"
