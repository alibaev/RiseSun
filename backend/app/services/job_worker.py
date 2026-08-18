"""Воркер асинхронных задач (Promt_MMWS.md, раздел 3, принцип 3 — длительные
операции не блокируют HTTP-ответ; раздел 2, требование к персистентной
очереди — задачи хранятся в PostgreSQL, не в памяти процесса, переживают
перезапуск Backend'а).

`SELECT ... FOR UPDATE SKIP LOCKED` — безопасный захват задачи при
нескольких экземплярах Backend одновременно (ТЗ п. 4.6, горизонтальное
масштабирование Backend API).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..core.security import decrypt_secret
from ..db import SessionLocal
from ..models import Job, JobStatus, Meter, MeterReading
from .gateway_client import read_register

logger = logging.getLogger("mmws_backend.job_worker")


async def _claim_next_job(db: AsyncSession) -> Job | None:
    result = await db.execute(
        select(Job)
        .where(Job.status == JobStatus.QUEUED)
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = result.scalar_one_or_none()
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.started_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(job)
    return job


async def _run_read_current(db: AsyncSession, job: Job) -> None:
    meter = await db.get(Meter, job.meter_id)
    if meter is None:
        job.status = JobStatus.FAILED
        job.error = {"code": "METER_NOT_FOUND", "message": f"Счётчик id={job.meter_id} не найден"}
        job.finished_at = datetime.now(timezone.utc)
        await db.commit()
        return

    gateway = meter.gateway
    obis = job.payload["obis"]
    password = decrypt_secret(meter.password_encrypted).decode("ascii")

    outcome = await read_register(
        grpc_target=gateway.grpc_target if gateway else settings.gateway_grpc_target,
        profile=meter.protocol_profile.value,
        host=meter.ip_address,
        port=meter.port,
        serial=meter.serial_number,
        password=password,
        obis=obis,
    )

    now = datetime.now(timezone.utc)
    if outcome.ok:
        job.status = JobStatus.SUCCEEDED
        job.result = {"obis": obis, "value": outcome.value}
        db.add(MeterReading(meter_id=meter.id, obis_code=obis, value_json=outcome.value))
        meter.last_seen_at = now
        meter.last_read_at = now
    else:
        job.status = JobStatus.FAILED
        job.error = {
            "code": outcome.error_code,
            "message": outcome.error_message,
            "is_partial": outcome.is_partial,
        }
    job.finished_at = now
    await db.commit()


_JOB_HANDLERS = {
    "read_current": _run_read_current,
}


async def _process_one(db: AsyncSession) -> bool:
    job = await _claim_next_job(db)
    if job is None:
        return False
    handler = _JOB_HANDLERS.get(job.job_type)
    try:
        if handler is None:
            raise ValueError(f"Неизвестный тип задачи: {job.job_type}")
        await handler(db, job)
    except Exception as exc:  # noqa: BLE001 — воркер не должен падать целиком из-за одной задачи
        logger.exception("Задача id=%s завершилась необработанной ошибкой", job.id)
        job.status = JobStatus.FAILED
        job.error = {"code": "WORKER_ERROR", "message": str(exc)}
        job.finished_at = datetime.now(timezone.utc)
        await db.commit()
    return True


async def worker_loop(stop_event: asyncio.Event) -> None:
    logger.info("Воркер задач запущен (poll interval=%.1fs)", settings.job_poll_interval_s)
    while not stop_event.is_set():
        async with SessionLocal() as db:
            processed = await _process_one(db)
        if not processed:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.job_poll_interval_s)
            except asyncio.TimeoutError:
                pass
    logger.info("Воркер задач остановлен")
