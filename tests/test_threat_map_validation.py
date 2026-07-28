from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import ThreatMapJobCreateRequest
from app.services.chm_service import ServiceValidationError
from app.services.threat_map_service import validate_threat_map_request_payload


def _valid_geojson() -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [106.7, -6.4],
                            [107.0, -6.4],
                            [107.0, -6.1],
                            [106.7, -6.1],
                            [106.7, -6.4],
                        ]
                    ],
                },
                "properties": {},
            }
        ],
    }


def test_rejects_ultra_preset() -> None:
    settings = Settings()
    payload = ThreatMapJobCreateRequest(geojson=_valid_geojson(), geojsonCrs="EPSG:4326", preset="ultra")

    with pytest.raises(ServiceValidationError, match="disabled"):
        validate_threat_map_request_payload(payload, settings)


def test_rejects_2048_dimensions() -> None:
    settings = Settings()
    with pytest.raises(ValidationError, match="1024"):
        payload = ThreatMapJobCreateRequest(geojson=_valid_geojson(), geojsonCrs="EPSG:4326", width=2048)
        validate_threat_map_request_payload(payload, settings)


def test_rejects_high_when_disabled() -> None:
    settings = Settings(threat_map_allow_high_preset=False)
    payload = ThreatMapJobCreateRequest(geojson=_valid_geojson(), geojsonCrs="EPSG:4326", preset="high")

    with pytest.raises(ServiceValidationError, match="currently disabled"):
        validate_threat_map_request_payload(payload, settings)


def test_accepts_web_mercator_geojson_input() -> None:
    settings = Settings()
    payload = ThreatMapJobCreateRequest(
        geojson={
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [11877500.0, -715000.0],
                                [11906500.0, -715000.0],
                                [11906500.0, -686000.0],
                                [11877500.0, -686000.0],
                                [11877500.0, -715000.0],
                            ]
                        ],
                    },
                    "properties": {},
                }
            ],
        },
        geojsonCrs="EPSG:3857",
    )

    validated = validate_threat_map_request_payload(payload, settings)
    assert "options" in validated
