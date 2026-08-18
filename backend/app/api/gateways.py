"""ТЗ п. 4.1.1 — реестр экземпляров Protocol Gateway.

Регистрация и подтверждение — исключительно роль «Супер-администратор»,
без авторегистрации (Promt_MMWS.md, раздел 3, принцип 7).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import require_permission
from ..core.permissions import Permission
from ..db import get_db
from ..models import Gateway, GatewayStatus, User
from ..schemas import GatewayCreate, GatewayOut
from ..services.audit import record_audit

router = APIRouter(prefix="/api/gateways", tags=["gateways"])


@router.get("", response_model=list[GatewayOut])
async def list_gateways(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_GATEWAYS)),
) -> list[Gateway]:
    result = await db.execute(select(Gateway).order_by(Gateway.created_at))
    return list(result.scalars().all())


@router.post("", response_model=GatewayOut, status_code=status.HTTP_201_CREATED)
async def register_gateway(
    body: GatewayCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_GATEWAYS)),
) -> Gateway:
    gateway = Gateway(
        name=body.name,
        manufacturer=body.manufacturer,
        driver_version=body.driver_version,
        grpc_target=body.grpc_target,
        supported_operations=body.supported_operations,
        status=GatewayStatus.PENDING,
        registered_by_id=user.id,
    )
    db.add(gateway)
    await db.flush()
    await record_audit(
        db, user_id=user.id, action="gateway.register", object_type="gateway",
        object_id=str(gateway.id), ip_address=request.client.host if request.client else None,
        details={"name": body.name, "grpc_target": body.grpc_target},
    )
    await db.commit()
    await db.refresh(gateway)
    return gateway


@router.post("/{gateway_id}/approve", response_model=GatewayOut)
async def approve_gateway(
    gateway_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_GATEWAYS)),
) -> Gateway:
    gateway = await db.get(Gateway, gateway_id)
    if gateway is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gateway не найден")
    if gateway.status == GatewayStatus.APPROVED:
        return gateway

    gateway.status = GatewayStatus.APPROVED
    gateway.approved_by_id = user.id
    gateway.approved_at = datetime.now(timezone.utc)
    await record_audit(
        db, user_id=user.id, action="gateway.approve", object_type="gateway",
        object_id=str(gateway.id), ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    await db.refresh(gateway)
    return gateway


@router.post("/{gateway_id}/disable", response_model=GatewayOut)
async def disable_gateway(
    gateway_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_GATEWAYS)),
) -> Gateway:
    gateway = await db.get(Gateway, gateway_id)
    if gateway is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gateway не найден")

    gateway.status = GatewayStatus.DISABLED
    await record_audit(
        db, user_id=user.id, action="gateway.disable", object_type="gateway",
        object_id=str(gateway.id), ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    await db.refresh(gateway)
    return gateway
