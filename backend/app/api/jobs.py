"""ТЗ п. 4.2.12 — статус асинхронной задачи и потоковое обновление по WebSocket."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import InvalidCredentials, get_current_user, load_user_from_token
from ..db import SessionLocal, get_db
from ..models import Job, JobStatus, User
from ..schemas import JobOut

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

_POLL_INTERVAL_S = 1.0


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Job:
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Задача не найдена")
    return job


@router.websocket("/{job_id}/stream")
async def stream_job(websocket: WebSocket, job_id: int) -> None:
    """Токен передаётся query-параметром ``?token=`` — WebSocket не
    поддерживает заголовок Authorization при установлении соединения из
    браузера."""
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4401)
        return
    try:
        async with SessionLocal() as db:
            await load_user_from_token(token, db)
    except InvalidCredentials:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    try:
        last_status: JobStatus | None = None
        last_result: dict | None = None
        while True:
            async with SessionLocal() as db:
                job = await db.get(Job, job_id)
            if job is None:
                await websocket.send_json({"error": "job_not_found"})
                break
            # job.result меняется по ходу выполнения долгих задач (Этап 3 —
            # профиль нагрузки обновляет rows_written каждые N сохранённых
            # строк, см. job_worker._run_read_load_profile), не только при
            # смене статуса — иначе прогресс-бар на Frontend не обновлялся
            # бы до самого конца задачи.
            if job.status != last_status or job.result != last_result:
                await websocket.send_json(JobOut.model_validate(job).model_dump(mode="json"))
                last_status = job.status
                last_result = job.result
            if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
                break
            await asyncio.sleep(_POLL_INTERVAL_S)
    except WebSocketDisconnect:
        pass
