from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import get_settings
from app.models import (
    ChmJobCreateRequest,
    ChmJobCreateResponse,
    ChmJobStatusResponse,
    CtreesAgbCropRequest,
    CropRequest,
    ErrorBody,
    HealthResponse,
    LandcoverStatsJobCreateRequest,
    LandcoverStatsJobStatusResponse,
)
from app.security import require_api_key
from app.services.chm_service import (
    ServiceValidationError,
    build_cropped_ctrees_agb_raster,
    safe_rmtree,
    stream_file_chunks,
    validate_chm_request_payload,
)
from app.services.landcover_stats_service import validate_landcover_request_payload
from app.services.job_service import (
    create_job,
    get_job,
    get_queue_snapshot,
    job_output_path,
    reconcile_incomplete_jobs,
    run_chm_job,
    run_landcover_stats_job,
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

# Always keep in-memory logs for /api/v1/logs and also emit to terminal stdout.
if not any(isinstance(handler, InMemoryLogHandler) for handler in logger.handlers):
    logger.addHandler(log_handler)

if not any(getattr(handler, "_chm_console", False) for handler in logger.handlers):
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    setattr(console_handler, "_chm_console", True)
    logger.addHandler(console_handler)

logger.setLevel(logging.INFO)
logger.propagate = False

_JOB_ADMISSION_LOCK = Lock()
_CHM_JOB_SLOTS = BoundedSemaphore(value=max(1, settings.max_concurrent_chm_jobs))

_RATE_WINDOW_SECONDS = 60
_RATE_LIMIT_PER_IP = settings.rate_limit_per_minute
_ip_hits: dict[str, deque[float]] = defaultdict(deque)


def _admit_chm_job_or_reject(*, endpoint: str, message: str) -> tuple[dict[str, Any], dict[str, int]]:
    with _JOB_ADMISSION_LOCK:
        queue_before = get_queue_snapshot(settings)
        pending = queue_before["queued"] + queue_before["running"]
        if pending >= settings.max_pending_chm_jobs:
            logger.warning(
                "job_admission_rejected endpoint=%s pending=%s max_pending=%s queue_counts queued=%s running=%s succeeded=%s failed=%s",
                endpoint,
                pending,
                settings.max_pending_chm_jobs,
                queue_before["queued"],
                queue_before["running"],
                queue_before["succeeded"],
                queue_before["failed"],
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Job queue is full. Please retry in a few minutes.",
            )

        job = create_job(settings, message=message)
        queue_after = get_queue_snapshot(settings)
        return job, queue_after


def _run_chm_job_with_slot(settings, job_id: str, geojson_obj: dict[str, Any]) -> None:
    logger.info("job_worker_waiting_for_slot job_id=%s max_concurrent=%s", job_id, settings.max_concurrent_chm_jobs)
    _CHM_JOB_SLOTS.acquire()
    try:
        logger.info("job_worker_slot_acquired job_id=%s", job_id)
        run_chm_job(settings, job_id, geojson_obj)
    finally:
        _CHM_JOB_SLOTS.release()
        logger.info("job_worker_slot_released job_id=%s", job_id)


def _run_landcover_job_with_slot(
    settings,
    job_id: str,
    geojson_obj: dict[str, Any],
    baseline_year: int,
    comparison_year: int,
) -> None:
    logger.info("landcover_job_worker_waiting_for_slot job_id=%s max_concurrent=%s", job_id, settings.max_concurrent_chm_jobs)
    _CHM_JOB_SLOTS.acquire()
    try:
        logger.info("landcover_job_worker_slot_acquired job_id=%s", job_id)
        run_landcover_stats_job(
            settings,
            job_id,
            geojson_obj,
            baseline_year,
            comparison_year,
        )
    finally:
        _CHM_JOB_SLOTS.release()
        logger.info("landcover_job_worker_slot_released job_id=%s", job_id)

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


@app.on_event("startup")
def reconcile_jobs_on_startup() -> None:
    reconciled = reconcile_incomplete_jobs(settings)
    if reconciled > 0:
        logger.warning("reconciled_interrupted_jobs count=%s", reconciled)


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


@app.post("/api/v1/chm/jobs", response_model=ChmJobCreateResponse, status_code=status.HTTP_202_ACCEPTED)
def create_chm_job(
    request: Request,
    payload: ChmJobCreateRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_api_key),
):
    logger.info(
        "job_create_request_received path=%s client_ip=%s",
        request.url.path,
        request.client.host if request.client else "unknown",
    )
    validate_chm_request_payload(payload.geojson, settings)
    job, queue = _admit_chm_job_or_reject(endpoint="/api/v1/chm/jobs", message="CHM extraction job created")
    background_tasks.add_task(_run_chm_job_with_slot, settings, job["jobId"], payload.geojson)
    logger.info(
        "job_create_enqueued job_id=%s queue_counts queued=%s running=%s succeeded=%s failed=%s",
        job["jobId"],
        queue["queued"],
        queue["running"],
        queue["succeeded"],
        queue["failed"],
    )
    return ChmJobCreateResponse(
        jobId=job["jobId"],
        status=job["status"],
        message="CHM extraction job created",
    )


