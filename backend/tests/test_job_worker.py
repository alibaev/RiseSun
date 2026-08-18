"""Тесты app/services/job_worker.py — что call_home корректно
прокидывается в gRPC-вызов для звонящих домой счётчиков."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sqlalchemy import select

from app.core.security import encrypt_secret, hash_password
from app.models import (
    AuditLog,
    Gateway,
    GatewayStatus,
    Job,
    JobStatus,
    Meter,
    ParameterWriteHistory,
    ParameterWriteResult,
    ProtocolProfile,
    User,
    UserRole,
)
from app.services.gateway_client import ReadResult, WriteResult
from app.services.job_worker import _run_read_current, _run_write_datetime


async def _seed_gateway_and_user(db) -> Gateway:
    user = User(username="root", password_hash=hash_password("x"), role=UserRole.SUPER_ADMIN)
    db.add(user)
    await db.flush()
    gateway = Gateway(
        name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=user.id
    )
    db.add(gateway)
    await db.flush()
    return gateway


@pytest.mark.asyncio
async def test_call_home_meter_passes_call_home_flag_to_gateway(db_session):
    gateway = await _seed_gateway_and_user(db_session)
    meter = Meter(
        serial_number="202306004113",
        is_call_home=True,
        protocol_profile=ProtocolProfile.HDLC_DLMS,
        password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=gateway.id,
    )
    db_session.add(meter)
    await db_session.flush()
    job = Job(job_type="read_current", meter_id=meter.id, payload={"obis": "1.1.60.50.0.ff"})
    db_session.add(job)
    await db_session.commit()

    with patch(
        "app.services.job_worker.read_register",
        new=AsyncMock(return_value=ReadResult(ok=True, value=450701)),
    ) as mocked:
        await _run_read_current(db_session, job)

    mocked.assert_awaited_once()
    kwargs = mocked.call_args.kwargs
    assert kwargs["call_home"] is True
    assert kwargs["host"] == ""
    assert kwargs["port"] == 0
    assert kwargs["call_timeout_s"] == 160.0
    assert job.status == JobStatus.SUCCEEDED
    assert job.result == {"obis": "1.1.60.50.0.ff", "value": 450701}


@pytest.mark.asyncio
async def test_regular_meter_passes_host_port_and_call_home_false(db_session):
    gateway = await _seed_gateway_and_user(db_session)
    meter = Meter(
        serial_number="202006003607",
        ip_address="192.168.1.50",
        port=4059,
        is_call_home=False,
        protocol_profile=ProtocolProfile.HDLC_DLMS,
        password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=gateway.id,
    )
    db_session.add(meter)
    await db_session.flush()
    job = Job(job_type="read_current", meter_id=meter.id, payload={"obis": "1.1.1.8.0.ff"})
    db_session.add(job)
    await db_session.commit()

    with patch(
        "app.services.job_worker.read_register",
        new=AsyncMock(return_value=ReadResult(ok=True, value=1234567)),
    ) as mocked:
        await _run_read_current(db_session, job)

    kwargs = mocked.call_args.kwargs
    assert kwargs["call_home"] is False
    assert kwargs["host"] == "192.168.1.50"
    assert kwargs["port"] == 4059
    assert kwargs["call_timeout_s"] == 60.0


@pytest.mark.asyncio
async def test_write_datetime_records_history_and_audit_for_both_obis(db_session):
    """Этап 2 (ТЗ п.4.2.4): успешная запись даты и времени — два отдельных
    OBIS-объекта (Current Time/Current Date), каждый со своей строкой в
    parameter_write_history и записью в общий audit_log."""
    gateway = await _seed_gateway_and_user(db_session)
    meter = Meter(
        serial_number="202006003607",
        ip_address="192.168.1.50",
        port=4059,
        protocol_profile=ProtocolProfile.HDLC_DLMS,
        password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=gateway.id,
    )
    db_session.add(meter)
    await db_session.flush()
    root = (await db_session.execute(select(User))).scalars().first()
    job = Job(job_type="write_datetime", meter_id=meter.id, payload={}, created_by_id=root.id)
    db_session.add(job)
    await db_session.commit()

    with (
        patch(
            "app.services.job_worker.read_register",
            new=AsyncMock(return_value=ReadResult(ok=True, value="0a1e00")),
        ) as mocked_read,
        patch(
            "app.services.job_worker.write_register",
            new=AsyncMock(return_value=WriteResult(ok=True)),
        ) as mocked_write,
    ):
        await _run_write_datetime(db_session, job)

    assert mocked_read.await_count == 2
    assert mocked_write.await_count == 2
    write_obis = {c.kwargs["obis"] for c in mocked_write.call_args_list}
    assert write_obis == {"1.0.0.9.1.ff", "1.0.0.9.2.ff"}
    assert all(c.kwargs["class_id"] == 1 for c in mocked_write.call_args_list)

    assert job.status == JobStatus.SUCCEEDED

    history = (
        (await db_session.execute(select(ParameterWriteHistory).where(ParameterWriteHistory.meter_id == meter.id)))
        .scalars()
        .all()
    )
    assert len(history) == 2
    assert {h.parameter for h in history} == {"datetime.time", "datetime.date"}
    assert all(h.result == ParameterWriteResult.SUCCESS for h in history)
    assert all(h.old_value == "0a1e00" for h in history)
    assert all(h.new_value is not None for h in history)

    audit_rows = (
        (await db_session.execute(select(AuditLog).where(AuditLog.action == "meter.write_parameter")))
        .scalars()
        .all()
    )
    assert len(audit_rows) == 2


@pytest.mark.asyncio
async def test_write_datetime_partial_failure_marks_job_failed(db_session):
    gateway = await _seed_gateway_and_user(db_session)
    meter = Meter(
        serial_number="202006003607",
        ip_address="192.168.1.50",
        port=4059,
        protocol_profile=ProtocolProfile.HDLC_DLMS,
        password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=gateway.id,
    )
    db_session.add(meter)
    await db_session.flush()
    root = (await db_session.execute(select(User))).scalars().first()
    job = Job(job_type="write_datetime", meter_id=meter.id, payload={}, created_by_id=root.id)
    db_session.add(job)
    await db_session.commit()

    with (
        patch(
            "app.services.job_worker.read_register",
            new=AsyncMock(return_value=ReadResult(ok=False, error_code="TIMEOUT", error_message="нет ответа")),
        ),
        patch(
            "app.services.job_worker.write_register",
            new=AsyncMock(
                side_effect=[WriteResult(ok=True), WriteResult(ok=False, error_code="TIMEOUT", error_message="нет ответа")]
            ),
        ),
    ):
        await _run_write_datetime(db_session, job)

    assert job.status == JobStatus.FAILED
    history = (
        (await db_session.execute(select(ParameterWriteHistory).where(ParameterWriteHistory.meter_id == meter.id)))
        .scalars()
        .all()
    )
    assert len(history) == 2
    results = {h.parameter: h.result for h in history}
    assert results["datetime.time"] == ParameterWriteResult.SUCCESS
    assert results["datetime.date"] == ParameterWriteResult.FAILURE
    # old_value отсутствует — предварительное чтение неудачно, но это не
    # мешает всё равно попытаться записать и зафиксировать результат.
    assert all(h.old_value is None for h in history)
