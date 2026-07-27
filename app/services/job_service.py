from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from app.config import Settings
from app.models import JobStatus
from app.services.chm_service import ServiceValidationError, build_cropped_raster, safe_rmtree

logger = logging.getLogger("chm_api")

_WRITE_LOCK = Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _estimate_eta_seconds(started_at: str | None, progress: int | None) -> int | None:
    if started_at is None or progress is None:
        return None
    if progress <= 0 or progress >= 100:
        return None

    started_at_dt = _parse_utc_iso(started_at)
    if started_at_dt is None:
        return None

    elapsed_seconds = (datetime.now(timezone.utc) - started_at_dt).total_seconds()
    if elapsed_seconds <= 0:
        return None

    remaining_seconds = elapsed_seconds * (100 - progress) / progress
    return max(0, int(round(remaining_seconds)))


def _ensure_dirs(settings: Settings) -> None:
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)


def _job_file_path(settings: Settings, job_id: str) -> Path:
    return settings.jobs_dir / f"{job_id}.json"


def job_output_path(settings: Settings, job_id: str) -> Path:
    return settings.outputs_dir / f"{job_id}.tif"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as tmp_file:
        json.dump(payload, tmp_file, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _write_job(settings: Settings, payload: dict[str, Any]) -> None:
    _ensure_dirs(settings)
    with _WRITE_LOCK:
        _atomic_write_json(_job_file_path(settings, payload["jobId"]), payload)


def _read_job_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def get_job(settings: Settings, job_id: str) -> dict[str, Any] | None:
    _ensure_dirs(settings)
    return _read_job_file(_job_file_path(settings, job_id))


def get_queue_snapshot(settings: Settings) -> dict[str, int]:
    _ensure_dirs(settings)
    counts = {
        JobStatus.queued.value: 0,
        JobStatus.running.value: 0,
        JobStatus.succeeded.value: 0,
        JobStatus.failed.value: 0,
    }
    for file_path in settings.jobs_dir.glob("*.json"):
        payload = _read_job_file(file_path)
        if payload is None:
            continue
        status = str(payload.get("status", ""))
        if status in counts:
            counts[status] += 1
    return counts


def _merge_job(settings: Settings, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    path = _job_file_path(settings, job_id)
    with _WRITE_LOCK:
        current = _read_job_file(path)
        if current is None:
            raise FileNotFoundError(f"job {job_id} not found")
        current.update(updates)
        _atomic_write_json(path, current)
    return current


def update_job_progress(settings: Settings, job_id: str, *, progress: int, message: str | None = None) -> dict[str, Any]:
    current_job = get_job(settings, job_id)
    if current_job is None:
        raise FileNotFoundError(f"job {job_id} not found")

    eta_seconds = _estimate_eta_seconds(current_job.get("startedAt"), progress)
    updates: dict[str, Any] = {
        "progress": progress,
        "etaSeconds": eta_seconds,
    }
    if message is not None:
        updates["message"] = message
    job = _merge_job(settings, job_id, updates)
    logger.info(
        "job_progress_updated job_id=%s progress=%s eta_seconds=%s message=%s",
        job_id,
        progress,
        eta_seconds,
        message or "",
    )
    return job


def create_job(settings: Settings, *, message: str = "CHM extraction job created") -> dict[str, Any]:
    _ensure_dirs(settings)
    job_id = str(uuid4())
    payload: dict[str, Any] = {
        "jobId": job_id,
        "status": JobStatus.queued.value,
        "createdAt": _utc_now_iso(),
        "startedAt": None,
        "finishedAt": None,
        "progress": 0,
        "etaSeconds": None,
        "message": message,
        "result": None,
        "error": None,
    }
    _write_job(settings, payload)
    queue = get_queue_snapshot(settings)
    logger.info(
        "job_queued job_id=%s queue_counts queued=%s running=%s succeeded=%s failed=%s",
        job_id,
        queue[JobStatus.queued.value],
        queue[JobStatus.running.value],
        queue[JobStatus.succeeded.value],
        queue[JobStatus.failed.value],
    )
    return payload


def mark_job_running(settings: Settings, job_id: str) -> dict[str, Any]:
    job = _merge_job(
        settings,
        job_id,
        {
            "status": JobStatus.running.value,
            "startedAt": _utc_now_iso(),
            "finishedAt": None,
            "progress": 10,
            "etaSeconds": None,
            "message": "CHM extraction running",
            "error": None,
        },
    )
    queue = get_queue_snapshot(settings)
    logger.info(
        "job_running job_id=%s queue_counts queued=%s running=%s succeeded=%s failed=%s",
        job_id,
        queue[JobStatus.queued.value],
        queue[JobStatus.running.value],
        queue[JobStatus.succeeded.value],
        queue[JobStatus.failed.value],
    )
    return job


def mark_job_failed(settings: Settings, job_id: str, *, code: str, message: str) -> dict[str, Any]:
    job = _merge_job(
        settings,
        job_id,
        {
            "status": JobStatus.failed.value,
            "finishedAt": _utc_now_iso(),
            "progress": None,
            "etaSeconds": None,
            "message": "CHM generation failed",
            "result": None,
            "error": {"code": code, "message": message},
        },
    )
    queue = get_queue_snapshot(settings)
    logger.warning(
        "job_failed job_id=%s code=%s message=%s queue_counts queued=%s running=%s succeeded=%s failed=%s",
        job_id,
        code,
        message,
        queue[JobStatus.queued.value],
        queue[JobStatus.running.value],
        queue[JobStatus.succeeded.value],
        queue[JobStatus.failed.value],
    )
    return job


def mark_job_succeeded(settings: Settings, job_id: str, *, output_file_size: int) -> dict[str, Any]:
    job = _merge_job(
        settings,
        job_id,
        {
            "status": JobStatus.succeeded.value,
            "finishedAt": _utc_now_iso(),
            "progress": 100,
            "etaSeconds": 0,
            "message": "CHM extraction completed",
            "result": {
                "downloadUrl": f"/api/v1/chm/jobs/{job_id}/download",
                "contentType": "image/tiff",
                "sizeBytes": output_file_size,
            },
            "error": None,
        },
    )
    queue = get_queue_snapshot(settings)
    logger.info(
        "job_succeeded job_id=%s output_bytes=%s queue_counts queued=%s running=%s succeeded=%s failed=%s",
        job_id,
        output_file_size,
        queue[JobStatus.queued.value],
        queue[JobStatus.running.value],
        queue[JobStatus.succeeded.value],
        queue[JobStatus.failed.value],
    )
    return job


def run_chm_job(settings: Settings, job_id: str, geojson_obj: dict[str, Any]) -> None:
    logger.info("job_worker_start job_id=%s", job_id)
    mark_job_running(settings, job_id)
    update_job_progress(settings, job_id, progress=20, message="Preparing crop workdir")
    workdir = Path(tempfile.mkdtemp(prefix=f"chm_job_{job_id}_"))
    logger.info("job_worker_workdir job_id=%s workdir=%s", job_id, workdir)

    def _on_progress(progress: int, message: str | None) -> None:
        update_job_progress(settings, job_id, progress=progress, message=message)

    try:
        result = build_cropped_raster(
            geojson_obj,
            settings=settings,
            workdir=workdir,
            progress_callback=_on_progress,
        )
        destination = job_output_path(settings, job_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        update_job_progress(settings, job_id, progress=99, message="Persisting GeoTIFF output")
        shutil.move(str(result.output_path), destination)
        mark_job_succeeded(settings, job_id, output_file_size=destination.stat().st_size)
        logger.info("job_worker_complete job_id=%s output_path=%s", job_id, destination)
    except ServiceValidationError as exc:
        logger.warning("job_failed_validation job_id=%s error=%s", job_id, str(exc))
        mark_job_failed(settings, job_id, code="validation_failed", message=str(exc))
    except Exception as exc:  # pragma: no cover
        logger.exception("job_failed_generation job_id=%s", job_id)
        mark_job_failed(settings, job_id, code="generation_failed", message=str(exc))
    finally:
        safe_rmtree(workdir)
        logger.info("job_worker_cleanup_done job_id=%s", job_id)


def reconcile_incomplete_jobs(settings: Settings) -> int:
    _ensure_dirs(settings)
    reconciled = 0
    for file_path in settings.jobs_dir.glob("*.json"):
        try:
            payload = _read_job_file(file_path)
            if payload is None:
                continue
            status = payload.get("status")
            if status in {JobStatus.queued.value, JobStatus.running.value}:
                job_id = str(payload.get("jobId", file_path.stem))
                mark_job_failed(
                    settings,
                    job_id,
                    code="worker_interrupted",
                    message="Job was interrupted before completion; resubmit the request.",
                )
                logger.warning("job_reconciled_interrupted job_id=%s", job_id)
                reconciled += 1
        except Exception:  # pragma: no cover
            logger.exception("job_reconcile_error path=%s", file_path)
    return reconciled
