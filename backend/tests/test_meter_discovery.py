"""app/services/meter_discovery.py — обнаружение новых счётчиков по
call-home (Этап 6)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.core.security import decrypt_secret, hash_password
from app.models import Gateway, GatewayStatus, Meter, MeterStatus, ProtocolProfile, ScheduledJob, User, UserRole
from app.services.gateway_client import SeenSerial
from app.services.meter_discovery import _run_once


async def _seed_approved_gateway(db, grpc_target: str = "localhost:50051") -> Gateway:
    user = User(username="root", password_hash=hash_password("x"), role=UserRole.SUPER_ADMIN)
    db.add(user)
    await db.flush()
    gateway = Gateway(name="GW", grpc_target=grpc_target, status=GatewayStatus.APPROVED, registered_by_id=user.id)
    db.add(gateway)
    await db.commit()
    await db.refresh(gateway)
    return gateway


@pytest.mark.asyncio
async def test_new_serial_auto_activated_and_added_to_catchall_schedule(db_session):
    """2026-09-10 (см. DECISIONS.md) — по просьбе пользователя (ожидание
    ~300 новых счётчиков) ЛЮБОЙ новый call-home серийник заводится сразу
    ACTIVE (пароль/профиль — единый дефолт парка), а не INSTALLED, и
    добавляется в расписание с самым большим существующим списком
    счётчиков (эвристика "группа всех счётчиков", в отличие от узкой
    smoke-test группы). Раньше это применялось только к 6 конкретным
    ранее удалённым тестовым серийникам — теперь ко всем."""
    gateway = await _seed_approved_gateway(db_session)
    catchall = ScheduledJob(
        name="Ежедневный опрос всех счётчиков", cron_expression="*/30 * * * *",
        job_type="read_current", operation_params={}, meter_ids=[101, 102, 103],
    )
    smoke = ScheduledJob(
        name="Smoke test", cron_expression="* * * * *",
        job_type="read_current", operation_params={}, meter_ids=[999],
    )
    db_session.add_all([catchall, smoke])
    await db_session.commit()

    with patch(
        "app.services.meter_discovery.list_call_home_serials",
        new=AsyncMock(return_value=[SeenSerial(serial="900000000123", first_seen_unix=1755590400.0)]),
    ):
        await _run_once()

    meters = (await db_session.execute(select(Meter))).scalars().all()
    assert len(meters) == 1
    meter = meters[0]
    assert meter.serial_number == "900000000123"
    assert meter.is_call_home is True
    assert meter.gateway_id == gateway.id
    assert meter.status == MeterStatus.ACTIVE
    assert meter.protocol_profile == ProtocolProfile.HDLC_DLMS
    assert decrypt_secret(meter.password_encrypted) == b"12345678"
    # 2026-09-11 — токовый класс новой партии смешанный (100А, но
    # встречаются и 5/7.5А), больше не угадывается при автообнаружении,
    # устанавливается только реальным чтением read_rated_current.
    assert meter.rated_current_amps is None

    await db_session.refresh(catchall)
    await db_session.refresh(smoke)
    assert meter.id in catchall.meter_ids
    assert meter.id not in smoke.meter_ids  # узкая группа не тронута


@pytest.mark.asyncio
async def test_does_not_duplicate_already_known_serial(db_session):
    gateway = await _seed_approved_gateway(db_session)
    db_session.add(
        Meter(serial_number="900000000123", is_call_home=True, status=MeterStatus.ACTIVE, gateway_id=gateway.id)
    )
    await db_session.commit()

    with patch(
        "app.services.meter_discovery.list_call_home_serials",
        new=AsyncMock(return_value=[SeenSerial(serial="900000000123", first_seen_unix=1755590400.0)]),
    ):
        await _run_once()

    meters = (await db_session.execute(select(Meter))).scalars().all()
    assert len(meters) == 1  # не задублировано


@pytest.mark.asyncio
async def test_unreachable_gateway_does_not_raise(db_session):
    await _seed_approved_gateway(db_session)

    with patch(
        "app.services.meter_discovery.list_call_home_serials",
        new=AsyncMock(side_effect=ConnectionError("недоступен")),
    ):
        await _run_once()  # не должно бросать

    meters = (await db_session.execute(select(Meter))).scalars().all()
    assert meters == []


@pytest.mark.asyncio
async def test_pending_gateway_not_polled(db_session):
    user = User(username="root", password_hash=hash_password("x"), role=UserRole.SUPER_ADMIN)
    db_session.add(user)
    await db_session.flush()
    db_session.add(Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.PENDING, registered_by_id=user.id))
    await db_session.commit()

    with patch(
        "app.services.meter_discovery.list_call_home_serials", new=AsyncMock()
    ) as mocked:
        await _run_once()

    mocked.assert_not_awaited()
