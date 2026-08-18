"""Запись в неизменяемый журнал аудита (ТЗ п. 4.2.7)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditLog


async def record_audit(
    db: AsyncSession,
    *,
    user_id: int | None,
    action: str,
    object_type: str | None = None,
    object_id: str | None = None,
    result: str = "success",
    source: str = "web",
    ip_address: str | None = None,
    details: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            result=result,
            source=source,
            ip_address=ip_address,
            details=details,
        )
    )
