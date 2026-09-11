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
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..core.security import decrypt_secret
from ..db import SessionLocal
from ..models import (
    DisconnectBatchItem,
    DisconnectBatchItemStatus,
    Job,
    JobStatus,
    LoadProfileData,
    Meter,
    MeterReading,
    NotificationCategory,
    ParameterWriteHistory,
    ParameterWriteResult,
    ScheduledJob,
    ScheduledJobRun,
    ScheduledJobRunStatus,
)
from .audit import record_audit
from .disconnect_batches import finalize_batch_if_complete
from .gateway_client import LoadProfileError, disconnect_meter, read_load_profile, read_register, write_register
from .load_profile import DEFAULT_LOAD_PROFILE_OBIS
from .notifications import create_notification
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


# Эксперимент "эмуляция ver2.zip" (2026-09-10, см. DECISIONS.md) завершён
# и дал отрицательный результат — исключение снято, множество пустое.
# Оставлено (не удалено) на случай повторного точечного эксперимента.
_VER2_EMULATION_EXPERIMENT_EXCLUDED_METER_IDS: set[int] = set()


async def _claim_next_job(db: AsyncSession) -> Job | None:
    result = await db.execute(
        select(Job)
        .where(
            Job.status == JobStatus.QUEUED,
            Job.meter_id.not_in(_VER2_EMULATION_EXPERIMENT_EXCLUDED_METER_IDS),
        )
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


async def claim_due_jobs_for_meter(
    db: AsyncSession, *, meter_id: int, job_types: list[str], limit: int
) -> list[Job]:
    """Аналог ``_claim_next_job``, но фильтрован по конкретному счётчику
    и забирает СРАЗУ НЕСКОЛЬКО job'ов (до ``limit``) — используется
    событийным путём (``POST /api/internal/gateway/meters/{serial}/
    claim-jobs``, см. DECISIONS.md и план ticklish-popping-bear.md),
    когда Gateway спрашивает "что читать" в момент подключения счётчика,
    а не когда воркер сам доходит до job'ы по FIFO.

    ``FOR UPDATE SKIP LOCKED`` даёт ту же гарантию неповторного захвата,
    что и у ``_claim_next_job`` — если этот же счётчик одновременно
    обрабатывается старым путём (маловероятная, но возможная гонка: две
    почти одновременные call-home попытки того же серийника, см.
    docstring ``callhome.py`` про частые переподключения), совпадающие
    job'ы просто не попадут в выборку второго вызова (уже не QUEUED)."""
    result = await db.execute(
        select(Job)
        .where(Job.meter_id == meter_id, Job.job_type.in_(job_types), Job.status == JobStatus.QUEUED)
        .order_by(Job.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    jobs = result.scalars().all()
    now = datetime.now(timezone.utc)
    for job in jobs:
        job.status = JobStatus.RUNNING
        job.started_at = now
    if jobs:
        await db.commit()
        for job in jobs:
            await db.refresh(job)
    return list(jobs)


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
    # class_id — необязательное поле payload (0 = дефолт Gateway, класс
    # Register), добавлено 2026-09-10 для точечных диагностических
    # чтений произвольных объектов (напр. Association View, класс 15) —
    # тот же параметр, что gateway_client.read_register уже поддерживал
    # для write/read-load-profile путей, просто раньше не пробрасывался
    # из payload обычного read_current.
    class_id = job.payload.get("class_id", 0)
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
        class_id=class_id,
        # call-home ждёт, пока звонящий счётчик установит и подтвердит
        # соединение (см. callhome.read_via_call_home, max_wait_s=150с —
        # gateway/src/mmws_gateway/grpc_server.py) — даём Backend'у чуть
        # больше времени, чем сам Gateway готов ждать, чтобы не отвалиться
        # раньше него самого.
        call_timeout_s=160.0 if meter.is_call_home else 60.0,
    )
    await finalize_read_current_job(db, job, meter, outcome, obis=obis)


async def finalize_read_current_job(db: AsyncSession, job: Job, meter: Meter, outcome, *, obis: str) -> None:
    """Вынесено из ``_run_read_current`` (2026-09-09, событийное чтение
    call-home — см. DECISIONS.md и план ticklish-popping-bear.md), чтобы
    ОДНА и та же логика финализации job'ы использовалась и старым путём
    (этот воркер, через gRPC), и новым эндпоинтом
    ``POST /api/internal/gateway/job-results`` — поведение старого пути
    не меняется, это чистый рефакторинг."""
    now = datetime.now(timezone.utc)
    if outcome.ok:
        job.status = JobStatus.SUCCEEDED
        job.result = {"obis": obis, "value": outcome.value}
        db.add(MeterReading(meter_id=meter.id, obis_code=obis, value_json=outcome.value))
        meter.last_seen_at = now
        meter.last_read_at = now
        job.finished_at = now
    elif outcome.error_code == "NO_CONNECTION_YET":
        # Счётчик не звонил вообще, пока этот (старый FIFO) путь ждал —
        # не значит, что он мёртв, просто ещё не позвонил. Терминально
        # проваливать job здесь означало бы выбросить реальный шанс,
        # который вот-вот достанется событийному чтению на следующий
        # звонок (см. DECISIONS.md, 2026-09-10) — возвращаем в очередь
        # вместо FAILED, job.finished_at НЕ трогаем (job не завершён).
        job.status = JobStatus.QUEUED
        job.started_at = None
    else:
        job.status = JobStatus.FAILED
        job.error = {
            "code": outcome.error_code,
            "message": outcome.error_message,
            "is_partial": outcome.is_partial,
        }
        job.finished_at = now
    await db.commit()


# Maximum Current (Imax), OBIS.xlsx (Read_Tree_полное_дерево, строка 706
# `0.6.3`) — гипотеза токового класса счётчика (100А vs 5А), переведена
# в полный OBIS по правилу, подтверждённому на регистре энергии
# (`C.D.E` -> `1.1.C.D.E.255`), но САМА эта гипотеза live-трафиком не
# подтверждена (см. DECISIONS.md, 2026-09-07) — читается один раз на
# счётчик (см. scheduler.py, job_type="read_rated_current"), результат
# сохраняется на Meter, а не в meter_readings (это не показание, а
# статичный паспортный параметр).
RATED_CURRENT_OBIS = "1.1.0.6.3.ff"

# Допустимые номиналы тока для этого парка счётчиков — по прямому указанию
# пользователя (2026-09-11): "Ампер может быть - 5, 7.5, 100, другие значения
# ошибочные, нужен повторный запрос". Значение вне этого множества трактуется
# как ошибка протокола/декодирования (например, ранее встречались -4 и 0 —
# физически невозможные номиналы), а не как достоверный паспортный параметр —
# тот же принцип "лучше вообще без данных", что и в чтении показаний
# (см. DECISIONS.md, hdlc_dlms.py). Job в этом случае падает FAILED,
# rated_current_amps НЕ записывается и остаётся None — это заставляет
# планировщик (scheduler.py: `rated_current_amps is None`) повторить попытку
# на следующем звонке счётчика, а не запомнить ошибочное значение навсегда.
VALID_RATED_CURRENT_AMPS = (5, 7.5, 100)


def _is_valid_rated_current(value: float) -> bool:
    return any(abs(value - valid) < 0.01 for valid in VALID_RATED_CURRENT_AMPS)


async def _run_read_rated_current(db: AsyncSession, job: Job) -> None:
    meter = await db.get(Meter, job.meter_id)
    if meter is None:
        job.status = JobStatus.FAILED
        job.error = {"code": "METER_NOT_FOUND", "message": f"Счётчик id={job.meter_id} не найден"}
        job.finished_at = datetime.now(timezone.utc)
        await db.commit()
        return

    gateway = meter.gateway
    password = decrypt_secret(meter.password_encrypted).decode("ascii")

    outcome = await read_register(
        grpc_target=gateway.grpc_target if gateway else settings.gateway_grpc_target,
        profile=meter.protocol_profile.value,
        host=meter.ip_address or "",
        port=meter.port or 0,
        call_home=meter.is_call_home,
        serial=meter.serial_number,
        password=password,
        obis=RATED_CURRENT_OBIS,
        call_timeout_s=160.0 if meter.is_call_home else 60.0,
    )
    await finalize_read_rated_current_job(db, job, meter, outcome)


async def finalize_read_rated_current_job(db: AsyncSession, job: Job, meter: Meter, outcome) -> None:
    """См. docstring ``finalize_read_current_job`` — тот же принцип
    (используется и старым воркером, и новым эндпоинтом job-results)."""
    now = datetime.now(timezone.utc)
    if outcome.ok and isinstance(outcome.value, (int, float)) and not _is_valid_rated_current(float(outcome.value)):
        job.status = JobStatus.FAILED
        job.error = {
            "code": "IMPLAUSIBLE_RATED_CURRENT",
            "message": f"Получено {outcome.value!r} — вне допустимого множества {VALID_RATED_CURRENT_AMPS}",
            "is_partial": False,
        }
        meter.last_seen_at = now
    elif outcome.ok and isinstance(outcome.value, (int, float)):
        job.status = JobStatus.SUCCEEDED
        job.result = {"obis": RATED_CURRENT_OBIS, "rated_current_amps": outcome.value}
        meter.rated_current_amps = float(outcome.value)
        meter.last_seen_at = now
    elif outcome.ok:
        job.status = JobStatus.FAILED
        job.error = {
            "code": "UNEXPECTED_VALUE_TYPE",
            "message": f"Ожидалось число, получено {outcome.value!r}",
            "is_partial": False,
        }
    elif outcome.error_code == "NO_CONNECTION_YET":
        # См. finalize_read_current_job — тот же принцип, не хороним job,
        # возвращаем в очередь на следующий звонок счётчика.
        job.status = JobStatus.QUEUED
        job.started_at = None
        await db.commit()
        return
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
            # 220с (не 160с, как у read_current) — живая проверка
            # 2026-08-19 показала, что многошаговый обмен профиля
            # нагрузки (AARQ/AARE + GET capture_period + GET с
            # диапазоном) на одном call-home соединении может упереться
            # в association_timeout_ms (45с) уже ПОСЛЕ того, как
            # внутренний max_wait_s=150с Gateway формально истёк (сам
            # цикл проверяет дедлайн только МЕЖДУ попытками, не обрывает
            # уже начатое ожидание ответа) — без запаса клиентский gRPC
            # deadline обрывал вызов раньше, чем Gateway успевал отдать
            # собственную, понятную ошибку.
            call_timeout_s=220.0 if meter.is_call_home else 180.0,
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
    if error_info is not None and error_info["code"] == "NO_CONNECTION_YET":
        # См. finalize_read_current_job — тот же принцип (2026-09-10, не
        # распространили сразу и сюда): счётчик не звонил, пока этот
        # путь ждал, не значит, что он мёртв — возвращаем в очередь на
        # следующий звонок вместо терминального FAILED. Уже записанные
        # строки (если были) не теряются — вставка идемпотентна
        # (ON CONFLICT DO NOTHING по meter_id+obis_code+timestamp),
        # повторный проход с тем же диапазоном дат безопасен.
        job.status = JobStatus.QUEUED
        job.started_at = None
        job.result = {"obis": obis, "rows_written": rows_written}
        await db.commit()
        return

    job.result = {"obis": obis, "rows_written": rows_written}
    if error_info is None:
        job.status = JobStatus.SUCCEEDED
        meter.last_seen_at = now
    else:
        job.status = JobStatus.FAILED
        job.error = error_info
    job.finished_at = now
    await db.commit()


async def _run_disconnect_operation(db: AsyncSession, job: Job, *, operation: str) -> None:
    """Удалённое отключение/подключение (Этап 5, ТЗ п.4.2.10) — общий
    обработчик для job_type «disconnect»/«reconnect», используется и
    ручным запуском с карточки счётчика, и пакетной операцией от
    биллинга (см. app/api/billing.py). ``job.payload["source"]`` —
    "web" | "billing", проставляется на этапе постановки задачи в
    очередь, определяет категорию источника в audit_log (ТЗ п.4.2.10:
    «указанием источника инициации — пользователь веб-интерфейса либо
    идентификатор пакета от биллинга»)."""
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

    outcome = await disconnect_meter(
        grpc_target=grpc_target,
        profile=meter.protocol_profile.value,
        host=meter.ip_address or "",
        port=meter.port or 0,
        call_home=meter.is_call_home,
        serial=meter.serial_number,
        password=password,
        operation=operation,
        call_timeout_s=160.0 if meter.is_call_home else 60.0,
    )

    job.status = JobStatus.SUCCEEDED if outcome.ok else JobStatus.FAILED
    job.result = {"operation": operation, "ok": outcome.ok}
    if not outcome.ok:
        job.error = {"code": outcome.error_code, "message": outcome.error_message}
    job.finished_at = datetime.now(timezone.utc)

    await record_audit(
        db,
        user_id=job.created_by_id,
        action=f"meter.{operation}",
        object_type="meter",
        object_id=str(meter.id),
        result="success" if outcome.ok else "failure",
        source=job.payload.get("source", "web"),
        details={
            "serial": meter.serial_number,
            "batch_id": job.payload.get("batch_id"),
            "error": None if outcome.ok else outcome.error_message,
        },
    )
    await db.commit()


async def _run_disconnect(db: AsyncSession, job: Job) -> None:
    await _run_disconnect_operation(db, job, operation="disconnect")


async def _run_reconnect(db: AsyncSession, job: Job) -> None:
    await _run_disconnect_operation(db, job, operation="reconnect")


_JOB_HANDLERS = {
    "read_current": _run_read_current,
    "write_datetime": _run_write_datetime,
    "write_parameter": _run_write_parameter,
    "read_load_profile": _run_read_load_profile,
    "disconnect": _run_disconnect,
    "reconnect": _run_reconnect,
    "read_rated_current": _run_read_rated_current,
}


async def _maybe_finalize_scheduled_job_run(db: AsyncSession, scheduled_job_run_id: int) -> None:
    """Этап 4 (ТЗ п.4.2.6/4.2.11 — «журнал выполнения каждого запуска»):
    один запуск расписания порождает по одной Job на каждый счётчик
    группы; как только ПОСЛЕДНЯЯ из них завершается (успешно или с
    ошибкой), сводим итог по всему запуску и — при наличии ошибок —
    создаём уведомление (ТЗ п.4.2.8). При нескольких экземплярах Backend
    возможна редкая гонка (два экземпляра почти одновременно видят
    «все дочерние задачи завершены») — проверка ``run.status !=
    RUNNING`` защищает от двойной финализации/уведомления в
    подавляющем большинстве случаев; для PoC-масштаба системы
    дополнительная блокировка сочтена избыточной."""
    siblings = (
        await db.execute(select(Job).where(Job.scheduled_job_run_id == scheduled_job_run_id))
    ).scalars().all()
    if any(j.status in (JobStatus.QUEUED, JobStatus.RUNNING) for j in siblings):
        return

    run = await db.get(ScheduledJobRun, scheduled_job_run_id)
    if run is None or run.status != ScheduledJobRunStatus.RUNNING:
        return

    succeeded = sum(1 for j in siblings if j.status == JobStatus.SUCCEEDED)
    failed = sum(1 for j in siblings if j.status == JobStatus.FAILED)
    run.meters_succeeded = succeeded
    run.meters_failed = failed
    run.finished_at = datetime.now(timezone.utc)
    if failed == 0:
        run.status = ScheduledJobRunStatus.SUCCEEDED
    elif succeeded == 0:
        run.status = ScheduledJobRunStatus.FAILED
    else:
        run.status = ScheduledJobRunStatus.PARTIAL_FAILURE

    if failed > 0:
        scheduled_job = await db.get(ScheduledJob, run.scheduled_job_id)
        name = scheduled_job.name if scheduled_job is not None else str(run.scheduled_job_id)
        await create_notification(
            db,
            category=NotificationCategory.SCHEDULED_JOB_FAILED,
            message=f"Расписание «{name}»: {failed} из {len(siblings)} задач завершились с ошибкой",
            scheduled_job_id=run.scheduled_job_id,
            details={"run_id": run.id, "succeeded": succeeded, "failed": failed},
        )
    await db.commit()


async def _maybe_finalize_disconnect_batch_item(db: AsyncSession, job: Job) -> None:
    """Этап 5 (ТЗ п.4.2.10, API.docx п.4.5) — если задача была порождена
    пакетной операцией биллинга (см. app/api/billing.py), обновляет
    статус соответствующего DisconnectBatchItem и, если это была
    последняя незавершённая задача пакета, финализирует сам пакет (тот
    же принцип, что и у _maybe_finalize_scheduled_job_run в Этапе 4).
    Для job'ов без связанного DisconnectBatchItem (ручной запуск с
    карточки счётчика) — no-op."""
    item = (
        await db.execute(select(DisconnectBatchItem).where(DisconnectBatchItem.job_id == job.id))
    ).scalar_one_or_none()
    if item is None:
        return
    item.status = DisconnectBatchItemStatus.DONE if job.status == JobStatus.SUCCEEDED else DisconnectBatchItemStatus.FAILED
    if job.status == JobStatus.FAILED and job.error:
        item.error_code = job.error.get("code")
    await db.flush()
    await finalize_batch_if_complete(db, item.batch_id)
    await db.commit()


async def reap_stale_running_jobs(db: AsyncSession) -> int:
    """Вызывается ОДИН раз при старте Backend (main.py, до запуска
    воркеров). ``RUNNING`` — статус, валидный только пока job реально
    держит в памяти какой-то воркер текущего процесса; свежий процесс
    не мог создать ни одной такой записи, значит ЛЮБАЯ строка со
    статусом RUNNING на момент старта — гарантированно осиротевшая (её
    воркер убит перезапуском/пересборкой контейнера и никогда не
    допишет результат). Найденный баг, 2026-09-08: такие job'ы
    зависали в RUNNING на 9+ часов; сами по себе очередь не блокировали
    (`_claim_next_job` смотрит только на QUEUED), но вводили в
    заблуждение при диагностике ("что-то зависло?") и — что важнее —
    учитывались как "уже есть невыполненная задача" в новой проверке
    планировщика (``scheduler._outstanding_job_meter_ids``), навсегда
    блокируя повторную попытку для своего счётчика."""
    result = await db.execute(select(Job).where(Job.status == JobStatus.RUNNING))
    stale_jobs = result.scalars().all()
    now = datetime.now(timezone.utc)
    for job in stale_jobs:
        job.status = JobStatus.FAILED
        job.error = {
            "code": "WORKER_RESTARTED",
            "message": "Задача осталась в RUNNING после перезапуска Backend — воркер, державший её, уже не существует",
            "is_partial": False,
        }
        job.finished_at = now
    if stale_jobs:
        await db.commit()
    return len(stale_jobs)


async def requeue_stale_running_jobs(db: AsyncSession, *, older_than_s: float) -> int:
    """Периодический (не только при старте, в отличие от
    ``reap_stale_running_jobs`` выше) аналог для страховки событийного
    пути (см. ``claim_due_jobs_for_meter``, DECISIONS.md, план
    ticklish-popping-bear.md): если Gateway забрал job'ы через
    claim-jobs (перевёл их в RUNNING), но так и не смог отчитаться
    (упал посреди ассоциации, сеть до Backend отвалилась) — эти job'ы
    иначе провисели бы в RUNNING вечно, невидимые ни ``_claim_next_job``
    (смотрит только на QUEUED), ни следующему call-home подключению того
    же счётчика (claim-jobs тоже смотрит только на QUEUED).

    В ОТЛИЧИЕ от ``reap_stale_running_jobs`` — здесь НЕЛЬЗЯ считать
    каждую встреченную RUNNING-запись осиротевшей: обычный воркер вполне
    легитимно держит job в RUNNING до `call_timeout_s` (до 220с у
    чтения профиля нагрузки, самый долгий путь в проекте) — единственный
    различитель — возраст ``started_at``. Порог обязан быть заметно
    больше 220с (см. ``settings.stale_running_job_reap_after_s``,
    дефолт 300с), иначе можно вернуть в очередь job, который старый путь
    ЕЩЁ реально выполняет — это не сломает данные (job просто выполнится
    дважды), но приведёт к путанице/лишней нагрузке.

    Возвращает в QUEUED (не в FAILED, как при старте) — job ещё может
    быть честно дочитан следующей попыткой, данные не потеряны."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_s)
    result = await db.execute(
        select(Job).where(Job.status == JobStatus.RUNNING, Job.started_at < cutoff)
    )
    stale_jobs = result.scalars().all()
    for job in stale_jobs:
        job.status = JobStatus.QUEUED
        job.started_at = None
    if stale_jobs:
        await db.commit()
    return len(stale_jobs)


async def stale_job_reaper_loop(stop_event: asyncio.Event) -> None:
    logger.info(
        "Реаниматор зависших RUNNING-задач запущен (порог=%.0fс)",
        settings.stale_running_job_reap_after_s,
    )
    while not stop_event.is_set():
        async with SessionLocal() as db:
            try:
                requeued = await requeue_stale_running_jobs(
                    db, older_than_s=settings.stale_running_job_reap_after_s
                )
                if requeued:
                    logger.warning("Возвращено в очередь %d зависших RUNNING-задач", requeued)
            except Exception:
                logger.exception("Ошибка цикла реанимации зависших задач")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass
    logger.info("Реаниматор зависших RUNNING-задач остановлен")


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

    if job.scheduled_job_run_id is not None:
        await _maybe_finalize_scheduled_job_run(db, job.scheduled_job_run_id)
    if job.job_type in ("disconnect", "reconnect"):
        await _maybe_finalize_disconnect_batch_item(db, job)
    return True


async def worker_loop(stop_event: asyncio.Event, *, worker_id: int = 0) -> None:
    """``worker_id`` — только для логов: main.py запускает
    ``settings.job_worker_concurrency`` экземпляров этого цикла
    параллельно (2026-09-07, было ровно 1 — см. config.py), захват
    job'а (``_claim_next_job``, ``SELECT ... FOR UPDATE SKIP LOCKED``)
    уже был рассчитан на несколько одновременных воркеров, просто
    раньше не запускалось больше одного."""
    logger.info("Воркер задач #%d запущен (poll interval=%.1fs)", worker_id, settings.job_poll_interval_s)
    while not stop_event.is_set():
        async with SessionLocal() as db:
            processed = await _process_one(db)
        if not processed:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.job_poll_interval_s)
            except asyncio.TimeoutError:
                pass
    logger.info("Воркер задач #%d остановлен", worker_id)
