"""ТЗ п.4.2.11 — Дашборд: стартовый экран со сводной статистикой."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import require_permission
from ..core.permissions import Permission
from ..db import get_db
from ..models import Job, JobStatus, Meter, MeterReading, TamperLog, User

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


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

    return {
        "meters_total": len(meters),
        "meters_online": meters_online,
        "meters_offline": len(meters) - meters_online,
        "jobs_active": jobs_active,
        "tamper_events_24h": tamper_events_24h,
        "readings_by_hour": readings_by_hour,
    }
