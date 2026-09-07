"""Планировщик автоматического опроса по расписанию (ТЗ п.4.2.6).

Фоновый цикл (тот же паттерн, что и ``heartbeat.py``/``job_worker.py``),
проверяющий каждые ``settings.scheduler_check_interval_s`` секунд, какие
включённые ``ScheduledJob`` должны сработать (croniter, следующий момент
срабатывания после ``last_run_at`` не позже текущего времени).
Срабатывание создаёт ``ScheduledJobRun`` и по одной ``Job`` на каждый
счётчик группы — сами задачи выполняет обычный ``job_worker`` (планировщик
только ставит их в очередь, не читает счётчики сам — то же разделение
ответственности, что и у ручного запуска через API,
Promt_MMWS.md, раздел 3, принцип 3).

``operation_params.skip_if_read_today`` (2026-09-07, согласовано с
пользователем для ежедневного опроса всех счётчиков) — опциональный
флаг для ``job_type="read_current"``: если включён, счётчики, у которых
уже есть ``MeterReading`` по тому же OBIS за ТЕКУЩИЕ сутки по времени
Asia/Bishkek (UTC+6, без перехода на летнее), из очередного запуска
исключаются. Это вместе с частым cron (например, каждые 30 минут)
реализует «зафиксировать показание на 00:00 Бишкек, если не вышло — на
00:30, потом на 01:00 и так далее, пока не получится, но не опрашивать
повторно счётчик, который уже отчитался за эти сутки» — без этого
флага (по умолчанию выключен) поведение прежнее: расписание всегда
опрашивает весь список ``meter_ids``.

``job_type="read_rated_current"`` (2026-09-07, согласовано с
пользователем) — аналогичный принцип, но БЕЗ суточного окна: токовый
класс счётчика (``Meter.rated_current_amps``) — статичный параметр, не
меняется со временем, поэтому счётчик с уже известным значением
исключается из группы НАВСЕГДА, не только на текущие сутки."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal
from ..models import Job, Meter, MeterReading, ScheduledJob, ScheduledJobRun, ScheduledJobRunStatus

logger = logging.getLogger("mmws_backend.scheduler")

_DEFAULT_LOAD_PROFILE_WINDOW_HOURS = 24
_BISHKEK_TZ = ZoneInfo("Asia/Bishkek")


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
    if scheduled_job.job_type == "read_rated_current":
        return {}  # OBIS фиксирован в job_worker.RATED_CURRENT_OBIS, не параметризуется
    # read_current
    return {"obis": scheduled_job.operation_params.get("obis", "1.1.1.8.0.ff")}


def _bishkek_day_bounds_utc(now: datetime) -> tuple[datetime, datetime]:
    day_start_bishkek = now.astimezone(_BISHKEK_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = day_start_bishkek.astimezone(timezone.utc)
    return day_start_utc, day_start_utc + timedelta(days=1)


async def _already_read_today_meter_ids(
    db: AsyncSession, meter_ids: list[int], obis: str, now: datetime
) -> set[int]:
    day_start_utc, day_end_utc = _bishkek_day_bounds_utc(now)
    result = await db.execute(
        select(MeterReading.meter_id).where(
            MeterReading.meter_id.in_(meter_ids),
            MeterReading.obis_code == obis,
            MeterReading.read_at >= day_start_utc,
            MeterReading.read_at < day_end_utc,
        )
    )
    return set(result.scalars().all())


async def _trigger_one(db: AsyncSession, scheduled_job: ScheduledJob) -> None:
    now = datetime.now(timezone.utc)
    meters = (
        await db.execute(select(Meter).where(Meter.id.in_(scheduled_job.meter_ids)))
    ).scalars().all()

    payload = _build_job_payload(scheduled_job)
    skip_reason = None
    if scheduled_job.job_type == "read_current" and scheduled_job.operation_params.get("skip_if_read_today"):
        already_read = await _already_read_today_meter_ids(
            db, [m.id for m in meters], payload["obis"], now
        )
        meters = [m for m in meters if m.id not in already_read]
        skip_reason = "все счётчики группы уже опрошены за сегодня (Asia/Bishkek)"
    elif scheduled_job.job_type == "read_rated_current":
        # Токовый класс (rated_current_amps) — статичный паспортный
        # параметр, не меняется у счётчика со временем, поэтому здесь
        # НЕТ суточного окна — счётчик, у которого он уже известен,
        # исключается НАВСЕГДА, а не до конца текущих суток (в отличие
        # от skip_if_read_today выше).
        meters = [m for m in meters if m.rated_current_amps is None]
        skip_reason = "у всех счётчиков группы токовый класс уже известен"

    scheduled_job.last_run_at = now
    if not meters:
        await db.commit()
        logger.info(
            "Расписание id=%s (%s) сработало — %s, пропуск",
            scheduled_job.id, scheduled_job.name, skip_reason or "пустая группа",
        )
        return

    run = ScheduledJobRun(
        scheduled_job_id=scheduled_job.id,
        status=ScheduledJobRunStatus.RUNNING,
        meters_total=len(meters),
    )
    db.add(run)
    await db.flush()

    for meter in meters:
        db.add(
            Job(
                job_type=scheduled_job.job_type,
                meter_id=meter.id,
                payload=payload,
                scheduled_job_run_id=run.id,
            )
        )

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
