"""Ежедневный экспорт профиля нагрузки (Profile1) во внешнюю БД ЕЭБД
(2026-09-14, по прямому указанию пользователя) — журнал прогонов и
ручной запуск. Доступ ограничен ``Permission.MANAGE_SCHEDULED_JOBS``
(Admin/Super-admin) — тот же принцип, что и у "Расписания опроса"
(scheduled_jobs.py): это административная автоматизация, не рядовой
просмотр показаний.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import require_permission
from ..core.permissions import Permission
from ..db import SessionLocal, get_db
from ..models import EudbExportItem, EudbExportRun, Meter, User
from ..schemas import EudbExportItemOut, EudbExportRunOut
from ..services.eudb_export import create_export_run, eudb_export_configured, execute_export_run

router = APIRouter(prefix="/api/eudb-export", tags=["eudb-export"])


@router.get("/runs", response_model=list[EudbExportRunOut])
async def list_runs(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_SCHEDULED_JOBS)),
) -> list[EudbExportRun]:
    result = await db.execute(select(EudbExportRun).order_by(EudbExportRun.started_at.desc()).limit(90))
    return list(result.scalars().all())


@router.get("/runs/{run_id}/items", response_model=list[EudbExportItemOut])
async def list_run_items(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_SCHEDULED_JOBS)),
) -> list[EudbExportItemOut]:
    result = await db.execute(
        select(EudbExportItem, Meter.serial_number)
        .join(Meter, Meter.id == EudbExportItem.meter_id)
        .where(EudbExportItem.run_id == run_id)
        .order_by(Meter.serial_number)
    )
    return [
        EudbExportItemOut(
            id=item.id, meter_id=item.meter_id, meter_serial=serial, ok=item.ok,
            rows_exported=item.rows_exported, error_message=item.error_message, created_at=item.created_at,
        )
        for item, serial in result.all()
    ]


@router.get("/status")
async def export_status(
    user: User = Depends(require_permission(Permission.MANAGE_SCHEDULED_JOBS)),
) -> dict:
    return {"configured": eudb_export_configured()}


async def _execute_in_background(run_id: int) -> None:
    async with SessionLocal() as db:
        run = await db.get(EudbExportRun, run_id)
        if run is None:
            return
        await execute_export_run(db, run)


@router.post("/run-now", response_model=EudbExportRunOut, status_code=202)
async def trigger_run_now(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_SCHEDULED_JOBS)),
) -> EudbExportRun:
    """202 сразу — сам прогон (подключение к ЕЭБД + весь активный парк)
    может занять заметное время, не в рамках одного HTTP-запроса (тот
    же принцип, что и у ``POST /{meter_id}/read...`` — задача ставится,
    результат смотрят отдельно). Возвращает уже созданную запись
    ``EudbExportRun`` в статусе RUNNING — фронтенд поллит ``GET /runs``
    тем же способом, что и остальные списки в проекте."""
    run = await create_export_run(db, triggered_manually=True)
    background_tasks.add_task(_execute_in_background, run.id)
    return run
