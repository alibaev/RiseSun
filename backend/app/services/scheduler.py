"""Планировщик автоматического опроса по расписанию (ТЗ п.4.2.6).

Фоновый цикл (тот же паттерн, что и ``heartbeat.py``/``job_worker.py``),
проверяющий каждые ``settings.scheduler_check_interval_s`` секунд, какие
включённые ``ScheduledJob`` должны сработать (croniter, следующий момент
срабатывания после ``last_run_at`` не позже текущего времени).
Срабатывание создаёт ``ScheduledJobRun`` и по одной ``Job`` на каждый
счётчик группы — сами задачи выполняет обычный ``job_worker`` (планировщик
только ставит их в очередь, не читает счётчики сам — то же разделение
ответственности, что и у ручного запуска через API,
Promt_MMWS.md, раздел 3, принцип 3)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal
from ..models import Job, Meter, ScheduledJob, ScheduledJobRun, ScheduledJobRunStatus

logger = logging.getLogger("mmws_backend.scheduler")

_DEFAULT_LOAD_PROFILE_WINDOW_HOURS = 24


def next_fire_time(cron_expression: str, base: datetime) -> datetime:
    itr = croniter(cron_expression, base)
    next_fire = itr.get_next(datetime)
    if next_fire.tzinfo is None:
        next_fire = next_fire.replace(tzinfo=timezone.utc)
    return next_fire


def _is_due(scheduled_job: ScheduledJob, now: datetime) -> bool:
    base = scheduled_job.last_run_at or scheduled_job.created_at
    try:
        return next_fire_time(scheduled_job.cron_expression, base) <= now
    except (ValueError, KeyError):
        logger.error(
            "Расписание id=%s имеет некорректное cron-выражение %r — пропускаю",
            scheduled_job.id, scheduled_job.cron_expression,
        )
        return False


def _build_job_payload(scheduled_job: ScheduledJob) -> dict:
    if scheduled_job.job_type == "read_load_profile":
        window_hours = scheduled_job.operation_params.get("window_hours", _DEFAULT_LOAD_PROFILE_WINDOW_HOURS)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        payload = {
            "from_iso": (now - timedelta(hours=window_hours)).isoformat(),
            "to_iso": now.isoformat(),
        }
        if scheduled_job.operation_params.get("obis"):
            payload["obis"] = scheduled_job.operation_params["obis"]
        return payload
    # read_current
    return {"obis": scheduled_job.operation_params.get("obis", "1.1.1.8.0.ff")}


async def _trigger_one(db: AsyncSession, scheduled_job: ScheduledJob) -> None:
    meters = (
        await db.execute(select(Meter).where(Meter.id.in_(scheduled_job.meter_ids)))
    ).scalars().all()
    run = ScheduledJobRun(
        scheduled_job_id=scheduled_job.id,
        status=ScheduledJobRunStatus.RUNNING,
        meters_total=len(meters),
    )
    db.add(run)
    await db.flush()

    payload = _build_job_payload(scheduled_job)
    for meter in meters:
        db.add(
            Job(
                job_type=scheduled_job.job_type,
                meter_id=meter.id,
                payload=payload,
                scheduled_job_run_id=run.id,
            )
        )

    scheduled_job.last_run_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info(
        "Расписание id=%s (%s) сработало — run id=%s, счётчиков: %d",
        scheduled_job.id, scheduled_job.name, run.id, len(meters),
    )


async def _run_once() -> None:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        result = await db.execute(select(ScheduledJob).where(ScheduledJob.is_enabled.is_(True)))
        due = [j for j in result.scalars().all() if _is_due(j, now)]
        for scheduled_job in due:
            await _trigger_one(db, scheduled_job)


async def scheduler_loop(stop_event: asyncio.Event) -> None:
    logger.info("Планировщик расписаний запущен (interval=%.0fs)", settings.scheduler_check_interval_s)
    while not stop_event.is_set():
        try:
            await _run_once()
        except Exception:  # noqa: BLE001 — один сбойный тик не должен останавливать цикл
            logger.exception("Ошибка цикла планировщика расписаний")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.scheduler_check_interval_s)
        except asyncio.TimeoutError:
            pass
    logger.info("Планировщик расписаний остановлен")
