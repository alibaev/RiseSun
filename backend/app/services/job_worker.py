"""Воркер асинхронных задач (Promt_MMWS.md, раздел 3, принцип 3 — длительные
операции не блокируют HTTP-ответ; раздел 2, требование к персистентной
очереди — задачи хранятся в PostgreSQL, не в памяти процесса, переживают
перезапуск Backend'а).

`SELECT ... FOR UPDATE SKIP LOCKED` — безопасный захват задачи при
нескольких экземплярах Backend одновременно (ТЗ п. 4.6, горизонтальное
масштабирование Backend API).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..core.security import decrypt_secret
from ..db import SessionLocal
from ..models import (
    Job,
    JobStatus,
    LoadProfileData,
    Meter,
    MeterReading,
    ParameterWriteHistory,
    ParameterWriteResult,
)
from .audit import record_audit
from .gateway_client import LoadProfileError, read_load_profile, read_register, write_register
from .load_profile import DEFAULT_LOAD_PROFILE_OBIS
from .write_parameters import WRITABLE_INT_PARAMETERS

logger = logging.getLogger("mmws_backend.job_worker")

# Этап 2, ТЗ п.4.2.4 («дата и время счётчика») + п.4.2.11 («Установить
# время»). Словарь OBIS (лист RW_Tree_параметры): «Current Time» и
# «Current Date» — ДВА отдельных объекта класса 1 (Data), не единый
# DLMS Clock (класс 8). Кодировка байт значения — лучшее приближение по
# листу «Типы_данных_кодирование» словаря OBIS (Type_ID=6 «Time»,
# формат hhmmss, 3 байта; Type_ID=5 «Date and Week», формат YYMMDDWW,
# 4 байта; BitType не указан как BCD ни для одной из этих записей, в
# отличие от энергии/тока — значит сырые двоичные байты, не BCD) — НЕ
# подтверждено на реальном оборудовании, требует сверки при живой
# проверке записи (см. DECISIONS.md).
_DATETIME_TIME_OBIS = "1.0.0.9.1.ff"
_DATETIME_DATE_OBIS = "1.0.0.9.2.ff"
_DATA_CLASS_ID = 1


async def _claim_next_job(db: AsyncSession) -> Job | None:
    result = await db.execute(
        select(Job)
        .where(Job.status == JobStatus.QUEUED)
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = result.scalar_one_or_none()
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.started_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(job)
    return job


async def _run_read_current(db: AsyncSession, job: Job) -> None:
    meter = await db.get(Meter, job.meter_id)
    if meter is None:
        job.status = JobStatus.FAILED
        job.error = {"code": "METER_NOT_FOUND", "message": f"Счётчик id={job.meter_id} не найден"}
        job.finished_at = datetime.now(timezone.utc)
        await db.commit()
        return

    gateway = meter.gateway
    obis = job.payload["obis"]
    password = decrypt_secret(meter.password_encrypted).decode("ascii")

    outcome = await read_register(
        grpc_target=gateway.grpc_target if gateway else settings.gateway_grpc_target,
        profile=meter.protocol_profile.value,
        host=meter.ip_address or "",
        port=meter.port or 0,
        call_home=meter.is_call_home,
        serial=meter.serial_number,
        password=password,
        obis=obis,
        # call-home ждёт, пока звонящий счётчик установит и подтвердит
        # соединение (см. callhome.read_via_call_home, max_wait_s=150с —
        # gateway/src/mmws_gateway/grpc_server.py) — даём Backend'у чуть
        # больше времени, чем сам Gateway готов ждать, чтобы не отвалиться
        # раньше него самого.
        call_timeout_s=160.0 if meter.is_call_home else 60.0,
    )

    now = datetime.now(timezone.utc)
    if outcome.ok:
        job.status = JobStatus.SUCCEEDED
        job.result = {"obis": obis, "value": outcome.value}
        db.add(MeterReading(meter_id=meter.id, obis_code=obis, value_json=outcome.value))
        meter.last_seen_at = now
        meter.last_read_at = now
    else:
        job.status = JobStatus.FAILED
        job.error = {
            "code": outcome.error_code,
            "message": outcome.error_message,
            "is_partial": outcome.is_partial,
        }
    job.finished_at = now
    await db.commit()


async def _run_write_datetime(db: AsyncSession, job: Job) -> None:
    """Устанавливает текущее (системное, UTC) время/дату на счётчике —
    ТЗ п.4.2.4/4.2.11. Перед каждой записью пытается прочитать прежнее
    значение (best-effort — используется существующий путь чтения,
    ошибка чтения не прерывает запись, просто ``old_value`` останется
    неизвестным) — обе категории фиксируются в parameter_write_history
    БЕЗУСЛОВНО, независимо от успеха (принцип 2 Promt_MMWS.md, раздел 3)."""
    meter = await db.get(Meter, job.meter_id)
    if meter is None:
        job.status = JobStatus.FAILED
        job.error = {"code": "METER_NOT_FOUND", "message": f"Счётчик id={job.meter_id} не найден"}
        job.finished_at = datetime.now(timezone.utc)
        await db.commit()
        return

    gateway = meter.gateway
    grpc_target = gateway.grpc_target if gateway else settings.gateway_grpc_target
    password = decrypt_secret(meter.password_encrypted).decode("ascii")
    now = datetime.now(timezone.utc)

    entries = [
        ("datetime.time", _DATETIME_TIME_OBIS, bytes([now.hour, now.minute, now.second])),
        ("datetime.date", _DATETIME_DATE_OBIS, bytes([now.year % 100, now.month, now.day, now.isoweekday()])),
    ]

    overall_ok = True
    results: dict[str, dict] = {}
    for parameter, obis, value_bytes in entries:
        old_read = await read_register(
            grpc_target=grpc_target,
            profile=meter.protocol_profile.value,
            host=meter.ip_address or "",
            port=meter.port or 0,
            call_home=meter.is_call_home,
            serial=meter.serial_number,
            password=password,
            obis=obis,
            class_id=_DATA_CLASS_ID,
            call_timeout_s=60.0,
        )
        old_value = old_read.value if old_read.ok else None

        outcome = await write_register(
            grpc_target=grpc_target,
            profile=meter.protocol_profile.value,
            host=meter.ip_address or "",
            port=meter.port or 0,
            call_home=meter.is_call_home,
            serial=meter.serial_number,
            password=password,
            obis=obis,
            class_id=_DATA_CLASS_ID,
            value_bytes=value_bytes,
            call_timeout_s=60.0,
        )
        new_value = value_bytes.hex()
        result = ParameterWriteResult.SUCCESS if outcome.ok else ParameterWriteResult.FAILURE
        db.add(
            ParameterWriteHistory(
                user_id=job.created_by_id,
                meter_id=meter.id,
                parameter=parameter,
                obis_code=obis,
                old_value=old_value,
                new_value=new_value,
                result=result,
                error_message=None if outcome.ok else outcome.error_message,
                job_id=job.id,
            )
        )
        await record_audit(
            db,
            user_id=job.created_by_id,
            action="meter.write_parameter",
            object_type="meter",
            object_id=str(meter.id),
            result="success" if outcome.ok else "failure",
            source="system",
            details={"parameter": parameter, "obis": obis, "old_value": old_value, "new_value": new_value},
        )
        results[parameter] = {"ok": outcome.ok, "error": None if outcome.ok else outcome.error_message}
        if not outcome.ok:
            overall_ok = False

    job.status = JobStatus.SUCCEEDED if overall_ok else JobStatus.FAILED
    job.result = results
    if not overall_ok:
        job.error = {
            "code": "WRITE_FAILED",
            "message": "Не удалось записать один или оба параметра даты/времени",
        }
    job.finished_at = datetime.now(timezone.utc)
    await db.commit()


async def _run_write_parameter(db: AsyncSession, job: Job) -> None:
    """Запись одиночного параметра из реестра ``WRITABLE_INT_PARAMETERS``
    (Этап 2, ТЗ п.4.2.4 — «Текущий/доступный номер расчётного периода»,
    единственные ещё не реализованные записываемые объекты словаря
    OBIS). Тот же принцип, что и у ``_run_write_datetime``: старое
    значение читается best-effort, запись фиксируется в
    parameter_write_history/audit_log безусловно."""
    meter = await db.get(Meter, job.meter_id)
    if meter is None:
        job.status = JobStatus.FAILED
        job.error = {"code": "METER_NOT_FOUND", "message": f"Счётчик id={job.meter_id} не найден"}
        job.finished_at = datetime.now(timezone.utc)
        await db.commit()
        return

    parameter = job.payload["parameter"]
    value = job.payload["value"]
    spec = WRITABLE_INT_PARAMETERS[parameter]

    gateway = meter.gateway
    grpc_target = gateway.grpc_target if gateway else settings.gateway_grpc_target
    password = decrypt_secret(meter.password_encrypted).decode("ascii")

    old_read = await read_register(
        grpc_target=grpc_target,
        profile=meter.protocol_profile.value,
        host=meter.ip_address or "",
        port=meter.port or 0,
        call_home=meter.is_call_home,
        serial=meter.serial_number,
        password=password,
        obis=spec.obis,
        class_id=spec.class_id,
        call_timeout_s=60.0,
    )
    old_value = old_read.value if old_read.ok else None

    outcome = await write_register(
        grpc_target=grpc_target,
        profile=meter.protocol_profile.value,
        host=meter.ip_address or "",
        port=meter.port or 0,
        call_home=meter.is_call_home,
        serial=meter.serial_number,
        password=password,
        obis=spec.obis,
        class_id=spec.class_id,
        value_bytes=bytes([value]),
        value_type=spec.value_type,
        call_timeout_s=60.0,
    )
    result = ParameterWriteResult.SUCCESS if outcome.ok else ParameterWriteResult.FAILURE
    db.add(
        ParameterWriteHistory(
            user_id=job.created_by_id,
            meter_id=meter.id,
            parameter=parameter,
            obis_code=spec.obis,
            old_value=old_value,
            new_value=value,
            result=result,
            error_message=None if outcome.ok else outcome.error_message,
            job_id=job.id,
        )
    )
    await record_audit(
        db,
        user_id=job.created_by_id,
        action="meter.write_parameter",
        object_type="meter",
        object_id=str(meter.id),
        result="success" if outcome.ok else "failure",
        source="system",
        details={"parameter": parameter, "obis": spec.obis, "old_value": old_value, "new_value": value},
    )

    job.status = JobStatus.SUCCEEDED if outcome.ok else JobStatus.FAILED
    job.result = {"parameter": parameter, "value": value, "ok": outcome.ok}
    if not outcome.ok:
        job.error = {"code": outcome.error_code, "message": outcome.error_message}
    job.finished_at = datetime.now(timezone.utc)
    await db.commit()


_LOAD_PROFILE_COMMIT_BATCH = 20


async def _run_read_load_profile(db: AsyncSession, job: Job) -> None:
    """Читает профиль нагрузки (Этап 3, ТЗ п.4.2.3) и сохраняет строки по
    мере поступления (не дожидаясь конца передачи — генератор
    ``gateway_client.read_load_profile`` отдаёт их сразу же). Периодический
    коммит каждые ``_LOAD_PROFILE_COMMIT_BATCH`` строк ограничивает, сколько
    уже принятых данных можно потерять при аварийном падении самого
    процесса воркера (не просто пойманном исключении — на пойманное
    исключение ``ON CONFLICT DO NOTHING`` уже вставленные, но
    незакоммиченные строки всё равно сохранит commit в конце). Вставка
    идемпотентна (уникальность meter_id+obis_code+timestamp) — повторный
    job с тем же диапазоном дат («докачка» после обрыва, is_partial) не
    создаёт дублей."""
    meter = await db.get(Meter, job.meter_id)
    if meter is None:
        job.status = JobStatus.FAILED
        job.error = {"code": "METER_NOT_FOUND", "message": f"Счётчик id={job.meter_id} не найден"}
        job.finished_at = datetime.now(timezone.utc)
        await db.commit()
        return

    gateway = meter.gateway
    grpc_target = gateway.grpc_target if gateway else settings.gateway_grpc_target
    password = decrypt_secret(meter.password_encrypted).decode("ascii")
    obis = job.payload.get("obis") or DEFAULT_LOAD_PROFILE_OBIS
    from_iso = job.payload["from_iso"]
    to_iso = job.payload["to_iso"]

    rows_written = 0
    error_info: dict | None = None
    try:
        async for row in read_load_profile(
            grpc_target=grpc_target,
            profile=meter.protocol_profile.value,
            host=meter.ip_address or "",
            port=meter.port or 0,
            call_home=meter.is_call_home,
            serial=meter.serial_number,
            password=password,
            obis=obis,
            from_iso=from_iso,
            to_iso=to_iso,
            call_timeout_s=160.0 if meter.is_call_home else 180.0,
        ):
            stmt = (
                pg_insert(LoadProfileData)
                .values(
                    meter_id=meter.id,
                    obis_code=obis,
                    timestamp=datetime.fromisoformat(row.timestamp_iso),
                    values_json=row.values,
                    job_id=job.id,
                )
                .on_conflict_do_nothing(constraint="uq_load_profile_row")
            )
            await db.execute(stmt)
            rows_written += 1
            if rows_written % _LOAD_PROFILE_COMMIT_BATCH == 0:
                job.result = {"obis": obis, "rows_written": rows_written}
                await db.commit()
    except LoadProfileError as exc:
        error_info = {"code": exc.code, "message": exc.message, "is_partial": exc.is_partial or rows_written > 0}

    now = datetime.now(timezone.utc)
    job.result = {"obis": obis, "rows_written": rows_written}
    if error_info is None:
        job.status = JobStatus.SUCCEEDED
        meter.last_seen_at = now
    else:
        job.status = JobStatus.FAILED
        job.error = error_info
    job.finished_at = now
    await db.commit()


_JOB_HANDLERS = {
    "read_current": _run_read_current,
    "write_datetime": _run_write_datetime,
    "write_parameter": _run_write_parameter,
    "read_load_profile": _run_read_load_profile,
}


async def _process_one(db: AsyncSession) -> bool:
    job = await _claim_next_job(db)
    if job is None:
        return False
    handler = _JOB_HANDLERS.get(job.job_type)
    try:
        if handler is None:
            raise ValueError(f"Неизвестный тип задачи: {job.job_type}")
        await handler(db, job)
    except Exception as exc:  # noqa: BLE001 — воркер не должен падать целиком из-за одной задачи
        logger.exception("Задача id=%s завершилась необработанной ошибкой", job.id)
        job.status = JobStatus.FAILED
        job.error = {"code": "WORKER_ERROR", "message": str(exc)}
        job.finished_at = datetime.now(timezone.utc)
        await db.commit()
    return True


async def worker_loop(stop_event: asyncio.Event) -> None:
    logger.info("Воркер задач запущен (poll interval=%.1fs)", settings.job_poll_interval_s)
    while not stop_event.is_set():
        async with SessionLocal() as db:
            processed = await _process_one(db)
        if not processed:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.job_poll_interval_s)
            except asyncio.TimeoutError:
                pass
    logger.info("Воркер задач остановлен")
