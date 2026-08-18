"""ТЗ п. 4.2.7 — просмотр журнала аудита (роль «Администратор»)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import require_permission
from ..core.permissions import Permission
from ..db import get_db
from ..models import AuditLog, User

router = APIRouter(prefix="/api/audit-log", tags=["audit"])


@router.get("")
async def list_audit_log(
    user_id: int | None = None,
    action: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.VIEW_AUDIT_LOG)),
) -> list[dict]:
    query = select(AuditLog)
    if user_id is not None:
        query = query.where(AuditLog.user_id == user_id)
    if action is not None:
        query = query.where(AuditLog.action == action)
    if date_from is not None:
        query = query.where(AuditLog.created_at >= date_from)
    if date_to is not None:
        query = query.where(AuditLog.created_at <= date_to)
    query = query.order_by(AuditLog.created_at.desc()).limit(limit)

    result = await db.execute(query)
    return [
        {
            "id": r.id,
            "user_id": r.user_id,
            "source": r.source,
            "action": r.action,
            "object_type": r.object_type,
            "object_id": r.object_id,
            "result": r.result,
            "ip_address": r.ip_address,
            "details": r.details,
            "created_at": r.created_at,
        }
        for r in result.scalars().all()
    ]
