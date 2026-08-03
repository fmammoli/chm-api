from datetime import datetime
from enum import Enum
from typing import Any
from typing import Literal

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    queued = "queued"
    deferred = "deferred"
    running = "running"
    succeeded = "succeeded"
    partial_success = "partial_success"
    failed = "failed"
    cancelled = "cancelled"


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


class AgbStatsJobCreateRequest(BaseModel):
    geojson: dict[str, Any] = Field(description="GeoJSON Feature or FeatureCollection")


class AgbThresholdCoverage(BaseModel):
    thresholdMgHa: float
    coverRatio: float
    coverPercent: float
    coverAreaHa: float


class AgbStatsResult(BaseModel):
    baselineYear: int
    comparisonYear: int
    minAgbMgHa: float
    maxAgbMgHa: float
    meanAgbMgHa: float
    medianAgbMgHa: float
    stdDevAgbMgHa: float
    varianceAgbMgHa2: float
    p10AgbMgHa: float
    p25AgbMgHa: float
    p75AgbMgHa: float
    p90AgbMgHa: float
    p95AgbMgHa: float
    interquartileRangeMgHa: float
    coefficientOfVariation: float
    totalAgbMg: float
    totalAgbMgHa: float
    baselineTotalAgbMg: float
    comparisonTotalAgbMg: float
    agbIncreaseMg: float
    agbDecreaseMg: float
    netChangeAgbMg: float
    netChangeAgbMgHa: float
    netChangePercent: float
    agbIncreaseAreaHa: float
    agbDecreaseAreaHa: float
    analyzedAreaHa: float
    aoiAreaHa: float
    coverageFraction: float
    validPixelCount: int
    agbCoverByThreshold: list[AgbThresholdCoverage]
    metadata: dict[str, Any] | None = None


class AgbStatsJobStatusResponse(BaseModel):
    jobId: str
    status: JobStatus
    createdAt: datetime
    startedAt: datetime | None = None
    finishedAt: datetime | None = None
    progress: int | None = None
    etaSeconds: int | None = None
    message: str | None = None
    result: AgbStatsResult | None = None
    error: ChmJobError | None = None


class ChmStatsJobCreateRequest(BaseModel):
    geojson: dict[str, Any] = Field(description="GeoJSON FeatureCollection")
    canopyThresholdsM: list[float] | None = Field(
        default=None,
        description="Optional canopy-height thresholds in meters used to compute cover ratios",
    )


class ChmThresholdCoverage(BaseModel):
    thresholdM: float
    coverRatio: float
    coverPercent: float
    coverAreaHa: float


class ChmRangeCoverage(BaseModel):
    lowerBoundM: float | None = None
    upperBoundM: float | None = None
    label: str
    coverRatio: float
    coverPercent: float
    coverAreaHa: float


class ChmStatsResult(BaseModel):
    minCanopyHeightM: float
    maxCanopyHeightM: float
    meanCanopyHeightM: float
    medianCanopyHeightM: float
    stdDevCanopyHeightM: float
    varianceCanopyHeightM2: float
    p10CanopyHeightM: float
    p25CanopyHeightM: float
    p75CanopyHeightM: float
    p90CanopyHeightM: float
    p95CanopyHeightM: float
    interquartileRangeM: float
    coefficientOfVariation: float
    totalCanopyVolumeProxyM3: float
    analyzedAreaHa: float
    aoiAreaHa: float
    coverageFraction: float
    validPixelCount: int
    canopyCoverByThreshold: list[ChmThresholdCoverage]
    canopyCoverByRange: list[ChmRangeCoverage] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


class ChmStatsJobStatusResponse(BaseModel):
    jobId: str
    status: JobStatus
    createdAt: datetime
    startedAt: datetime | None = None
    finishedAt: datetime | None = None
    progress: int | None = None
    etaSeconds: int | None = None
    message: str | None = None
    result: ChmStatsResult | None = None
    error: ChmJobError | None = None


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


class ThreatMapPreset(str, Enum):
    balanced = "balanced"
    high = "high"
    ultra = "ultra"


class ThreatMapOverlayStyle(BaseModel):
    strokeColor: str | None = None
    strokeWidth: int | None = Field(default=None, ge=1, le=12)
    fillColor: str | None = None
    fillOpacity: float | None = Field(default=None, ge=0.0, le=1.0)
    markerColor: str | None = None
    markerOutlineColor: str | None = None
    markerSize: int | None = Field(default=None, ge=2, le=24)
    labelColor: str | None = None
    labelBgColor: str | None = None


class ThreatMapOverlayLayer(BaseModel):
    id: str
    label: str
    geojson: dict[str, Any] = Field(description="Overlay layer GeoJSON")
    geojsonCrs: Literal["EPSG:4326", "EPSG:3857"] = Field(default="EPSG:3857")
    style: ThreatMapOverlayStyle = Field(default_factory=ThreatMapOverlayStyle)
    showInLegend: bool = True
    legendOrder: int = 100


class ThreatMapJobCreateRequest(BaseModel):
    geojson: dict[str, Any] = Field(description="GeoJSON Feature or FeatureCollection")
    geojsonCrs: Literal["EPSG:4326", "EPSG:3857"] = Field(default="EPSG:3857")
    overlayGeojson: dict[str, Any] | None = Field(default=None, description="Optional overlay GeoJSON to draw on each frame")
    overlayGeojsonCrs: Literal["EPSG:4326", "EPSG:3857"] = Field(default="EPSG:3857")
    overlayPointGeojson: dict[str, Any] | None = Field(default=None, description="Optional point GeoJSON to draw on each frame")
    overlayPointName: str | None = Field(default=None, description="Optional label for the overlay point")
    overlayPointCrs: Literal["EPSG:4326", "EPSG:3857"] = Field(default="EPSG:3857")
    overlayLayers: list[ThreatMapOverlayLayer] | None = Field(
        default=None,
        description="Optional styled overlay layers (polygon/point) with legend metadata",
    )
    preset: ThreatMapPreset = Field(default=ThreatMapPreset.balanced)
    width: int | None = Field(default=None, ge=64, le=1024)
    height: int | None = Field(default=None, ge=64, le=1024)
    fps: float | None = Field(default=None, gt=0.05, le=2.0)
    frameDurationSeconds: float | None = Field(default=None, gt=0.25, le=5.0)
    outputFormat: Literal["mp4", "frames_tar_gz"] = Field(default="frames_tar_gz")


class ThreatMapArtifact(str, Enum):
    mp4 = "mp4"
    zip = "zip"
    frames_tar_gz = "frames_tar_gz"


class ThreatMapJobResult(BaseModel):
    downloadUrl: str
    contentType: str
    artifactType: ThreatMapArtifact
    sizeBytes: int
    yearsRendered: int
    yearsExpected: int
    fallbackReasonCode: str | None = None


class ThreatMapJobStatusResponse(BaseModel):
    jobId: str
    status: JobStatus
    createdAt: datetime
    startedAt: datetime | None = None
    finishedAt: datetime | None = None
    progress: int | None = None
    etaSeconds: int | None = None
    currentYear: int | None = None
    message: str | None = None
    warnings: list[str] = Field(default_factory=list)
    result: ThreatMapJobResult | None = None
    error: ChmJobError | None = None


class ErrorBody(BaseModel):
    message: str


class HealthResponse(BaseModel):
    status: str = "ok"
