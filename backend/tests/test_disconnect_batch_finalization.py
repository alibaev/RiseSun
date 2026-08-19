"""app/services/job_worker._maybe_finalize_disconnect_batch_item и
app/services/disconnect_batches.finalize_batch_if_complete (Этап 5, ТЗ
п.4.2.10, API.docx п.4.5)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.security import encrypt_secret, hash_password
from app.models import (
    BillingApiKey,
    DisconnectBatch,
    DisconnectBatchItem,
    DisconnectBatchItemStatus,
    DisconnectBatchOperation,
    DisconnectBatchStatus,
    Gateway,
    GatewayStatus,
    Job,
    JobStatus,
    Meter,
    ProtocolProfile,
    User,
    UserRole,
)
from app.services.disconnect_batches import finalize_batch_if_complete
from app.services.job_worker import _maybe_finalize_disconnect_batch_item


async def _seed(db) -> tuple[int, int]:
    """Возвращает (meter_id, batch_pk)."""
    user = User(username="root", password_hash=hash_password("x"), role=UserRole.SUPER_ADMIN)
    db.add(user)
    await db.flush()
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=user.id)
    db.add(gateway)
    await db.flush()
    meter = Meter(
        serial_number="202006003607", ip_address="127.0.0.1", port=4059,
        protocol_profile=ProtocolProfile.HDLC_DLMS, password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=gateway.id,
    )
    db.add(meter)
    await db.flush()
    api_key = BillingApiKey(client_id="test-client", key_hash="x")
    db.add(api_key)
    await db.flush()
    batch = DisconnectBatch(
        batch_id="b-test", operation=DisconnectBatchOperation.DISCONNECT, idempotency_key="idem-1",
        api_key_id=api_key.id, status=DisconnectBatchStatus.QUEUED,
    )
    db.add(batch)
    await db.flush()
    await db.commit()
    return meter.id, batch.id


@pytest.mark.asyncio
async def test_finalize_waits_for_all_items(db_session):
    meter_id, batch_pk = await _seed(db_session)
    db_session.add(DisconnectBatchItem(batch_id=batch_pk, meter_serial="a", meter_id=meter_id, status=DisconnectBatchItemStatus.DONE))
    db_session.add(DisconnectBatchItem(batch_id=batch_pk, meter_serial="b", meter_id=meter_id, status=DisconnectBatchItemStatus.QUEUED))
    await db_session.commit()

    await finalize_batch_if_complete(db_session, batch_pk)
    await db_session.commit()

    batch = await db_session.get(DisconnectBatch, batch_pk)
    assert batch.status == DisconnectBatchStatus.QUEUED
    assert batch.finished_at is None


@pytest.mark.asyncio
async def test_finalize_all_done_completes(db_session):
    meter_id, batch_pk = await _seed(db_session)
    db_session.add(DisconnectBatchItem(batch_id=batch_pk, meter_serial="a", meter_id=meter_id, status=DisconnectBatchItemStatus.DONE))
    db_session.add(DisconnectBatchItem(batch_id=batch_pk, meter_serial="b", meter_id=meter_id, status=DisconnectBatchItemStatus.DONE))
    await db_session.commit()

    await finalize_batch_if_complete(db_session, batch_pk)
    await db_session.commit()

    batch = await db_session.get(DisconnectBatch, batch_pk)
    assert batch.status == DisconnectBatchStatus.COMPLETED
    assert batch.finished_at is not None


@pytest.mark.asyncio
async def test_finalize_partial_failure(db_session):
    meter_id, batch_pk = await _seed(db_session)
    db_session.add(DisconnectBatchItem(batch_id=batch_pk, meter_serial="a", meter_id=meter_id, status=DisconnectBatchItemStatus.DONE))
    db_session.add(DisconnectBatchItem(batch_id=batch_pk, meter_serial="b", meter_id=meter_id, status=DisconnectBatchItemStatus.FAILED))
    await db_session.commit()

    await finalize_batch_if_complete(db_session, batch_pk)
    await db_session.commit()

    batch = await db_session.get(DisconnectBatch, batch_pk)
    assert batch.status == DisconnectBatchStatus.COMPLETED_WITH_ERRORS


@pytest.mark.asyncio
async def test_finalize_all_failed(db_session):
    meter_id, batch_pk = await _seed(db_session)
    db_session.add(DisconnectBatchItem(batch_id=batch_pk, meter_serial="a", meter_id=meter_id, status=DisconnectBatchItemStatus.FAILED))
    await db_session.commit()

    await finalize_batch_if_complete(db_session, batch_pk)
    await db_session.commit()

    batch = await db_session.get(DisconnectBatch, batch_pk)
    assert batch.status == DisconnectBatchStatus.FAILED


@pytest.mark.asyncio
async def test_job_worker_hook_updates_item_and_finalizes_batch(db_session):
    """Сквозная проверка: job.status меняется -> _maybe_finalize_
    disconnect_batch_item обновляет DisconnectBatchItem и финализирует
    DisconnectBatch, когда это была последняя незавершённая задача."""
    meter_id, batch_pk = await _seed(db_session)
    job = Job(job_type="disconnect", meter_id=meter_id, payload={"source": "billing"}, status=JobStatus.SUCCEEDED)
    db_session.add(job)
    await db_session.flush()
    db_session.add(
        DisconnectBatchItem(
            batch_id=batch_pk, meter_serial="a", meter_id=meter_id,
            status=DisconnectBatchItemStatus.QUEUED, job_id=job.id,
        )
    )
    await db_session.commit()

    await _maybe_finalize_disconnect_batch_item(db_session, job)

    item = (
        await db_session.execute(select(DisconnectBatchItem).where(DisconnectBatchItem.job_id == job.id))
    ).scalar_one()
    assert item.status == DisconnectBatchItemStatus.DONE

    batch = await db_session.get(DisconnectBatch, batch_pk)
    assert batch.status == DisconnectBatchStatus.COMPLETED


@pytest.mark.asyncio
async def test_job_worker_hook_is_noop_for_manual_job(db_session):
    meter_id, batch_pk = await _seed(db_session)
    job = Job(job_type="disconnect", meter_id=meter_id, payload={"source": "web"}, status=JobStatus.SUCCEEDED)
    db_session.add(job)
    await db_session.commit()

    await _maybe_finalize_disconnect_batch_item(db_session, job)  # не должно бросать и ничего не менять

    batch = await db_session.get(DisconnectBatch, batch_pk)
    assert batch.status == DisconnectBatchStatus.QUEUED
