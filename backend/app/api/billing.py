"""ТЗ п.4.2.9/4.2.10, реализовано строго по техрегламенту API.docx —
сегмент REST API для внешней биллинговой системы (/api/v1/billing/*).
Отдельная аутентификация по API-ключу (app/auth/billing_deps.py), не
JWT-сессия веб-интерфейса — Permission из app/core/permissions.py здесь
не участвует вовсе."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.billing_deps import require_billing_api_key
from ..db import get_db
from ..models import (
    BillingApiKey,
    DisconnectBatch,
    DisconnectBatchItem,
    DisconnectBatchItemStatus,
    DisconnectBatchOperation,
    DisconnectBatchStatus,
    Job,
    Meter,
    MeterReading,
)
from ..schemas import (
    DisconnectBatchAccepted,
    DisconnectBatchItemOut,
    DisconnectBatchRequest,
    DisconnectBatchStatusOut,
)
from ..services.audit import record_audit
from ..services.billing_errors import BillingApiError
from ..services.disconnect_batches import finalize_batch_if_complete, generate_batch_id

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])

_MAX_PAGE_SIZE = 2000
_DEFAULT_PAGE_SIZE = 500
_MAX_DATE_RANGE_DAYS = 31
_MAX_BATCH_SIZE = 500


def _parse_iso(value: str, *, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise BillingApiError(400, "INVALID_DATE_RANGE", f"{field} должен быть датой ISO 8601: {value!r}")


def _paginate(page: int, page_size: int) -> tuple[int, int]:
    if page < 1:
        raise BillingApiError(400, "VALIDATION_ERROR", "page должен быть >= 1")
    if page_size < 1 or page_size > _MAX_PAGE_SIZE:
        raise BillingApiError(400, "VALIDATION_ERROR", f"page_size должен быть от 1 до {_MAX_PAGE_SIZE}")
    return page, page_size


@router.get("/readings")
async def get_readings(
    date_from: str,
    date_to: str,
    meter_ids: str | None = None,
    region: str | None = None,
    page: int = 1,
    page_size: int = _DEFAULT_PAGE_SIZE,
    db: AsyncSession = Depends(get_db),
    api_key: BillingApiKey = Depends(require_billing_api_key),
) -> dict:
    """API.docx п.4.1. ``date_from``/``date_to`` — ISO 8601 с указанием
    часового пояса; диапазон не более 31 суток (раздел 6)."""
    page, page_size = _paginate(page, page_size)
    from_dt = _parse_iso(date_from, field="date_from")
    to_dt = _parse_iso(date_to, field="date_to")
    if from_dt >= to_dt:
        raise BillingApiError(400, "INVALID_DATE_RANGE", "date_from must be earlier than date_to")
    if to_dt - from_dt > timedelta(days=_MAX_DATE_RANGE_DAYS):
        raise BillingApiError(
            400, "INVALID_DATE_RANGE", f"Диапазон дат не должен превышать {_MAX_DATE_RANGE_DAYS} суток"
        )

    query = (
        select(MeterReading, Meter.serial_number)
        .join(Meter, MeterReading.meter_id == Meter.id)
        .where(MeterReading.read_at >= from_dt, MeterReading.read_at <= to_dt)
    )
    if meter_ids:
        query = query.where(Meter.serial_number.in_([s.strip() for s in meter_ids.split(",") if s.strip()]))
    if region:
        query = query.where(Meter.location == region)

    total = len((await db.execute(query)).all())
    rows = (
        await db.execute(query.order_by(MeterReading.read_at).offset((page - 1) * page_size).limit(page_size))
    ).all()

    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": [
            {
                "meter_serial": serial,
                "obis_code": reading.obis_code,
                "value": reading.value_json,
                "unit": reading.unit,
                "read_at": reading.read_at.isoformat(),
            }
            for reading, serial in rows
        ],
    }


@router.get("/tariffs")
async def get_tariffs(
    meter_ids: str | None = None,
    region: str | None = None,
    page: int = 1,
    page_size: int = _DEFAULT_PAGE_SIZE,
    db: AsyncSession = Depends(get_db),
    api_key: BillingApiKey = Depends(require_billing_api_key),
) -> dict:
    """API.docx п.4.2. Обязателен ``meter_ids`` либо ``region``.

    ВАЖНО: сейчас всегда возвращает пустой ``items`` — чтение тарифного
    расписания со счётчика (полной таблицы зон/ставок) не реализовано ни
    в одном из этапов (Этап 2 реализовал только "weekend_rate_type" —
    один селектор типа тарифа выходного дня, не полную таблицу зон,
    представимую в формате API.docx). Контракт эндпоинта (аутентификация,
    валидация параметров, пагинация, формат ответа) реализован полностью
    и готов к подключению реального источника данных — см. DECISIONS.md."""
    page, page_size = _paginate(page, page_size)
    if not meter_ids and not region:
        raise BillingApiError(400, "VALIDATION_ERROR", "Требуется meter_ids либо region")
    return {"page": page, "page_size": page_size, "total": 0, "items": []}


@router.post("/disconnect-batch", response_model=DisconnectBatchAccepted, status_code=202)
async def create_disconnect_batch(
    body: DisconnectBatchRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    api_key: BillingApiKey = Depends(require_billing_api_key),
) -> DisconnectBatchAccepted:
    return await _create_batch(
        DisconnectBatchOperation.DISCONNECT, body, request, idempotency_key, db, api_key
    )


@router.post("/reconnect-batch", response_model=DisconnectBatchAccepted, status_code=202)
async def create_reconnect_batch(
    body: DisconnectBatchRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    api_key: BillingApiKey = Depends(require_billing_api_key),
) -> DisconnectBatchAccepted:
    return await _create_batch(
        DisconnectBatchOperation.RECONNECT, body, request, idempotency_key, db, api_key
    )


async def _create_batch(
    operation: DisconnectBatchOperation,
    body: DisconnectBatchRequest,
    request: Request,
    idempotency_key: str | None,
    db: AsyncSession,
    api_key: BillingApiKey,
) -> DisconnectBatchAccepted:
    if not idempotency_key:
        raise BillingApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Заголовок Idempotency-Key обязателен")
    if len(body.meters) > _MAX_BATCH_SIZE:
        raise BillingApiError(
            400, "BATCH_TOO_LARGE", f"Размер пакета превышает лимит {_MAX_BATCH_SIZE} счётчиков"
        )

    existing = (
        await db.execute(
            select(DisconnectBatch).where(
                DisconnectBatch.api_key_id == api_key.id, DisconnectBatch.idempotency_key == idempotency_key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.operation != operation or set(
            (await db.execute(select(DisconnectBatchItem.meter_serial).where(DisconnectBatchItem.batch_id == existing.id)))
            .scalars()
            .all()
        ) != set(body.meters):
            raise BillingApiError(
                400, "DUPLICATE_IDEMPOTENCY_KEY",
                "Idempotency-Key уже использован с иным содержимым запроса",
            )
        # Тот же клиент повторяет тот же пакет (например, после сетевого
        # таймаута, API.docx п.2.3) — отдаём уже принятый пакет как есть,
        # не ставя его в очередь повторно.
        return DisconnectBatchAccepted(
            batch_id=existing.batch_id, accepted_count=len(body.meters), status=existing.status,
            status_url=f"/api/v1/billing/disconnect-batch/{existing.batch_id}",
        )

    meters = (
        await db.execute(select(Meter).where(Meter.serial_number.in_(body.meters)))
    ).scalars().all()
    meters_by_serial = {m.serial_number: m for m in meters}

    batch = DisconnectBatch(
        batch_id=generate_batch_id(), operation=operation, idempotency_key=idempotency_key,
        api_key_id=api_key.id, status=DisconnectBatchStatus.QUEUED, reason=body.reason,
    )
    db.add(batch)
    await db.flush()

    job_type = "disconnect" if operation == DisconnectBatchOperation.DISCONNECT else "reconnect"
    for serial in body.meters:
        meter = meters_by_serial.get(serial)
        if meter is None:
            db.add(
                DisconnectBatchItem(
                    batch_id=batch.id, meter_serial=serial,
                    status=DisconnectBatchItemStatus.FAILED, error_code="METER_NOT_FOUND",
                )
            )
            continue
        job = Job(
            job_type=job_type, meter_id=meter.id,
            payload={"source": "billing", "batch_id": batch.batch_id},
        )
        db.add(job)
        await db.flush()
        db.add(
            DisconnectBatchItem(
                batch_id=batch.id, meter_serial=serial, meter_id=meter.id,
                status=DisconnectBatchItemStatus.QUEUED, job_id=job.id,
            )
        )

    await db.flush()
    await finalize_batch_if_complete(db, batch.id)  # покрывает случай "все серийные номера не найдены"

    await record_audit(
        db, user_id=None, action=f"billing.{job_type}_batch", object_type="disconnect_batch",
        object_id=batch.batch_id, source="billing",
        ip_address=request.client.host if request.client else None,
        details={"api_key_client_id": api_key.client_id, "meter_count": len(body.meters), "reason": body.reason},
    )
    await db.commit()

    return DisconnectBatchAccepted(
        batch_id=batch.batch_id, accepted_count=len(body.meters), status=batch.status,
        status_url=f"/api/v1/billing/disconnect-batch/{batch.batch_id}",
    )


@router.get("/disconnect-batch/{batch_id}", response_model=DisconnectBatchStatusOut)
async def get_batch_status(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    api_key: BillingApiKey = Depends(require_billing_api_key),
) -> DisconnectBatchStatusOut:
    """API.docx п.4.5 — единый механизм статуса для disconnect и reconnect."""
    batch = (await db.execute(select(DisconnectBatch).where(DisconnectBatch.batch_id == batch_id))).scalar_one_or_none()
    if batch is None:
        raise BillingApiError(404, "BATCH_NOT_FOUND", f"Пакет {batch_id!r} не найден")

    items = (
        await db.execute(select(DisconnectBatchItem).where(DisconnectBatchItem.batch_id == batch.id))
    ).scalars().all()

    return DisconnectBatchStatusOut(
        batch_id=batch.batch_id,
        operation=batch.operation.value,
        status=batch.status,
        created_at=batch.created_at,
        finished_at=batch.finished_at,
        items=[
            DisconnectBatchItemOut(meter_serial=i.meter_serial, status=i.status, error_code=i.error_code)
            for i in items
        ],
    )
