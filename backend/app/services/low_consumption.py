"""Отметка счётчиков с малым потреблением по диапазону кВт·ч (2026-09-11).

Критерий — последнее показание суммарной активной энергии (OBIS
``1.1.1.8.0.ff``) активного счётчика попадает в диапазон [min_kwh, max_kwh].
Логика вынесена из HTTP-обработчика в отдельную функцию, чтобы она была
доступна не только пользователю через API (ручной диапазон в UI), но и
"системе" — например, будущей периодической задаче планировщика, которая
захочет находить подозрительно малое потребление автоматически, без участия
пользователя (см. DECISIONS.md, запрос пользователя 2026-09-11).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Meter, MeterReading, MeterStatus

ENERGY_OBIS = "1.1.1.8.0.ff"


async def apply_low_consumption_range(
    db: AsyncSession, *, min_kwh: float, max_kwh: float
) -> list[Meter]:
    """Проставляет is_low_consumption=True активным счётчикам, чьё последнее
    показание ENERGY_OBIS лежит в [min_kwh, max_kwh]. Уже отмеченные счётчики
    не трогаются повторно (идемпотентно), счётчики без единого показания —
    пропускаются (диапазон применяется только к тому, что реально прочитано).
    Возвращает список счётчиков, которые эта функция реально отметила
    (счётчики, уже бывшие в категории, в список не попадают)."""
    latest_readings = await db.execute(
        select(MeterReading.meter_id, MeterReading.value_json)
        .where(MeterReading.obis_code == ENERGY_OBIS)
        .distinct(MeterReading.meter_id)
        .order_by(MeterReading.meter_id, MeterReading.read_at.desc())
    )
    value_by_meter_id = dict(latest_readings.all())

    meters_result = await db.execute(
        select(Meter).where(
            Meter.status == MeterStatus.ACTIVE,
            Meter.is_active.is_(True),
            Meter.is_low_consumption.is_(False),
        )
    )
    matched: list[Meter] = []
    for meter in meters_result.scalars().all():
        raw_value = value_by_meter_id.get(meter.id)
        if raw_value is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if min_kwh <= value <= max_kwh:
            meter.is_low_consumption = True
            matched.append(meter)
    if matched:
        await db.flush()
    return matched
