from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from threading import Condition, Lock, Thread
from typing import Any
from uuid import uuid4

from app.config import Settings
from app.models import JobStatus, ThreatMapJobCreateRequest
from app.services.threat_map_service import JobProgressUpdate, ThreatMapError, process_threat_map_job, utc_now_iso


class ThreatMapQueueFullError(RuntimeError):
    pass


logger = logging.getLogger("chm_api")


_WRITE_LOCK = Lock()
_QUEUE_LOCK = Lock()
_COND = Condition(_QUEUE_LOCK)
_STOP = False
_WORKER: Thread | None = None
_PENDING: deque[str] = deque()
_RUNNING_JOB_ID: str | None = None


def _ensure_dirs(settings: Settings) -> None:
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)


def _job_file_path(settings: Settings, job_id: str) -> Path:
    return settings.jobs_dir / f"threat_map_{job_id}.json"


def _read_job(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def _write_job(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as file_obj:
        json.dump(payload, file_obj, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _merge_job(settings: Settings, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    with _WRITE_LOCK:
        path = _job_file_path(settings, job_id)
        current = _read_job(path)
        if current is None:
            raise FileNotFoundError(f"threat-map job {job_id} not found")
        current.update(updates)
        _write_job(path, current)
    return current


def _list_jobs(settings: Settings) -> list[dict[str, Any]]:
    _ensure_dirs(settings)
    jobs: list[dict[str, Any]] = []
    for file_path in settings.jobs_dir.glob("threat_map_*.json"):
        payload = _read_job(file_path)
        if payload is not None:
            jobs.append(payload)
    return jobs


def _pending_count(settings: Settings) -> int:
    count = 0
    for job in _list_jobs(settings):
        if job.get("status") in {
            JobStatus.deferred.value,
            JobStatus.queued.value,
            JobStatus.running.value,
        }:
            count += 1
    return count


def create_threat_map_job(settings: Settings, payload: ThreatMapJobCreateRequest) -> dict[str, Any]:
    _ensure_dirs(settings)

    with _QUEUE_LOCK:
        pending_total = _pending_count(settings)
        if pending_total >= settings.threat_map_max_queue_length:
            logger.warning(
                "threat_map_queue_full pending_total=%s max_queue_length=%s",
                pending_total,
                settings.threat_map_max_queue_length,
            )
            raise ThreatMapQueueFullError("Threat-map queue is full")

        job_id = str(uuid4())
        status_value = JobStatus.queued.value if (_RUNNING_JOB_ID is None and len(_PENDING) == 0) else JobStatus.deferred.value

        record = {
            "jobId": job_id,
            "kind": "threat_map",
            "status": status_value,
            "createdAt": utc_now_iso(),
            "startedAt": None,
            "finishedAt": None,
            "progress": 0,
            "etaSeconds": None,
            "currentYear": None,
            "message": "Threat-map job created",
            "warnings": [],
            "result": None,
            "error": None,
            "cancelRequested": False,
            "request": payload.model_dump(),
        }

        with _WRITE_LOCK:
            _write_job(_job_file_path(settings, job_id), record)

        _PENDING.append(job_id)
        logger.info(
            "threat_map_job_enqueued job_id=%s status=%s queue_depth=%s output_format=%s",
            job_id,
            status_value,
            len(_PENDING),
            payload.outputFormat,
        )
        _COND.notify_all()
        return record


def get_threat_map_job(settings: Settings, job_id: str) -> dict[str, Any] | None:
    return _read_job(_job_file_path(settings, job_id))


def request_threat_map_cancel(settings: Settings, job_id: str) -> dict[str, Any] | None:
    job = get_threat_map_job(settings, job_id)
    if job is None:
        return None

    status = str(job.get("status"))
    if status in {
        JobStatus.succeeded.value,
        JobStatus.partial_success.value,
        JobStatus.failed.value,
        JobStatus.cancelled.value,
    }:
        logger.info("threat_map_cancel_ignored_terminal job_id=%s status=%s", job_id, status)
        return job

    if status in {JobStatus.deferred.value, JobStatus.queued.value}:
        with _QUEUE_LOCK:
            try:
                _PENDING.remove(job_id)
            except ValueError:
                pass

        logger.info("threat_map_job_cancelled_before_start job_id=%s", job_id)
        return _merge_job(
            settings,
            job_id,
            {
                "status": JobStatus.cancelled.value,
                "finishedAt": utc_now_iso(),
                "progress": None,
                "etaSeconds": None,
                "message": "Threat-map job cancelled",
                "error": {"code": "cancelled", "message": "Cancelled by request"},
            },
        )

    logger.info("threat_map_job_cancel_requested job_id=%s status=%s", job_id, status)
    return _merge_job(
        settings,
        job_id,
        {
            "cancelRequested": True,
            "message": "Cancellation requested",
        },
    )


def _reconcile_on_startup(settings: Settings) -> None:
    records = sorted(_list_jobs(settings), key=lambda item: str(item.get("createdAt", "")))
    recovered = 0
    interrupted = 0
    with _QUEUE_LOCK:
        _PENDING.clear()
        for record in records:
            status = str(record.get("status"))
            job_id = str(record.get("jobId"))
            if status == JobStatus.running.value:
                interrupted += 1
                _merge_job(
                    settings,
                    job_id,
                    {
                        "status": JobStatus.failed.value,
                        "finishedAt": utc_now_iso(),
                        "message": "Threat-map worker interrupted",
                        "error": {
                            "code": "worker_interrupted",
                            "message": "Job interrupted before completion; resubmit request",
                        },
                    },
                )
                continue

            if status in {JobStatus.deferred.value, JobStatus.queued.value}:
                _PENDING.append(job_id)
                recovered += 1
    if interrupted or recovered:
        logger.warning(
            "threat_map_reconcile_startup interrupted=%s recovered_pending=%s",
            interrupted,
            recovered,
        )


def start_threat_map_worker(settings: Settings) -> None:
    global _WORKER, _STOP

    if not settings.threat_map_enabled:
        logger.info("threat_map_worker_disabled")
        return

    with _QUEUE_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            logger.info("threat_map_worker_already_running")
            return
        _STOP = False

    _reconcile_on_startup(settings)

    def worker_loop() -> None:
        global _RUNNING_JOB_ID
        logger.info("threat_map_worker_started")

        while True:
            with _QUEUE_LOCK:
                while not _STOP and not _PENDING:
                    _COND.wait(timeout=0.5)

                if _STOP:
                    logger.info("threat_map_worker_stopping")
                    return

                job_id = _PENDING.popleft()
                _RUNNING_JOB_ID = job_id
                logger.info("threat_map_worker_dequeued job_id=%s remaining_queue=%s", job_id, len(_PENDING))

            try:
                _merge_job(
                    settings,
                    job_id,
                    {
                        "status": JobStatus.running.value,
                        "startedAt": utc_now_iso(),
                        "progress": 1,
                        "message": "Threat-map rendering started",
                        "error": None,
                    },
                )

                job = get_threat_map_job(settings, job_id)
                if job is None:
                    raise ThreatMapError("generation_failed", "Job payload missing")

                payload = ThreatMapJobCreateRequest.model_validate(job["request"])
                logger.info(
                    "threat_map_worker_processing job_id=%s preset=%s output_format=%s",
                    job_id,
                    payload.preset,
                    payload.outputFormat,
                )
                last_logged_year: int | None = None

                def on_progress(update: JobProgressUpdate) -> None:
                    nonlocal last_logged_year
                    _merge_job(
                        settings,
                        job_id,
                        {
                            "progress": update.progress,
                            "currentYear": update.current_year,
                            "message": update.message,
                        },
                    )
                    if update.current_year is not None and update.current_year != last_logged_year:
                        logger.info(
                            "threat_map_worker_progress job_id=%s progress=%s current_year=%s message=%s",
                            job_id,
                            update.progress,
                            update.current_year,
                            update.message,
                        )
                        last_logged_year = update.current_year

                def is_cancelled() -> bool:
                    latest = get_threat_map_job(settings, job_id)
                    return bool(latest and latest.get("cancelRequested"))

                result = process_threat_map_job(
                    settings=settings,
                    job_id=job_id,
                    payload=payload,
                    progress_callback=on_progress,
                    is_cancelled=is_cancelled,
                )

                if result["status"] == JobStatus.succeeded.value:
                    final_status = JobStatus.succeeded.value
                elif result["status"] == JobStatus.partial_success.value:
                    final_status = JobStatus.partial_success.value
                else:
                    final_status = JobStatus.failed.value

                _merge_job(
                    settings,
                    job_id,
                    {
                        "status": final_status,
                        "finishedAt": utc_now_iso(),
                        "progress": 100,
                        "etaSeconds": 0,
                        "currentYear": 2024,
                        "message": "Threat-map rendering complete",
                        "warnings": result.get("warnings", []),
                        "result": {
                            "downloadUrl": f"/api/v1/threat-map/jobs/{job_id}/download",
                            "contentType": result["contentType"],
                            "artifactType": result["artifactType"],
                            "sizeBytes": result["sizeBytes"],
                            "yearsRendered": result["yearsRendered"],
                            "yearsExpected": result["yearsExpected"],
                            "fallbackReasonCode": result.get("fallbackReasonCode"),
                        },
                        "error": None,
                    },
                )
                logger.info(
                    "threat_map_worker_completed job_id=%s final_status=%s artifact_type=%s size_bytes=%s warnings=%s",
                    job_id,
                    final_status,
                    result.get("artifactType"),
                    result.get("sizeBytes"),
                    len(result.get("warnings", [])),
                )
            except ThreatMapError as exc:
                code = exc.code
                cancelled = code == "cancelled"
                logger.warning(
                    "threat_map_worker_failed job_id=%s code=%s cancelled=%s message=%s",
                    job_id,
                    code,
                    cancelled,
                    str(exc),
                )
                _merge_job(
                    settings,
                    job_id,
                    {
                        "status": JobStatus.cancelled.value if cancelled else JobStatus.failed.value,
                        "finishedAt": utc_now_iso(),
                        "progress": None,
                        "etaSeconds": None,
                        "message": "Threat-map job cancelled" if cancelled else "Threat-map generation failed",
                        "result": None,
                        "error": {"code": code, "message": str(exc)},
                    },
                )
            except Exception as exc:  # pragma: no cover
                logger.exception("threat_map_worker_unhandled_exception job_id=%s", job_id)
                _merge_job(
                    settings,
                    job_id,
                    {
                        "status": JobStatus.failed.value,
                        "finishedAt": utc_now_iso(),
                        "progress": None,
                        "etaSeconds": None,
                        "message": "Threat-map generation failed",
                        "result": None,
                        "error": {"code": "generation_failed", "message": str(exc)},
                    },
                )
            finally:
                with _QUEUE_LOCK:
                    _RUNNING_JOB_ID = None
                    logger.info("threat_map_worker_slot_released job_id=%s", job_id)

    _WORKER = Thread(target=worker_loop, name="threat-map-worker", daemon=True)
    _WORKER.start()


def stop_threat_map_worker() -> None:
    global _WORKER, _STOP, _RUNNING_JOB_ID

    with _QUEUE_LOCK:
        _STOP = True
        _COND.notify_all()

    if _WORKER is not None:
        _WORKER.join(timeout=2)
    with _QUEUE_LOCK:
        _PENDING.clear()
        _RUNNING_JOB_ID = None
    _WORKER = None
    logger.info("threat_map_worker_stopped")


def threat_map_output_path(settings: Settings, job_id: str, artifact_type: str) -> Path:
    if artifact_type == "frames_tar_gz":
        return settings.outputs_dir / f"threat_map_{job_id}_frames.tar.gz"
    if artifact_type == "zip":
        return settings.outputs_dir / f"threat_map_{job_id}.zip"
    return settings.outputs_dir / f"threat_map_{job_id}.mp4"


def threat_map_queue_snapshot(settings: Settings) -> dict[str, int]:
    counts = {
        JobStatus.deferred.value: 0,
        JobStatus.queued.value: 0,
        JobStatus.running.value: 0,
        JobStatus.succeeded.value: 0,
        JobStatus.partial_success.value: 0,
        JobStatus.failed.value: 0,
        JobStatus.cancelled.value: 0,
    }

    for job in _list_jobs(settings):
        status = str(job.get("status", ""))
        if status in counts:
            counts[status] += 1
    return counts


def _parse_utc(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
