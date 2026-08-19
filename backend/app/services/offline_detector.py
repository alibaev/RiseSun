"""Детектор перехода счётчика в offline (ТЗ п.4.2.8 — «по истечении
настраиваемого таймаута», ``settings.meter_offline_timeout_s``, та же
величина, что и в ``Meter.is_online``).

Edge-triggered без отдельного состояния в памяти: уведомление создаётся,
только если для текущего периода "не в сети" (после ``last_seen_at``)
ещё нет notification — так один и тот же уход в offline не плодит
уведомление на каждый тик цикла, а следующий уход offline (после того,
как счётчик снова вышел на связь и ``last_seen_at`` обновился) снова
создаёт новое уведомление."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..models import Meter, Notification, NotificationCategory
from .notifications import create_notification

logger = logging.getLogger("mmws_backend.offline_detector")


async def _run_once() -> None:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        result = await db.execute(
            select(Meter).where(Meter.is_active.is_(True), Meter.last_seen_at.is_not(None))
        )
        meters = result.scalars().all()

        for meter in meters:
            offline_for = (now - meter.last_seen_at).total_seconds()
            if offline_for < settings.meter_offline_timeout_s:
                continue

            already_notified = await db.execute(
                select(Notification.id)
                .where(
                    Notification.category == NotificationCategory.METER_OFFLINE,
                    Notification.meter_id == meter.id,
                    Notification.created_at > meter.last_seen_at,
                )
                .limit(1)
            )
            if already_notified.scalar_one_or_none() is not None:
                continue

            await create_notification(
                db,
                category=NotificationCategory.METER_OFFLINE,
                message=f"Счётчик {meter.serial_number} перешёл в состояние offline",
                meter_id=meter.id,
                details={"last_seen_at": meter.last_seen_at.isoformat()},
            )
        await db.commit()


async def offline_detector_loop(stop_event: asyncio.Event) -> None:
    logger.info(
        "Детектор offline-счётчиков запущен (interval=%.0fs, timeout=%ds)",
        settings.notification_check_interval_s, settings.meter_offline_timeout_s,
    )
    while not stop_event.is_set():
        try:
            await _run_once()
        except Exception:  # noqa: BLE001 — один сбойный тик не должен останавливать цикл
            logger.exception("Ошибка цикла детектора offline-счётчиков")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.notification_check_interval_s)
        except asyncio.TimeoutError:
            pass
    logger.info("Детектор offline-счётчиков остановлен")
