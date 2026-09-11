"""Внутренний HTTP-канал Gateway -> Backend — событийное чтение
call-home счётчиков сразу при подключении (2026-09-09, см. DECISIONS.md
и план /root/.claude/plans/ticklish-popping-bear.md). Не часть
публичного API (ТЗ), вызывается только Gateway'ем изнутри docker-сети,
авторизация — общий секрет (``require_gateway_internal_secret``), а не
JWT-сессия пользователя или биллинговый API-ключ.

Момент, когда Gateway опознаёт звонящий счётчик на СВЕЖЕПРИНЯТОМ
соединении — единственный, когда есть реальный шанс успеть провести
SNRM/AARQ/GET до истечения "окна жизни" соединения (~30-60с, см.
DECISIONS.md 2026-08-18); FIFO-очередь старого пути (``job_worker.
worker_loop``) систематически проигрывает эту гонку при заметном
бэклоге. Этот роутер даёт Gateway'ю два действия: спросить, что сейчас
нужно прочитать у конкретного счётчика (атомарный claim, чтобы не
задвоить со старым путём), и отчитаться о результатах.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.gateway_internal_deps import require_gateway_internal_secret
from ..config import settings
from ..core.security import decrypt_secret
from ..db import SessionLocal, get_db
from ..models import Job, JobStatus, Meter, MeterStatus

logger = logging.getLogger("mmws_backend.gateway_internal")
from ..schemas import (
    ClaimDueJobsRequest,
    ClaimDueJobsResponse,
    DueJobOut,
    DueLoadProfileJobOut,
    ReportJobResultsRequest,
    ReportJobResultsResponse,
    ReportLoadProfileResultRequest,
    ReportLoadProfileResultResponse,
)
from ..services.gateway_client import ReadResult
from ..services.load_profile import DEFAULT_LOAD_PROFILE_OBIS
from ..services.job_worker import (
    RATED_CURRENT_OBIS,
    _insert_load_profile_row,
    _maybe_finalize_scheduled_job_run,
    claim_due_jobs_for_meter,
    finalize_read_current_job,
    finalize_read_load_profile_job,
    finalize_read_rated_current_job,
)
from ..services.res_mapping import res_name_for_port

router = APIRouter(prefix="/api/internal/gateway", tags=["gateway-internal"])


async def _record_connection_metadata(serial: str, peer_ip: str | None, local_port: int | None) -> None:
    """Фоновая задача (2026-09-11, по просьбе пользователя — "не должна
    мешать чтению, пусть работает отдельным потоком"): выполняется ПОСЛЕ
    того, как ответ claim-jobs уже отправлен Gateway'ю (FastAPI
    BackgroundTasks), в собственной сессии БД — не может задержать или
    заблокировать основной путь чтения ни на миллисекунду, независимо от
    того, сколько займёт эта запись. call-home-счётчики сами инициируют
    соединение, их ip_address иначе никогда и нигде не сохраняется —
    пишем при КАЖДОМ опознании (не только когда есть due job'ы), т.к. IP
    модема может плавать между сеансами связи. res_name — из
    local_port (см. services/res_mapping.py); пользователь физически
    разводит дозвон РЭС по портам, поэтому тоже обновляется при каждом
    опознании, а не только один раз."""
    try:
        async with SessionLocal() as db:
            result = await db.execute(select(Meter).where(Meter.serial_number == serial))
            meter = result.scalar_one_or_none()
            if meter is None:
                return
            changed = False
            if peer_ip and meter.ip_address != peer_ip:
                meter.ip_address = peer_ip
                changed = True
            res_name = res_name_for_port(local_port)
            if res_name is not None and meter.res_name != res_name:
                meter.res_name = res_name
                changed = True
            if changed:
                await db.commit()
    except Exception:  # noqa: BLE001 — сугубо вспомогательная запись, не должна ронять фон
        logger.exception(
            "Не удалось сохранить peer_ip=%s/local_port=%s для счётчика %s", peer_ip, local_port, serial
        )


@router.post("/meters/{serial}/claim-jobs", response_model=ClaimDueJobsResponse)
async def claim_due_jobs(
    serial: str,
    body: ClaimDueJobsRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_gateway_internal_secret),
) -> ClaimDueJobsResponse:
    if body.peer_ip or body.local_port is not None:
        background_tasks.add_task(_record_connection_metadata, serial, body.peer_ip, body.local_port)

    # Глобальный выключатель / поэтапный allowlist (см. config.py) —
    # "ничего не найдено", НЕ ошибка: Gateway трактует это точно так же,
    # как сетевой сбой — оставляет соединение в пуле для старого пути.
    if not settings.immediate_read_enabled:
        return ClaimDueJobsResponse(meter_found=False)
    if settings.immediate_read_allowed_serials and serial not in settings.immediate_read_allowed_serials:
        return ClaimDueJobsResponse(meter_found=False)

    result = await db.execute(select(Meter).where(Meter.serial_number == serial))
    meter = result.scalar_one_or_none()
    if (
        meter is None
        or not meter.is_call_home
        or not meter.is_active
        or meter.status != MeterStatus.ACTIVE
        or meter.protocol_profile is None
        or meter.password_encrypted is None
    ):
        return ClaimDueJobsResponse(meter_found=False)

    limit = body.max_jobs or settings.gateway_internal_claim_batch_max
    claimed = await claim_due_jobs_for_meter(db, meter_id=meter.id, job_types=body.job_types, limit=limit)

    jobs: list[DueJobOut] = []
    load_profile_jobs: list[DueLoadProfileJobOut] = []
    for job in claimed:
        if job.job_type == "read_current":
            # class_id — из payload (по умолчанию 0 = Register), а не
            # хардкод: диагностические чтения произвольных объектов
            # (напр. Data class_id=1 для OBIS ключа AES 0.0.60.32.77.ff,
            # см. DECISIONS.md 2026-09-10) иначе всегда читались бы как
            # Register и падали на несовпадении класса.
            jobs.append(
                DueJobOut(
                    job_id=job.id,
                    job_type=job.job_type,
                    obis=job.payload["obis"],
                    class_id=job.payload.get("class_id", 0),
                )
            )
        elif job.job_type == "read_rated_current":
            jobs.append(DueJobOut(job_id=job.id, job_type=job.job_type, obis=RATED_CURRENT_OBIS, class_id=0))
        elif job.job_type == "read_load_profile":
            # 2026-09-11 — перенос на событийный путь (см. DECISIONS.md).
            load_profile_jobs.append(
                DueLoadProfileJobOut(
                    job_id=job.id,
                    obis=job.payload.get("obis") or DEFAULT_LOAD_PROFILE_OBIS,
                    class_id=job.payload.get("class_id", 0),
                    from_iso=job.payload["from_iso"],
                    to_iso=job.payload["to_iso"],
                )
            )

    password = decrypt_secret(meter.password_encrypted).decode("ascii")
    return ClaimDueJobsResponse(
        meter_found=True,
        meter_id=meter.id,
        protocol_profile=meter.protocol_profile.value,
        password=password,
        jobs=jobs,
        load_profile_jobs=load_profile_jobs,
    )


@router.post("/job-results", response_model=ReportJobResultsResponse)
async def report_job_results(
    body: ReportJobResultsRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_gateway_internal_secret),
) -> ReportJobResultsResponse:
    result = await db.execute(select(Meter).where(Meter.serial_number == body.serial))
    meter = result.scalar_one_or_none()
    if meter is None:
        return ReportJobResultsResponse(accepted=0, skipped=len(body.results))

    accepted = 0
    skipped = 0
    for item in body.results:
        job = await db.get(Job, item.job_id)
        # job уже не RUNNING — старый путь (после реанимации
        # stale_job_reaper_loop) успел обработать её первым; принять
        # отчёт повторно означало бы задвоить MeterReading и, возможно,
        # затереть более свежий исход тем же самым старым результатом.
        if job is None or job.meter_id != meter.id or job.status != JobStatus.RUNNING:
            skipped += 1
            continue

        outcome = ReadResult(
            ok=item.ok,
            value=item.value,
            error_code=item.error_code,
            error_message=item.error_message,
            is_partial=item.is_partial,
        )
        if job.job_type == "read_current":
            await finalize_read_current_job(db, job, meter, outcome, obis=item.obis)
        elif job.job_type == "read_rated_current":
            await finalize_read_rated_current_job(db, job, meter, outcome)
        else:
            skipped += 1
            continue

        if job.scheduled_job_run_id is not None:
            await _maybe_finalize_scheduled_job_run(db, job.scheduled_job_run_id)
        accepted += 1

    return ReportJobResultsResponse(accepted=accepted, skipped=skipped)


@router.post("/load-profile-results", response_model=ReportLoadProfileResultResponse)
async def report_load_profile_results(
    body: ReportLoadProfileResultRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_gateway_internal_secret),
) -> ReportLoadProfileResultResponse:
    """2026-09-11 — перенос read_load_profile на событийный путь (см.
    DECISIONS.md). Gateway собирает все строки буфера в памяти (генератор
    ``read_load_profile_via_established_link``, как и раньше) и
    отчитывается ОДНИМ запросом — обрыв связи посреди передачи не теряет
    уже собранные строки (``ok=False, is_partial=True, rows`` непустой)."""
    result = await db.execute(select(Meter).where(Meter.serial_number == body.serial))
    meter = result.scalar_one_or_none()
    if meter is None:
        return ReportLoadProfileResultResponse(accepted=False, rows_written=0)

    job = await db.get(Job, body.job_id)
    # job уже не RUNNING — старый путь (после реанимации stale_job_
    # reaper_loop) успел обработать её первым — тот же принцип
    # анти-задвоения, что и в report_job_results.
    if job is None or job.meter_id != meter.id or job.status != JobStatus.RUNNING:
        return ReportLoadProfileResultResponse(accepted=False, rows_written=0)

    rows_written = 0
    for row in body.rows:
        await _insert_load_profile_row(
            db, meter_id=meter.id, obis=body.obis, job_id=job.id,
            timestamp_iso=row.timestamp_iso, values=row.values,
        )
        rows_written += 1

    error_info = None
    if not body.ok:
        error_info = {
            "code": body.error_code, "message": body.error_message,
            "is_partial": body.is_partial or rows_written > 0,
        }
    await finalize_read_load_profile_job(db, job, meter, obis=body.obis, rows_written=rows_written, error_info=error_info)

    if job.scheduled_job_run_id is not None:
        await _maybe_finalize_scheduled_job_run(db, job.scheduled_job_run_id)

    return ReportLoadProfileResultResponse(accepted=True, rows_written=rows_written)
