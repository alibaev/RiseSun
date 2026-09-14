"""ТЗ п.4.2.11 — Дашборд: сводная статистика."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.security import encrypt_secret, hash_password
from app.models import (
    Gateway,
    GatewayStatus,
    Job,
    JobStatus,
    LoadProfileData,
    Meter,
    MeterReading,
    ProtocolProfile,
    User,
    UserRole,
)


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
async def test_dashboard_counts_meters_jobs_readings(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()

    now = datetime.now(timezone.utc)
    online_meter = Meter(
        serial_number="online1", ip_address="127.0.0.1", port=4059,
        protocol_profile=ProtocolProfile.HDLC_DLMS, password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=gateway.id, last_seen_at=now,
    )
    offline_meter = Meter(
        serial_number="offline1", ip_address="127.0.0.1", port=4060,
        protocol_profile=ProtocolProfile.HDLC_DLMS, password_encrypted=encrypt_secret(b"12345678"),
        # >24ч — за пределами meter_offline_timeout_s (86400с, поднят с
        # 3600с 2026-09-08, см. DECISIONS.md) и не сегодняшний день.
        gateway_id=gateway.id, last_seen_at=now - timedelta(hours=30),
    )
    db_session.add_all([online_meter, offline_meter])
    await db_session.flush()

    db_session.add(Job(job_type="read_current", meter_id=online_meter.id, payload={}))
    db_session.add(
        MeterReading(meter_id=online_meter.id, obis_code="1.0.1.8.0.ff", value_json=1, unit="kWh", read_at=now)
    )
    await db_session.commit()

    token = await _login(client, "root", "pass1234")
    resp = await client.get("/api/dashboard", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["meters_total"] == 2
    assert body["meters_online"] == 1
    assert body["meters_offline"] == 1
    assert body["jobs_active"] == 1
    assert body["tamper_events_24h"] == 0
    assert len(body["readings_by_hour"]) == 1
    assert body["readings_by_hour"][0]["count"] == 1
    # 1 из 2 активных счётчиков имеет показание — 50%, и сегодня, и за 3 суток.
    assert body["read_percentage_today"] == 50.0
    assert body["read_percentage_3d"] == 50.0


@pytest.mark.asyncio
async def test_dashboard_read_percentage_excludes_old_readings(client, db_session):
    """read_percentage_today не должен учитывать показание за пределами
    текущих суток (Asia/Bishkek), даже если оно попадает в окно 3 суток."""
    root = await _seed_user(db_session, username="root2", password="pass1234", role=UserRole.SUPER_ADMIN)
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()

    now = datetime.now(timezone.utc)
    meter = Meter(
        serial_number="m-old-reading", ip_address="127.0.0.1", port=4059,
        protocol_profile=ProtocolProfile.HDLC_DLMS, password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=gateway.id, last_seen_at=now - timedelta(hours=30),
    )
    db_session.add(meter)
    await db_session.flush()
    # Вчера (гарантированно вне текущих суток Бишкека), но внутри 3 суток.
    db_session.add(
        MeterReading(
            meter_id=meter.id, obis_code="1.0.1.8.0.ff", value_json=1, unit="kWh",
            read_at=now - timedelta(hours=30),
        )
    )
    await db_session.commit()

    token = await _login(client, "root2", "pass1234")
    resp = await client.get("/api/dashboard", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["read_percentage_today"] == 0.0
    assert body["read_percentage_3d"] == 100.0


@pytest.mark.asyncio
async def test_dashboard_load_profile_stats(client, db_session):
    """load_profile_percentage_today считает по LoadProfileData.recorded_at
    (момент сохранения у нас), а не по timestamp строки (момент замера
    на счётчике) — тестовое чтение окна 00:00-02:00 сегодняшнего дня
    должно засчитаться как «опрос сегодня» независимо от того, какой
    исторический диапазон запрашивался."""
    root = await _seed_user(db_session, username="root3", password="pass1234", role=UserRole.SUPER_ADMIN)
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()

    now = datetime.now(timezone.utc)
    meter_ok = Meter(
        serial_number="lp-ok", ip_address="127.0.0.1", port=4059,
        protocol_profile=ProtocolProfile.HDLC_DLMS, password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=gateway.id, last_seen_at=now,
    )
    meter_pending = Meter(
        serial_number="lp-pending", ip_address="127.0.0.1", port=4060,
        protocol_profile=ProtocolProfile.HDLC_DLMS, password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=gateway.id, last_seen_at=now,
    )
    db_session.add_all([meter_ok, meter_pending])
    await db_session.flush()

    db_session.add(
        LoadProfileData(
            meter_id=meter_ok.id, obis_code="1-0:99.1.0.255",
            timestamp=now - timedelta(days=5), values_json=[1, 2, 3], recorded_at=now,
        )
    )
    db_session.add(Job(job_type="read_load_profile", meter_id=meter_pending.id, status=JobStatus.QUEUED, payload={}))
    db_session.add(
        Job(
            job_type="read_load_profile", meter_id=meter_pending.id, status=JobStatus.FAILED,
            payload={}, finished_at=now,
        )
    )
    await db_session.commit()

    token = await _login(client, "root3", "pass1234")
    resp = await client.get("/api/dashboard", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["load_profile_percentage_today"] == 50.0
    assert body["load_profile_jobs_active"] == 1
    assert body["load_profile_jobs_failed_24h"] == 1


@pytest.mark.asyncio
async def test_observer_can_view_dashboard(client, db_session):
    await _seed_user(db_session, username="obs", password="pass1234", role=UserRole.OBSERVER)
    token = await _login(client, "obs", "pass1234")
    resp = await client.get("/api/dashboard", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
