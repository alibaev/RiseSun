"""Карта OBIS-кодов (по просьбе пользователя, 2026-09-08) — только
чтение, справочник статический (см. ``app.obis_catalog.OBIS_CATALOG``),
не хранится в БД."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth.deps import require_permission
from ..core.permissions import Permission
from ..models import User
from ..obis_catalog import OBIS_CATALOG, ObisEntry
from ..schemas import ObisEntryOut

router = APIRouter(prefix="/api/obis-catalog", tags=["obis-catalog"])


@router.get("", response_model=list[ObisEntryOut])
async def list_obis_catalog(
    user: User = Depends(require_permission(Permission.VIEW_METERS)),
) -> list[ObisEntry]:
    return OBIS_CATALOG
