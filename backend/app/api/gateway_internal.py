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

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.gateway_internal_deps import require_gateway_internal_secret
from ..config import settings
from ..core.security import decrypt_secret
from ..db import get_db
from ..models import Job, JobStatus, Meter, MeterStatus
from ..schemas import (
    ClaimDueJobsRequest,
    ClaimDueJobsResponse,
    DueJobOut,
    ReportJobResultsRequest,
    ReportJobResultsResponse,
)
from ..services.gateway_client import ReadResult
from ..services.job_worker import (
    RATED_CURRENT_OBIS,
    _maybe_finalize_scheduled_job_run,
    claim_due_jobs_for_meter,
    finalize_read_current_job,
    finalize_read_rated_current_job,
)

router = APIRouter(prefix="/api/internal/gateway", tags=["gateway-internal"])


@router.post("/meters/{serial}/claim-jobs", response_model=ClaimDueJobsResponse)
async def claim_due_jobs(
    serial: str,
    body: ClaimDueJobsRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_gateway_internal_secret),
) -> ClaimDueJobsResponse:
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
    for job in claimed:
        if job.job_type == "read_current":
            jobs.append(DueJobOut(job_id=job.id, job_type=job.job_type, obis=job.payload["obis"], class_id=0))
        elif job.job_type == "read_rated_current":
            jobs.append(DueJobOut(job_id=job.id, job_type=job.job_type, obis=RATED_CURRENT_OBIS, class_id=0))

    password = decrypt_secret(meter.password_encrypted).decode("ascii")
    return ClaimDueJobsResponse(
        meter_found=True,
        meter_id=meter.id,
        protocol_profile=meter.protocol_profile.value,
        password=password,
        jobs=jobs,
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
