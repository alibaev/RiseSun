"""Общие хелперы пакетных операций отключения/подключения (Этап 5, ТЗ
п.4.2.10, API.docx п.4.3-4.5) — используются и роутером
``app/api/billing.py`` (создание пакета), и ``job_worker.py`` (финализация
по мере завершения дочерних Job — тот же принцип, что и у
``_maybe_finalize_scheduled_job_run``, Этап 4)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DisconnectBatch, DisconnectBatchItem, DisconnectBatchItemStatus, DisconnectBatchStatus


def generate_batch_id() -> str:
    return f"b-{uuid.uuid4().hex[:16]}"


async def finalize_batch_if_complete(db: AsyncSession, batch_pk: int) -> None:
    items = (
        await db.execute(select(DisconnectBatchItem).where(DisconnectBatchItem.batch_id == batch_pk))
    ).scalars().all()
    if any(item.status == DisconnectBatchItemStatus.QUEUED for item in items):
        return

    batch = await db.get(DisconnectBatch, batch_pk)
    if batch is None or batch.status not in (DisconnectBatchStatus.QUEUED, DisconnectBatchStatus.IN_PROGRESS):
        return  # уже финализирован (см. комментарий про гонку в job_worker._maybe_finalize_scheduled_job_run)

    failed = sum(1 for item in items if item.status == DisconnectBatchItemStatus.FAILED)
    if failed == 0:
        batch.status = DisconnectBatchStatus.COMPLETED
    elif failed == len(items):
        batch.status = DisconnectBatchStatus.FAILED
    else:
        batch.status = DisconnectBatchStatus.COMPLETED_WITH_ERRORS
    batch.finished_at = datetime.now(timezone.utc)
