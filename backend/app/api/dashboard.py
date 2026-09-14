"""ТЗ п.4.2.11 — Дашборд: стартовый экран со сводной статистикой."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import require_permission
from ..core.permissions import Permission
from ..db import get_db
from ..models import Job, JobStatus, LoadProfileData, Meter, MeterReading, TamperLog, User

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

_BISHKEK_TZ = ZoneInfo("Asia/Bishkek")


def _bishkek_day_start_utc(now: datetime) -> datetime:
    day_start_bishkek = now.astimezone(_BISHKEK_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start_bishkek.astimezone(timezone.utc)


async def _read_percentage(db: AsyncSession, active_meter_ids: list[int], since: datetime) -> float:
    """Процент активных счётчиков, у которых есть хотя бы одно
    MeterReading (успешное чтение) начиная с ``since`` — тот же смысл,
    что у "процент чтения", которым пользователь оперирует по данным
    референсной системы (2026-09-08, см. DECISIONS.md). Считается по
    ЧИСЛУ РАЗНЫХ счётчиков с показанием, а не по числу job'ов/показаний
    — повторные чтения одного счётчика не завышают процент."""
    if not active_meter_ids:
        return 0.0
    distinct_read = (
        await db.execute(
            select(func.count(func.distinct(MeterReading.meter_id))).where(
                MeterReading.meter_id.in_(active_meter_ids), MeterReading.read_at >= since,
            )
        )
    ).scalar_one()
    return round(100.0 * distinct_read / len(active_meter_ids), 1)


async def _load_profile_percentage(db: AsyncSession, active_meter_ids: list[int], since: datetime) -> float:
    """Аналог ``_read_percentage``, но по LoadProfileData.recorded_at —
    доля активных счётчиков, от которых мы реально ПОЛУЧИЛИ и сохранили
    хотя бы одну строку профиля нагрузки начиная с ``since``. Используем
    recorded_at (момент сохранения у нас), а не timestamp самой строки
    (момент замера на счётчике) — иначе тестовое чтение старого окна
    задним числом не будет засчитано как «опрос сегодня»."""
    if not active_meter_ids:
        return 0.0
    distinct = (
        await db.execute(
            select(func.count(func.distinct(LoadProfileData.meter_id))).where(
                LoadProfileData.meter_id.in_(active_meter_ids), LoadProfileData.recorded_at >= since,
            )
        )
    ).scalar_one()
    return round(100.0 * distinct / len(active_meter_ids), 1)


@router.get("")
async def get_dashboard(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.VIEW_METERS)),
) -> dict:
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)

    meters = (await db.execute(select(Meter).where(Meter.is_active.is_(True)))).scalars().all()
    meters_online = sum(1 for m in meters if m.is_online)

    jobs_active = (
        await db.execute(
            select(func.count()).select_from(Job).where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
        )
    ).scalar_one()

    # Опрос профиля нагрузки (Profile 1) — отдельная сводка, чтобы не
    # спрашивать отчёт вручную: сколько задач ещё в очереди/выполняется,
    # сколько завершилось неудачно за сутки, и с какой доли активных
    # счётчиков реально получены и сохранены строки профиля.
    load_profile_jobs_active = (
        await db.execute(
            select(func.count())
            .select_from(Job)
            .where(Job.job_type == "read_load_profile", Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
        )
    ).scalar_one()
    load_profile_jobs_failed_24h = (
        await db.execute(
            select(func.count())
            .select_from(Job)
            .where(
                Job.job_type == "read_load_profile",
                Job.status == JobStatus.FAILED,
                Job.finished_at >= day_ago,
            )
        )
    ).scalar_one()

    tamper_events_24h = (
        await db.execute(
            select(func.count()).select_from(TamperLog).where(TamperLog.occurred_at >= day_ago)
        )
    ).scalar_one()

    # «график динамики опроса/показаний» — число сохранённых показаний
    # по часам за последние сутки (сырое количество строк
    # meter_readings.read_at, а не агрегация по конкретным OBIS-кодам —
    # достаточно для «динамики опроса», не для содержательного графика
    # энергопотребления).
    readings = (
        await db.execute(select(MeterReading.read_at).where(MeterReading.read_at >= day_ago))
    ).scalars().all()
    buckets: dict[str, int] = {}
    for read_at in readings:
        hour_key = read_at.replace(minute=0, second=0, microsecond=0).isoformat()
        buckets[hour_key] = buckets.get(hour_key, 0) + 1
    readings_by_hour = [{"hour": hour, "count": count} for hour, count in sorted(buckets.items())]

    active_meter_ids = [m.id for m in meters]
    day_start_bishkek = _bishkek_day_start_utc(now)
    read_percentage_today = await _read_percentage(db, active_meter_ids, day_start_bishkek)
    read_percentage_3d = await _read_percentage(db, active_meter_ids, now - timedelta(days=3))
    load_profile_percentage_today = await _load_profile_percentage(db, active_meter_ids, day_start_bishkek)

    return {
        "meters_total": len(meters),
        "read_percentage_today": read_percentage_today,
        "read_percentage_3d": read_percentage_3d,
        "meters_online": meters_online,
        "meters_offline": len(meters) - meters_online,
        "jobs_active": jobs_active,
        "tamper_events_24h": tamper_events_24h,
        "readings_by_hour": readings_by_hour,
        "load_profile_percentage_today": load_profile_percentage_today,
        "load_profile_jobs_active": load_profile_jobs_active,
        "load_profile_jobs_failed_24h": load_profile_jobs_failed_24h,
    }
