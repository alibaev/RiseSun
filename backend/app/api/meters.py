"""ТЗ п. 4.2.2 (справочник счётчиков), п. 4.2.3 (чтение данных), Приложение А."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import require_permission
from ..core.permissions import Permission
from ..core.security import encrypt_secret
from ..db import get_db
from ..models import (
    EventLog,
    Gateway,
    GatewayStatus,
    Job,
    LoadProfileData,
    Meter,
    MeterReading,
    MeterStatus,
    ParameterWriteHistory,
    TamperLog,
    User,
)
from ..schemas import (
    ActivateMeterRequest,
    JobOut,
    LoadProfileRowOut,
    MeterCreate,
    MeterOut,
    MeterReadingOut,
    MeterUpdate,
    ParameterWriteHistoryOut,
    ReadLoadProfileTriggerRequest,
    ReadTriggerRequest,
    WriteParameterRequest,
)
from ..services.audit import record_audit
from ..services.write_parameters import WRITABLE_INT_PARAMETERS

router = APIRouter(prefix="/api/meters", tags=["meters"])


async def _get_active_meter_or_error(db: AsyncSession, meter_id: int) -> Meter:
    """Общая проверка для всех эндпоинтов, ставящих Job (Этап 6):
    счётчик со статусом INSTALLED ещё не имеет пароля/протокольного
    профиля (см. models.MeterStatus) — попытка создать для него задачу
    привела бы к падению job_worker на decrypt_secret(None), а не к
    понятной ошибке. Проверяется здесь, один раз, а не в каждом
    обработчике по отдельности."""
    meter = await db.get(Meter, meter_id)
    if meter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Счётчик не найден")
    if meter.status != MeterStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Счётчик обнаружен автоматически, но ещё не активирован администратором "
            "(POST /{meter_id}/activate) — пароль и протокольный профиль не заданы",
        )
    return meter


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
    meters = list(result.scalars().all())

    # Последнее показание каждого счётчика — одним запросом (DISTINCT ON,
    # Postgres) вместо N+1, список счётчиков может быть большим (Этап 6,
    # 150+ активированных по call-home). Список отображает и других
    # счётчиков помимо ACTIVE (INSTALLED и т.п.), поэтому не сужаем
    # выборку по meter_ids — она и так ограничена самой таблицей readings.
    latest_readings = await db.execute(
        select(MeterReading.meter_id, MeterReading.value_json)
        .distinct(MeterReading.meter_id)
        .order_by(MeterReading.meter_id, MeterReading.read_at.desc())
    )
    values_by_meter_id = dict(latest_readings.all())
    for meter in meters:
        meter.last_reading_value = values_by_meter_id.get(meter.id)
    return meters


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
        is_call_home=body.is_call_home,
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


@router.post("/{meter_id}/activate", response_model=MeterOut)
async def activate_meter(
    meter_id: int,
    body: ActivateMeterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_METERS)),
) -> Meter:
    """Этап 6 — принимает в работу счётчик, автоматически обнаруженный
    по call-home (status=INSTALLED -> ACTIVE), с указанием пароля и
    протокольного профиля — единственных полей, которые Gateway
    принципиально не мог узнать из самого факта звонка домой."""
    meter = await db.get(Meter, meter_id)
    if meter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Счётчик не найден")
    if meter.status == MeterStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Счётчик уже активирован")

    meter.password_encrypted = encrypt_secret(body.password.encode("ascii"))
    meter.protocol_profile = body.protocol_profile
    if body.ip_address is not None:
        meter.ip_address = body.ip_address
    if body.port is not None:
        meter.port = body.port
    if body.location is not None:
        meter.location = body.location
    if body.model is not None:
        meter.model = body.model
    meter.status = MeterStatus.ACTIVE

    await record_audit(
        db, user_id=user.id, action="meter.activate", object_type="meter",
        object_id=str(meter.id), ip_address=request.client.host if request.client else None,
        details={"serial_number": meter.serial_number, "protocol_profile": body.protocol_profile.value},
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
    await _get_active_meter_or_error(db, meter_id)

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


@router.post("/{meter_id}/write-datetime", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def trigger_write_datetime(
    meter_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.WRITE_PARAMETER)),
) -> Job:
    """Ставит операцию установки текущей даты/времени счётчика в очередь
    (ТЗ п.4.2.4, «дата и время счётчика»; п.4.2.11, кнопка «Установить
    время» на карточке счётчика) — синхронизация с системным временем
    Backend (UTC). Доступно ролям «Инженер»/«Администратор»/«Супер-
    администратор» (Permission.WRITE_PARAMETER)."""
    await _get_active_meter_or_error(db, meter_id)

    job = Job(job_type="write_datetime", meter_id=meter_id, payload={}, created_by_id=user.id)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


@router.post("/{meter_id}/disconnect", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def trigger_disconnect(
    meter_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.WRITE_PARAMETER)),
) -> Job:
    """Удалённое отключение счётчика (ТЗ п.4.2.10) — вручную, по одному
    счётчику. Явное подтверждение обеспечивает Frontend (ConfirmModal)
    перед вызовом этого эндпоинта — ввиду физических последствий операции
    (обесточивание потребителя) обязательно по ТЗ."""
    await _get_active_meter_or_error(db, meter_id)

    job = Job(job_type="disconnect", meter_id=meter_id, payload={"source": "web"}, created_by_id=user.id)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


@router.post("/{meter_id}/reconnect", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def trigger_reconnect(
    meter_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.WRITE_PARAMETER)),
) -> Job:
    """Удалённое подключение счётчика (ТЗ п.4.2.10) — вручную, по одному счётчику."""
    await _get_active_meter_or_error(db, meter_id)

    job = Job(job_type="reconnect", meter_id=meter_id, payload={"source": "web"}, created_by_id=user.id)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


@router.post(
    "/{meter_id}/write-parameter/{parameter}", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED
)
async def trigger_write_parameter(
    meter_id: int,
    parameter: str,
    body: WriteParameterRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.WRITE_PARAMETER)),
) -> Job:
    """Ставит запись одиночного параметра из реестра
    ``WRITABLE_INT_PARAMETERS`` в очередь (Этап 2, ТЗ п.4.2.4 —
    «Текущий»/«Доступный номер расчётного периода»)."""
    if parameter not in WRITABLE_INT_PARAMETERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Неизвестный параметр записи: {parameter!r}. Доступные: {sorted(WRITABLE_INT_PARAMETERS)}",
        )
    await _get_active_meter_or_error(db, meter_id)

    job = Job(
        job_type="write_parameter",
        meter_id=meter_id,
        payload={"parameter": parameter, "value": body.value},
        created_by_id=user.id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


@router.get("/{meter_id}/write-history", response_model=list[ParameterWriteHistoryOut])
async def list_write_history(
    meter_id: int,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.VIEW_METERS)),
) -> list[ParameterWriteHistory]:
    """История записи параметров (ТЗ п.4.2.4 — предыдущее/новое значение,
    результат каждой операции) — просмотр доступен всем ролям с правом
    видеть счётчик, запись — только Permission.WRITE_PARAMETER."""
    result = await db.execute(
        select(ParameterWriteHistory)
        .where(ParameterWriteHistory.meter_id == meter_id)
        .order_by(ParameterWriteHistory.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


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


@router.post("/{meter_id}/read-load-profile", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def trigger_read_load_profile(
    meter_id: int,
    body: ReadLoadProfileTriggerRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.TRIGGER_READ)),
) -> Job:
    """Ставит чтение профиля нагрузки за диапазон дат в очередь (Этап 3,
    ТЗ п.4.2.3). Долгая, потенциально многоминутная операция —
    выполняется асинхронно воркером (job_worker._run_read_load_profile),
    строки сохраняются по мере поступления и доступны через
    GET /{meter_id}/load-profile ещё до завершения job (не нужно ждать
    JobStatus.SUCCEEDED, чтобы увидеть уже принятые данные)."""
    await _get_active_meter_or_error(db, meter_id)

    job = Job(
        job_type="read_load_profile",
        meter_id=meter_id,
        payload={"from_iso": body.from_iso, "to_iso": body.to_iso, "obis": body.obis},
        created_by_id=user.id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


@router.get("/{meter_id}/load-profile", response_model=list[LoadProfileRowOut])
async def list_load_profile(
    meter_id: int,
    from_iso: str | None = None,
    to_iso: str | None = None,
    limit: int = 1000,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.VIEW_METERS)),
) -> list[LoadProfileData]:
    query = select(LoadProfileData).where(LoadProfileData.meter_id == meter_id)
    if from_iso:
        query = query.where(LoadProfileData.timestamp >= datetime.fromisoformat(from_iso))
    if to_iso:
        query = query.where(LoadProfileData.timestamp <= datetime.fromisoformat(to_iso))
    result = await db.execute(query.order_by(LoadProfileData.timestamp).limit(limit))
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
