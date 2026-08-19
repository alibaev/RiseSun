"""Уведомления (ТЗ п.4.2.8) — общая точка создания записи в ``notifications``,
используется и планировщиком (ошибка запланированной задачи), и фоновым
детектором offline-счётчиков (см. ``offline_detector.py``).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Notification, NotificationCategory


async def create_notification(
    db: AsyncSession,
    *,
    category: NotificationCategory,
    message: str,
    meter_id: int | None = None,
    scheduled_job_id: int | None = None,
    details: dict | None = None,
) -> Notification:
    notification = Notification(
        category=category,
        message=message,
        meter_id=meter_id,
        scheduled_job_id=scheduled_job_id,
        details=details,
    )
    db.add(notification)
    await db.flush()
    return notification
