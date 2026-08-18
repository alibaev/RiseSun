"""ТЗ п. 4.2.2 (справочник счётчиков), п. 4.2.3 (чтение данных), Приложение А."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import require_permission
from ..core.permissions import Permission
from ..core.security import encrypt_secret
from ..db import get_db
from ..models import EventLog, Gateway, GatewayStatus, Job, Meter, MeterReading, TamperLog, User
from ..schemas import (
    JobOut,
    MeterCreate,
    MeterOut,
    MeterReadingOut,
    MeterUpdate,
    ReadTriggerRequest,
)
from ..services.audit import record_audit

router = APIRouter(prefix="/api/meters", tags=["meters"])


@router.get("", response_model=list[MeterOut])
async def list_meters(
    location: str | None = None,
    protocol_profile: str | None = None,
    is_active: bool | None = None,
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.VIEW_METERS)),
) -> list[Meter]:
    query = select(Meter)
    if location:
        query = query.where(Meter.location == location)
    if protocol_profile:
        query = query.where(Meter.protocol_profile == protocol_profile)
    if is_active is not None:
        query = query.where(Meter.is_active == is_active)
    if search:
        pattern = f"%{search}%"
        query = query.where(
            (Meter.serial_number.ilike(pattern)) | (Meter.ip_address.ilike(pattern))
        )
    result = await db.execute(query.order_by(Meter.serial_number))
    return list(result.scalars().all())


@router.get("/{meter_id}", response_model=MeterOut)
async def get_meter(
    meter_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.VIEW_METERS)),
) -> Meter:
    meter = await db.get(Meter, meter_id)
    if meter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Счётчик не найден")
    return meter


@router.post("", response_model=MeterOut, status_code=status.HTTP_201_CREATED)
async def create_meter(
    body: MeterCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_METERS)),
) -> Meter:
    gateway = await db.get(Gateway, body.gateway_id)
    if gateway is None or gateway.status != GatewayStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Gateway не найден либо не подтверждён (approved) Супер-администратором",
        )

    meter = Meter(
        serial_number=body.serial_number,
        ip_address=body.ip_address,
        port=body.port,
        protocol_profile=body.protocol_profile,
        password_encrypted=encrypt_secret(body.password.encode("ascii")),
        aes_key_encrypted=encrypt_secret(bytes.fromhex(body.aes_key_hex)) if body.aes_key_hex else None,
        location=body.location,
        model=body.model,
        gateway_id=body.gateway_id,
    )
    db.add(meter)
    await db.flush()
    await record_audit(
        db, user_id=user.id, action="meter.create", object_type="meter",
        object_id=str(meter.id), ip_address=request.client.host if request.client else None,
        details={"serial_number": body.serial_number},
    )
    await db.commit()
    await db.refresh(meter)
    return meter


@router.put("/{meter_id}", response_model=MeterOut)
async def update_meter(
    meter_id: int,
    body: MeterUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_METERS)),
) -> Meter:
    meter = await db.get(Meter, meter_id)
    if meter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Счётчик не найден")

    changes = body.model_dump(exclude_unset=True, exclude={"password", "aes_key_hex"})
    for field, value in changes.items():
        setattr(meter, field, value)
    if body.password is not None:
        meter.password_encrypted = encrypt_secret(body.password.encode("ascii"))
    if body.aes_key_hex is not None:
        meter.aes_key_encrypted = encrypt_secret(bytes.fromhex(body.aes_key_hex))

    await record_audit(
        db, user_id=user.id, action="meter.update", object_type="meter",
        object_id=str(meter.id), ip_address=request.client.host if request.client else None,
        details={"changed_fields": list(changes.keys())},
    )
    await db.commit()
    await db.refresh(meter)
    return meter


@router.delete("/{meter_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meter(
    meter_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_METERS)),
) -> None:
    meter = await db.get(Meter, meter_id)
    if meter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Счётчик не найден")

    await db.delete(meter)
    try:
        await db.flush()
    except IntegrityError:
        # Найдено при ручной проверке (2026-08-18): показания/задачи/журналы
        # ссылаются на счётчик по FK без каскада — удаление роняло 500
        # вместо понятной ошибки. История счётчика — фактически часть
        # аудита, поэтому удаление с историей запрещено осознанно
        # (не каскадное удаление), а не просто "починено" молча.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Нельзя удалить счётчик — с ним связаны показания, задачи "
                "или журналы. Деактивируйте счётчик (is_active=false) вместо удаления."
            ),
        )

    await record_audit(
        db, user_id=user.id, action="meter.delete", object_type="meter",
        object_id=str(meter_id), ip_address=request.client.host if request.client else None,
    )
    await db.commit()


@router.post("/{meter_id}/read", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def trigger_read(
    meter_id: int,
    body: ReadTriggerRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.TRIGGER_READ)),
) -> Job:
    """Ставит операцию чтения в очередь и немедленно возвращает job_id
    (ТЗ п. 4.7 — постановка в очередь укладывается в целевые 500 мс, без
    ожидания фактического опроса счётчика)."""
    meter = await db.get(Meter, meter_id)
    if meter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Счётчик не найден")

    job = Job(
        job_type="read_current",
        meter_id=meter_id,
        payload={"obis": body.obis},
        created_by_id=user.id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


@router.get("/{meter_id}/readings", response_model=list[MeterReadingOut])
async def list_readings(
    meter_id: int,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.VIEW_METERS)),
) -> list[MeterReading]:
    result = await db.execute(
        select(MeterReading)
        .where(MeterReading.meter_id == meter_id)
        .order_by(MeterReading.read_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


@router.get("/{meter_id}/event-log")
async def list_event_log(
    meter_id: int,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.VIEW_METERS)),
) -> list[dict]:
    result = await db.execute(
        select(EventLog).where(EventLog.meter_id == meter_id).order_by(EventLog.occurred_at.desc()).limit(limit)
    )
    return [
        {"id": r.id, "category": r.category, "raw_code": r.raw_code, "occurred_at": r.occurred_at}
        for r in result.scalars().all()
    ]


@router.get("/{meter_id}/tamper-log")
async def list_tamper_log(
    meter_id: int,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.VIEW_METERS)),
) -> list[dict]:
    result = await db.execute(
        select(TamperLog).where(TamperLog.meter_id == meter_id).order_by(TamperLog.occurred_at.desc()).limit(limit)
    )
    return [
        {"id": r.id, "category": r.category, "raw_code": r.raw_code, "occurred_at": r.occurred_at}
        for r in result.scalars().all()
    ]
