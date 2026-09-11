"""GET /api/meters/{id}/readings — фильтр по периоду (2026-09-11, UI:
вкладка "Показания" с выбором периода)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.security import encrypt_secret, hash_password
from app.models import Gateway, GatewayStatus, Meter, MeterReading, ProtocolProfile, User, UserRole


async def _seed_user(db, *, username: str, password: str, role: UserRole) -> User:
    user = User(username=username, password_hash=hash_password(password), role=role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client, username: str, password: str) -> str:
    resp = await client.post("/api/auth/login", data={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.mark.asyncio
async def test_readings_filtered_by_period(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()
    meter = Meter(
        serial_number="900000000777", ip_address="127.0.0.1", port=4059,
        protocol_profile=ProtocolProfile.HDLC_DLMS, password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=gateway.id,
    )
    db_session.add(meter)
    await db_session.flush()

    now = datetime.now(timezone.utc)
    old = MeterReading(meter_id=meter.id, obis_code="1.1.1.8.0.ff", value_json=1, read_at=now - timedelta(days=5))
    in_range = MeterReading(meter_id=meter.id, obis_code="1.1.1.8.0.ff", value_json=2, read_at=now - timedelta(hours=1))
    db_session.add_all([old, in_range])
    await db_session.commit()

    token = await _login(client, "root", "pass1234")
    resp = await client.get(
        f"/api/meters/{meter.id}/readings",
        params={"from_iso": (now - timedelta(days=1)).isoformat(), "to_iso": now.isoformat()},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["value_json"] == 2


@pytest.mark.asyncio
async def test_readings_without_period_returns_all_up_to_limit(client, db_session):
    root = await _seed_user(db_session, username="root2", password="pass1234", role=UserRole.SUPER_ADMIN)
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()
    meter = Meter(
        serial_number="900000000778", ip_address="127.0.0.1", port=4059,
        protocol_profile=ProtocolProfile.HDLC_DLMS, password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=gateway.id,
    )
    db_session.add(meter)
    await db_session.flush()

    now = datetime.now(timezone.utc)
    db_session.add_all(
        [
            MeterReading(meter_id=meter.id, obis_code="1.1.1.8.0.ff", value_json=1, read_at=now - timedelta(days=30)),
            MeterReading(meter_id=meter.id, obis_code="1.1.1.8.0.ff", value_json=2, read_at=now),
        ]
    )
    await db_session.commit()

    token = await _login(client, "root2", "pass1234")
    resp = await client.get(f"/api/meters/{meter.id}/readings", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 2
