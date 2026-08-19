"""app/services/job_worker._maybe_finalize_scheduled_job_run — сводный
итог запуска расписания по завершении всех дочерних Job (ТЗ п.4.2.6/
4.2.11) и уведомление при ошибке (ТЗ п.4.2.8)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.core.security import encrypt_secret, hash_password
from app.models import (
    Gateway,
    GatewayStatus,
    Job,
    JobStatus,
    Meter,
    Notification,
    NotificationCategory,
    ProtocolProfile,
    ScheduledJob,
    ScheduledJobRun,
    ScheduledJobRunStatus,
    User,
    UserRole,
)
from app.services.job_worker import _maybe_finalize_scheduled_job_run


async def _seed(db) -> tuple[int, int]:
    """Возвращает (scheduled_job_id, run_id)."""
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
    scheduled_job = ScheduledJob(
        name="Опрос", cron_expression="*/5 * * * *", job_type="read_current",
        operation_params={}, meter_ids=[meter.id],
    )
    db.add(scheduled_job)
    await db.flush()
    run = ScheduledJobRun(scheduled_job_id=scheduled_job.id, status=ScheduledJobRunStatus.RUNNING, meters_total=2)
    db.add(run)
    await db.flush()
    await db.commit()
    return scheduled_job.id, run.id, meter.id


@pytest.mark.asyncio
async def test_finalize_waits_until_all_siblings_terminal(db_session):
    scheduled_job_id, run_id, meter_id = await _seed(db_session)
    db_session.add(Job(job_type="read_current", meter_id=meter_id, payload={}, scheduled_job_run_id=run_id, status=JobStatus.SUCCEEDED))
    db_session.add(Job(job_type="read_current", meter_id=meter_id, payload={}, scheduled_job_run_id=run_id, status=JobStatus.QUEUED))
    await db_session.commit()

    await _maybe_finalize_scheduled_job_run(db_session, run_id)

    run = await db_session.get(ScheduledJobRun, run_id)
    assert run.status == ScheduledJobRunStatus.RUNNING
    assert run.finished_at is None


@pytest.mark.asyncio
async def test_finalize_all_succeeded(db_session):
    scheduled_job_id, run_id, meter_id = await _seed(db_session)
    db_session.add(Job(job_type="read_current", meter_id=meter_id, payload={}, scheduled_job_run_id=run_id, status=JobStatus.SUCCEEDED))
    db_session.add(Job(job_type="read_current", meter_id=meter_id, payload={}, scheduled_job_run_id=run_id, status=JobStatus.SUCCEEDED))
    await db_session.commit()

    await _maybe_finalize_scheduled_job_run(db_session, run_id)

    run = await db_session.get(ScheduledJobRun, run_id)
    assert run.status == ScheduledJobRunStatus.SUCCEEDED
    assert run.meters_succeeded == 2
    assert run.meters_failed == 0
    assert run.finished_at is not None

    notifications = (await db_session.execute(select(Notification))).scalars().all()
    assert notifications == []


@pytest.mark.asyncio
async def test_finalize_partial_failure_creates_notification(db_session):
    scheduled_job_id, run_id, meter_id = await _seed(db_session)
    db_session.add(Job(job_type="read_current", meter_id=meter_id, payload={}, scheduled_job_run_id=run_id, status=JobStatus.SUCCEEDED))
    db_session.add(Job(job_type="read_current", meter_id=meter_id, payload={}, scheduled_job_run_id=run_id, status=JobStatus.FAILED))
    await db_session.commit()

    await _maybe_finalize_scheduled_job_run(db_session, run_id)

    run = await db_session.get(ScheduledJobRun, run_id)
    assert run.status == ScheduledJobRunStatus.PARTIAL_FAILURE
    assert run.meters_succeeded == 1
    assert run.meters_failed == 1

    notifications = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifications) == 1
    assert notifications[0].category == NotificationCategory.SCHEDULED_JOB_FAILED
    assert notifications[0].scheduled_job_id == scheduled_job_id
    assert notifications[0].details == {"run_id": run_id, "succeeded": 1, "failed": 1}


@pytest.mark.asyncio
async def test_finalize_all_failed(db_session):
    scheduled_job_id, run_id, meter_id = await _seed(db_session)
    db_session.add(Job(job_type="read_current", meter_id=meter_id, payload={}, scheduled_job_run_id=run_id, status=JobStatus.FAILED))
    db_session.add(Job(job_type="read_current", meter_id=meter_id, payload={}, scheduled_job_run_id=run_id, status=JobStatus.FAILED))
    await db_session.commit()

    await _maybe_finalize_scheduled_job_run(db_session, run_id)

    run = await db_session.get(ScheduledJobRun, run_id)
    assert run.status == ScheduledJobRunStatus.FAILED


@pytest.mark.asyncio
async def test_finalize_is_idempotent(db_session):
    scheduled_job_id, run_id, meter_id = await _seed(db_session)
    db_session.add(Job(job_type="read_current", meter_id=meter_id, payload={}, scheduled_job_run_id=run_id, status=JobStatus.FAILED))
    db_session.add(Job(job_type="read_current", meter_id=meter_id, payload={}, scheduled_job_run_id=run_id, status=JobStatus.FAILED))
    await db_session.commit()

    await _maybe_finalize_scheduled_job_run(db_session, run_id)
    await _maybe_finalize_scheduled_job_run(db_session, run_id)  # повторный вызов не должен дублировать уведомление

    notifications = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifications) == 1
