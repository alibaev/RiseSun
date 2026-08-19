"""app/services/offline_detector.py — уведомление при переходе счётчика
в offline по истечении настраиваемого таймаута (ТЗ п.4.2.8)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.core.security import encrypt_secret, hash_password
from app.models import Gateway, GatewayStatus, Meter, Notification, NotificationCategory, ProtocolProfile, User, UserRole
from app.services.offline_detector import _run_once


async def _seed_meter(db, *, last_seen_at) -> int:
    user = User(username="root", password_hash=hash_password("x"), role=UserRole.SUPER_ADMIN)
    db.add(user)
    await db.flush()
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=user.id)
    db.add(gateway)
    await db.flush()
    meter = Meter(
        serial_number="202006003607", ip_address="127.0.0.1", port=4059,
        protocol_profile=ProtocolProfile.HDLC_DLMS, password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=gateway.id, last_seen_at=last_seen_at,
    )
    db.add(meter)
    await db.commit()
    await db.refresh(meter)
    return meter.id


@pytest.mark.asyncio
async def test_no_notification_for_recently_seen_meter(db_session):
    await _seed_meter(db_session, last_seen_at=datetime.now(timezone.utc))
    await _run_once()

    notifications = (await db_session.execute(select(Notification))).scalars().all()
    assert notifications == []


@pytest.mark.asyncio
async def test_notification_created_when_timeout_exceeded(db_session):
    overdue = datetime.now(timezone.utc) - timedelta(seconds=settings.meter_offline_timeout_s + 60)
    meter_id = await _seed_meter(db_session, last_seen_at=overdue)

    await _run_once()

    notifications = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifications) == 1
    assert notifications[0].category == NotificationCategory.METER_OFFLINE
    assert notifications[0].meter_id == meter_id


@pytest.mark.asyncio
async def test_notification_not_duplicated_on_second_tick(db_session):
    overdue = datetime.now(timezone.utc) - timedelta(seconds=settings.meter_offline_timeout_s + 60)
    await _seed_meter(db_session, last_seen_at=overdue)

    await _run_once()
    await _run_once()

    notifications = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifications) == 1


@pytest.mark.asyncio
async def test_notification_recreated_after_meter_comes_back_and_goes_offline_again(db_session):
    """Счётчик уходит в offline (уведомление №1), затем СНОВА выходит на
    связь (``last_seen_at`` обновляется — реальный подтверждённый
    коннект), после чего снова считается offline: должно появиться
    новое, отдельное уведомление, а не тишина от guard'а «уже
    уведомляли». Таймаут временно занижен до 0, чтобы не ждать реальный
    ``meter_offline_timeout_s`` секунд — сам факт «last_seen_at раньше
    текущего момента» тогда уже считается overdue."""
    overdue = datetime.now(timezone.utc) - timedelta(seconds=settings.meter_offline_timeout_s + 60)
    meter_id = await _seed_meter(db_session, last_seen_at=overdue)
    await _run_once()

    meter = await db_session.get(Meter, meter_id)
    meter.last_seen_at = datetime.now(timezone.utc)  # счётчик подтверждённо вышел на связь заново
    await db_session.commit()

    with patch("app.services.offline_detector.settings.meter_offline_timeout_s", 0):
        await _run_once()

    notifications = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifications) == 2
