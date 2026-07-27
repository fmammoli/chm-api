from typing import Any
from typing import Literal

from pydantic import BaseModel, Field


class CropRequest(BaseModel):
    geojson: dict[str, Any] = Field(description="GeoJSON Feature or FeatureCollection")


class CtreesAgbCropRequest(BaseModel):
    geojson: dict[str, Any] = Field(description="GeoJSON Feature or FeatureCollection")
    year: int = Field(ge=2000, le=2025, description="AGB year to crop (2000-2025)")
    variable: Literal["agb", "uncertainty"] = Field(default="agb", description="Dataset variable to crop")


class ErrorBody(BaseModel):
    message: str


class HealthResponse(BaseModel):
    status: str = "ok"
