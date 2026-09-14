"""ТЗ п.4.2.6/4.2.11 — расписания автоматического опроса счётчиков:
CRUD, вычисляемое время следующего запуска, журнал выполнения запусков.

Доступ (2026-09-12, по прямому указанию пользователя — "доступ к
расписанию только Суперадминистратор и Администратор") — ВЕСЬ роутер,
включая чтение (список/один/журнал запусков), требует
``Permission.MANAGE_SCHEDULED_JOBS`` (Admin/Super-admin), а не
``VIEW_METERS`` (которым обладают все роли, включая Наблюдателя) — до
этой правки любая роль могла ПРОСМАТРИВАТЬ расписания, хоть и не могла
их менять."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import require_permission
from ..core.permissions import Permission
from ..db import get_db
from ..models import Job, Meter, ScheduledJob, ScheduledJobRun, User
from ..schemas import (
    JobOut,
    ScheduledJobCreate,
    ScheduledJobOut,
    ScheduledJobRunOut,
    ScheduledJobUpdate,
)
from ..services.scheduler import next_fire_time

router = APIRouter(prefix="/api/scheduled-jobs", tags=["scheduled-jobs"])


def _to_out(scheduled_job: ScheduledJob) -> ScheduledJobOut:
    out = ScheduledJobOut.model_validate(scheduled_job)
    base = scheduled_job.last_run_at or scheduled_job.created_at
    try:
        out.next_run_at = next_fire_time(scheduled_job.cron_expression, base) if scheduled_job.is_enabled else None
    except (ValueError, KeyError):
        out.next_run_at = None
    return out


async def _validate_meter_ids(db: AsyncSession, meter_ids: list[int]) -> None:
    found = (await db.execute(select(Meter.id).where(Meter.id.in_(meter_ids)))).scalars().all()
    missing = [mid for mid in meter_ids if mid not in set(found)]
    if missing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Счётчики не найдены: {missing}")


@router.get("", response_model=list[ScheduledJobOut])
async def list_scheduled_jobs(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_SCHEDULED_JOBS)),
) -> list[ScheduledJobOut]:
    result = await db.execute(select(ScheduledJob).order_by(ScheduledJob.name))
    return [_to_out(j) for j in result.scalars().all()]


@router.get("/{scheduled_job_id}", response_model=ScheduledJobOut)
async def get_scheduled_job(
    scheduled_job_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_SCHEDULED_JOBS)),
) -> ScheduledJobOut:
    scheduled_job = await db.get(ScheduledJob, scheduled_job_id)
    if scheduled_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Расписание не найдено")
    return _to_out(scheduled_job)


@router.post("", response_model=ScheduledJobOut, status_code=status.HTTP_201_CREATED)
async def create_scheduled_job(
    body: ScheduledJobCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_SCHEDULED_JOBS)),
) -> ScheduledJobOut:
    await _validate_meter_ids(db, body.meter_ids)
    scheduled_job = ScheduledJob(
        name=body.name,
        cron_expression=body.cron_expression,
        job_type=body.job_type,
        operation_params=body.operation_params,
        meter_ids=body.meter_ids,
        is_enabled=body.is_enabled,
        created_by_id=user.id,
    )
    db.add(scheduled_job)
    await db.commit()
    await db.refresh(scheduled_job)
    return _to_out(scheduled_job)


@router.put("/{scheduled_job_id}", response_model=ScheduledJobOut)
async def update_scheduled_job(
    scheduled_job_id: int,
    body: ScheduledJobUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_SCHEDULED_JOBS)),
) -> ScheduledJobOut:
    scheduled_job = await db.get(ScheduledJob, scheduled_job_id)
    if scheduled_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Расписание не найдено")

    if body.meter_ids is not None:
        await _validate_meter_ids(db, body.meter_ids)
        scheduled_job.meter_ids = body.meter_ids
    if body.name is not None:
        scheduled_job.name = body.name
    if body.cron_expression is not None:
        scheduled_job.cron_expression = body.cron_expression
    if body.operation_params is not None:
        scheduled_job.operation_params = body.operation_params
    if body.is_enabled is not None:
        scheduled_job.is_enabled = body.is_enabled

    await db.commit()
    await db.refresh(scheduled_job)
    return _to_out(scheduled_job)


@router.delete("/{scheduled_job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scheduled_job(
    scheduled_job_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_SCHEDULED_JOBS)),
) -> None:
    scheduled_job = await db.get(ScheduledJob, scheduled_job_id)
    if scheduled_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Расписание не найдено")
    await db.delete(scheduled_job)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить расписание — есть история запусков. Отключите его (is_enabled=false) вместо удаления.",
        )
    await db.commit()


@router.get("/{scheduled_job_id}/runs", response_model=list[ScheduledJobRunOut])
async def list_scheduled_job_runs(
    scheduled_job_id: int,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_SCHEDULED_JOBS)),
) -> list[ScheduledJobRun]:
    result = await db.execute(
        select(ScheduledJobRun)
        .where(ScheduledJobRun.scheduled_job_id == scheduled_job_id)
        .order_by(ScheduledJobRun.started_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


@router.get("/{scheduled_job_id}/runs/{run_id}/jobs", response_model=list[JobOut])
async def list_scheduled_job_run_jobs(
    scheduled_job_id: int,
    run_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_SCHEDULED_JOBS)),
) -> list[Job]:
    """Итог по каждому счётчику конкретного запуска (ТЗ п.4.2.11 —
    «журнал выполнения каждого запуска»)."""
    result = await db.execute(select(Job).where(Job.scheduled_job_run_id == run_id).order_by(Job.meter_id))
    return list(result.scalars().all())
