from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import get_settings
from app.models import CtreesAgbCropRequest, CropRequest, ErrorBody, HealthResponse
from app.security import require_api_key
from app.services.chm_service import (
    ServiceValidationError,
    build_cropped_ctrees_agb_raster,
    build_cropped_raster,
    safe_rmtree,
    stream_file_chunks,
)

# Configure logging to output to console
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)

settings = get_settings()
app = FastAPI(title=settings.app_name, version=settings.app_version)
logger = logging.getLogger("chm_api")
templates = Jinja2Templates(directory="app/templates")


class InMemoryLogHandler(logging.Handler):
    def __init__(self, max_entries: int = 500):
        super().__init__()
        self.max_entries = max_entries
        self._entries: deque[dict[str, object]] = deque(maxlen=max_entries)
        self._sequence = 0

    def emit(self, record: logging.LogRecord) -> None:
        self._sequence += 1
        self._entries.append(
            {
                "seq": self._sequence,
                "ts": time.time(),
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
            }
        )

    def tail(self, count: int = 200) -> list[dict[str, object]]:
        if count <= 0:
            return []
        return list(self._entries)[-count:]


log_handler = InMemoryLogHandler(500)
log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
logger.addHandler(log_handler)
logger.setLevel(logging.INFO)
logger.propagate = False

_RATE_WINDOW_SECONDS = 60
_RATE_LIMIT_PER_IP = settings.rate_limit_per_minute
_ip_hits: dict[str, deque[float]] = defaultdict(deque)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type", "X-API-Key"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)


@app.middleware("http")
async def basic_rate_limit(request: Request, call_next):
    # Lightweight in-memory rate limiter. Replace with Redis-backed limiter for multi-instance deployments.
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    hits = _ip_hits[client_ip]

    while hits and now - hits[0] > _RATE_WINDOW_SECONDS:
        hits.popleft()

    if len(hits) >= _RATE_LIMIT_PER_IP:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=ErrorBody(message="Too many requests").model_dump(),
        )

    hits.append(now)
    return await call_next(request)


@app.middleware("http")
async def request_debug_log(request: Request, call_next):
    response = await call_next(request)
    logger.info(
        "request method=%s path=%s status=%s has_x_api_key=%s content_length=%s",
        request.method,
        request.url.path,
        response.status_code,
        bool(request.headers.get("x-api-key")),
        request.headers.get("content-length", ""),
    )
    return response


@app.exception_handler(ServiceValidationError)
def validation_exception_handler(_: Request, exc: ServiceValidationError):
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=ErrorBody(message=str(exc)).model_dump())


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.get("/logs-ui", response_class=HTMLResponse)
async def logs_ui(request: Request):
    return templates.TemplateResponse(request, "logs_ui.html")


@app.get("/api/v1/logs")
async def stream_logs(
    request: Request,
    tail: int = 200,
    _: None = Depends(require_api_key),
):
    logger.info("streaming logs requested tail=%s", tail)

    async def event_stream():
        current_items = log_handler.tail(count=tail)
        for item in current_items:
            yield json.dumps(item, separators=(",", ":"), ensure_ascii=False) + "\n"

        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            if await request.is_disconnected():
                break

            latest_items = log_handler.tail(count=tail)
            if len(latest_items) > len(current_items):
                for item in latest_items[len(current_items) :]:
                    yield json.dumps(item, separators=(",", ":"), ensure_ascii=False) + "\n"
                current_items = latest_items
                break

            await asyncio.sleep(0.05)

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


@app.post("/api/v1/chm/crop")
def crop_chm(
    payload: CropRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_api_key),
):
    logger.info("🚀 Crop request received. geojson type=%s", type(payload.geojson))
    workdir = Path(tempfile.mkdtemp(prefix="chm_crop_"))
    logger.info("📁 Created temporary workdir=%s", workdir)
    try:
        logger.info("⏳ Starting raster processing...")
        result = build_cropped_raster(payload.geojson, settings=settings, workdir=workdir)
        logger.info("✅ Raster processing completed. output_path=%s crs=%s", result.output_path, result.crs)
    except ServiceValidationError as e:
        logger.warning("⚠️ Validation error: %s", str(e))
        safe_rmtree(workdir)
        raise
    except Exception as exc:  # pragma: no cover
        logger.error("❌ Processing failed: %s", str(exc), exc_info=True)
        safe_rmtree(workdir)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Raster processing failed") from exc

    headers = {
        "Content-Type": "image/tiff; application=geotiff",
        "Content-Disposition": 'inline; filename="canopy_height_output.tif"',
        "X-Raster-CRS": result.crs,
        "X-Raster-Bounds": ",".join(str(v) for v in result.bounds),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    background_tasks.add_task(safe_rmtree, workdir)
    logger.info("📤 Streaming response with %d bytes", result.output_path.stat().st_size)

    return StreamingResponse(
        stream_file_chunks(result.output_path),
        media_type="image/tiff",
        headers=headers,
    )


@app.post("/api/v1/ctrees/agb/crop")
def crop_ctrees_agb(
    payload: CtreesAgbCropRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_api_key),
):
    logger.info(
        "🚀 CTrees AGB crop request received. year=%s variable=%s geojson type=%s",
        payload.year,
        payload.variable,
        type(payload.geojson),
    )
    workdir = Path(tempfile.mkdtemp(prefix="ctrees_agb_crop_"))
    logger.info("📁 Created temporary workdir=%s", workdir)
    try:
        result = build_cropped_ctrees_agb_raster(
            payload.geojson,
            year=payload.year,
            variable=payload.variable,
            settings=settings,
            workdir=workdir,
        )
        logger.info("✅ CTrees AGB processing completed. output_path=%s crs=%s", result.output_path, result.crs)
    except ServiceValidationError:
        safe_rmtree(workdir)
        raise
    except Exception as exc:  # pragma: no cover
        logger.error("❌ CTrees AGB processing failed: %s", str(exc), exc_info=True)
        safe_rmtree(workdir)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="CTrees AGB processing failed") from exc

    headers = {
        "Content-Type": "image/tiff; application=geotiff",
        "Content-Disposition": f'inline; filename="ctrees_agb_{payload.variable}_{payload.year}.tif"',
        "X-Raster-CRS": result.crs,
        "X-Raster-Bounds": ",".join(str(v) for v in result.bounds),
        "X-Raster-Year": str(payload.year),
        "X-Raster-Variable": payload.variable,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    background_tasks.add_task(safe_rmtree, workdir)

    return StreamingResponse(
        stream_file_chunks(result.output_path),
        media_type="image/tiff",
        headers=headers,
    )