@app.get("/api/v1/chm/jobs/{job_id}", response_model=ChmJobStatusResponse)
def get_chm_job(
    request: Request,
    job_id: str,
    _: None = Depends(require_api_key),
):
    logger.info(
        "job_status_request_received path=%s job_id=%s client_ip=%s",
        request.url.path,
        job_id,
        request.client.host if request.client else "unknown",
    )
    job = get_job(settings, job_id)
    if job is None:
        logger.warning("job_status_not_found job_id=%s", job_id)
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=ErrorBody(message="Job not found").model_dump(),
        )
    logger.info(
        "job_status_response job_id=%s status=%s progress=%s",
        job_id,
        job.get("status"),
        job.get("progress"),
    )
    return ChmJobStatusResponse.model_validate(job)


@app.get("/api/v1/chm/jobs/{job_id}/download")
def download_chm_job_result(
    request: Request,
    job_id: str,
    _: None = Depends(require_api_key),
):
    logger.info(
        "job_download_request_received path=%s job_id=%s client_ip=%s",
        request.url.path,
        job_id,
        request.client.host if request.client else "unknown",
    )
    job = get_job(settings, job_id)
    if job is None:
        logger.warning("job_download_not_found job_id=%s", job_id)
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=ErrorBody(message="Job not found").model_dump(),
        )

    if job.get("status") != "succeeded":
        logger.info("job_download_blocked job_id=%s status=%s", job_id, job.get("status"))
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ErrorBody(message=f"Job is not complete. Current status: {job.get('status')}").model_dump(),
        )

    output_path = job_output_path(settings, job_id)
    if not output_path.exists():
        logger.warning("job_download_missing_output job_id=%s expected_path=%s", job_id, output_path)
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=ErrorBody(message="Job result file not found").model_dump(),
        )

    headers = {
        "Content-Disposition": f'attachment; filename="chm_{job_id}.tif"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    logger.info("job_download_streaming job_id=%s output_path=%s size_bytes=%s", job_id, output_path, output_path.stat().st_size)
    return StreamingResponse(stream_file_chunks(output_path), media_type="image/tiff", headers=headers)


@app.post("/api/v1/chm/crop", response_model=ChmJobCreateResponse, status_code=status.HTTP_202_ACCEPTED)
def crop_chm_compat(
    request: Request,
    payload: CropRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_api_key),
):
    # Legacy endpoint now enqueues a job instead of blocking on synchronous crop.
    logger.info(
        "legacy_crop_request_received path=%s client_ip=%s",
        request.url.path,
        request.client.host if request.client else "unknown",
    )
    validate_chm_request_payload(payload.geojson, settings)
    job, queue = _admit_chm_job_or_reject(
        endpoint="/api/v1/chm/crop",
        message="Legacy crop endpoint accepted; poll job status",
    )
    background_tasks.add_task(_run_chm_job_with_slot, settings, job["jobId"], payload.geojson)
    logger.info(
        "legacy_crop_redirected_to_job job_id=%s queue_counts queued=%s running=%s succeeded=%s failed=%s",
        job["jobId"],
        queue["queued"],
        queue["running"],
        queue["succeeded"],
        queue["failed"],
    )
    return ChmJobCreateResponse(
        jobId=job["jobId"],
        status=job["status"],
        message="CHM extraction job created. Use /api/v1/chm/jobs/{jobId} to poll and /download to fetch TIFF.",
    )


@app.post("/api/v1/landcover/stats/jobs", response_model=ChmJobCreateResponse, status_code=status.HTTP_202_ACCEPTED)
def create_landcover_stats_job(
    request: Request,
    payload: LandcoverStatsJobCreateRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_api_key),
):
    logger.info(
        "landcover_job_create_request_received path=%s client_ip=%s baseline_year=%s comparison_year=%s",
        request.url.path,
        request.client.host if request.client else "unknown",
        payload.baselineYear,
        payload.comparisonYear,
    )
    validate_landcover_request_payload(payload.geojson, settings)
    job, queue = _admit_chm_job_or_reject(
        endpoint="/api/v1/landcover/stats/jobs",
        message="Landcover stats job created",
    )
    background_tasks.add_task(
        _run_landcover_job_with_slot,
        settings,
        job["jobId"],
        payload.geojson,
        payload.baselineYear,
        payload.comparisonYear,
    )
    logger.info(
        "landcover_job_create_enqueued job_id=%s queue_counts queued=%s running=%s succeeded=%s failed=%s",
        job["jobId"],
        queue["queued"],
        queue["running"],
        queue["succeeded"],
        queue["failed"],
    )
    return ChmJobCreateResponse(
        jobId=job["jobId"],
        status=job["status"],
        message="Landcover stats job created",
    )


@app.get("/api/v1/landcover/stats/jobs/{job_id}", response_model=LandcoverStatsJobStatusResponse)
def get_landcover_stats_job(
    request: Request,
    job_id: str,
    _: None = Depends(require_api_key),
):
    logger.info(
        "landcover_job_status_request_received path=%s job_id=%s client_ip=%s",
        request.url.path,
        job_id,
        request.client.host if request.client else "unknown",
    )
    job = get_job(settings, job_id)
    if job is None:
        logger.warning("landcover_job_status_not_found job_id=%s", job_id)
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=ErrorBody(message="Job not found").model_dump(),
        )
    logger.info(
        "landcover_job_status_response job_id=%s status=%s progress=%s",
        job_id,
        job.get("status"),
        job.get("progress"),
    )
    return LandcoverStatsJobStatusResponse.model_validate(job)


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
