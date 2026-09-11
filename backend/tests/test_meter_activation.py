"""ТЗ раздел 6 (обнаружение новых счётчиков) — POST /api/meters/{id}/activate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.security import hash_password
from app.models import Gateway, GatewayStatus, Meter, MeterReading, MeterStatus, User, UserRole


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


async def _seed_installed_meter(db, gateway_id: int, serial: str = "900000000042") -> Meter:
    meter = Meter(serial_number=serial, is_call_home=True, status=MeterStatus.INSTALLED, gateway_id=gateway_id)
    db.add(meter)
    await db.commit()
    await db.refresh(meter)
    return meter


@pytest.mark.asyncio
async def test_activate_installed_meter(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()
    meter = await _seed_installed_meter(db_session, gateway.id)
    token = await _login(client, "root", "pass1234")

    resp = await client.post(
        f"/api/meters/{meter.id}/activate",
        json={"protocol_profile": "hdlc_dlms", "password": "12345678", "location": "ТП-5"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["protocol_profile"] == "hdlc_dlms"
    assert body["location"] == "ТП-5"

    await db_session.refresh(meter)
    assert meter.password_encrypted is not None


@pytest.mark.asyncio
async def test_activate_already_active_meter_conflicts(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()
    meter = Meter(
        serial_number="900000000043", protocol_profile="hdlc_dlms", is_call_home=False,
        ip_address="127.0.0.1", port=4059, status=MeterStatus.ACTIVE, gateway_id=gateway.id,
    )
    from app.core.security import encrypt_secret

    meter.password_encrypted = encrypt_secret(b"12345678")
    db_session.add(meter)
    await db_session.commit()
    await db_session.refresh(meter)
    token = await _login(client, "root", "pass1234")

    resp = await client.post(
        f"/api/meters/{meter.id}/activate",
        json={"protocol_profile": "hdlc_dlms", "password": "12345678"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_activate_requires_manage_meters_permission(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    await _seed_user(db_session, username="eng", password="pass1234", role=UserRole.ENGINEER)
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()
    meter = await _seed_installed_meter(db_session, gateway.id)
    eng_token = await _login(client, "eng", "pass1234")

    resp = await client.post(
        f"/api/meters/{meter.id}/activate",
        json={"protocol_profile": "hdlc_dlms", "password": "12345678"},
        headers={"Authorization": f"Bearer {eng_token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_activate_invalid_meter_restores_is_active(client, db_session):
    """Вкладка «Некорректные данные» (2026-09-08) — счётчик, переведённый в
    MeterStatus.INVALID вместе с is_active=False (повреждённый серийник),
    должен снова стать активным после исправления данных и повторной
    активации — иначе он останется невидим для планировщика навсегда."""
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()
    meter = Meter(
        serial_number="20d901230058", is_call_home=True, status=MeterStatus.INVALID,
        is_active=False, gateway_id=gateway.id,
    )
    db_session.add(meter)
    await db_session.commit()
    await db_session.refresh(meter)
    token = await _login(client, "root", "pass1234")

    fix_resp = await client.put(
        f"/api/meters/{meter.id}",
        json={"serial_number": "201901230058"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert fix_resp.status_code == 200, fix_resp.text
    assert fix_resp.json()["serial_number"] == "201901230058"

    resp = await client.post(
        f"/api/meters/{meter.id}/activate",
        json={"protocol_profile": "hdlc_dlms", "password": "12345678"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["is_active"] is True


@pytest.mark.asyncio
async def test_update_meter_duplicate_serial_number_conflicts(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()
    db_session.add(Meter(serial_number="900000000099", is_call_home=True, status=MeterStatus.ACTIVE, gateway_id=gateway.id))
    invalid_meter = Meter(serial_number="20d901230058", is_call_home=True, status=MeterStatus.INVALID, gateway_id=gateway.id)
    db_session.add(invalid_meter)
    await db_session.commit()
    await db_session.refresh(invalid_meter)
    token = await _login(client, "root", "pass1234")

    resp = await client.put(
        f"/api/meters/{invalid_meter.id}",
        json={"serial_number": "900000000099"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_update_meter_toggles_is_low_consumption(db_session, client):
    """2026-09-11 (по просьбе пользователя — категория "Малое
    потребление" между "Некорректные данные" и "Активные")."""
    root = await _seed_user(db_session, username="root3", password="pass1234", role=UserRole.SUPER_ADMIN)
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()
    meter = Meter(serial_number="202001002236", is_call_home=True, status=MeterStatus.ACTIVE, gateway_id=gateway.id)
    db_session.add(meter)
    await db_session.commit()
    await db_session.refresh(meter)
    assert meter.is_low_consumption is False
    token = await _login(client, "root3", "pass1234")

    resp = await client.put(
        f"/api/meters/{meter.id}",
        json={"is_low_consumption": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_low_consumption"] is True

    await db_session.refresh(meter)
    assert meter.is_low_consumption is True


async def test_apply_low_consumption_range_matches_meter_in_range(db_session, client):
    """2026-09-11 — "от и до скольки кВтч" в разделе "Малое потребление"."""
    root = await _seed_user(db_session, username="root4", password="pass1234", role=UserRole.SUPER_ADMIN)
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()

    in_range = Meter(serial_number="202001000001", is_call_home=True, status=MeterStatus.ACTIVE, gateway_id=gateway.id)
    out_of_range = Meter(serial_number="202001000002", is_call_home=True, status=MeterStatus.ACTIVE, gateway_id=gateway.id)
    no_reading = Meter(serial_number="202001000003", is_call_home=True, status=MeterStatus.ACTIVE, gateway_id=gateway.id)
    db_session.add_all([in_range, out_of_range, no_reading])
    await db_session.flush()
    db_session.add_all(
        [
            MeterReading(meter_id=in_range.id, obis_code="1.1.1.8.0.ff", value_json=0.03),
            MeterReading(meter_id=out_of_range.id, obis_code="1.1.1.8.0.ff", value_json=500.0),
        ]
    )
    await db_session.commit()
    token = await _login(client, "root4", "pass1234")

    resp = await client.post(
        "/api/meters/low-consumption/apply-range",
        json={"min_kwh": 0, "max_kwh": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    matched_ids = {m["id"] for m in resp.json()["matched_meters"]}
    assert matched_ids == {in_range.id}

    await db_session.refresh(in_range)
    await db_session.refresh(out_of_range)
    await db_session.refresh(no_reading)
    assert in_range.is_low_consumption is True
    assert out_of_range.is_low_consumption is False
    assert no_reading.is_low_consumption is False


async def test_apply_low_consumption_range_rejects_min_greater_than_max(db_session, client):
    root = await _seed_user(db_session, username="root5", password="pass1234", role=UserRole.SUPER_ADMIN)
    token = await _login(client, "root5", "pass1234")

    resp = await client.post(
        "/api/meters/low-consumption/apply-range",
        json={"min_kwh": 10, "max_kwh": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_meters_includes_status_field(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()
    await _seed_installed_meter(db_session, gateway.id)
    token = await _login(client, "root", "pass1234")

    resp = await client.get("/api/meters", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()[0]["status"] == "installed"


@pytest.mark.asyncio
async def test_res_stats_groups_meters_by_res_name(client, db_session):
    """2026-09-11 (по просьбе пользователя) — меню "РЭСы и Объекты"."""
    root = await _seed_user(db_session, username="root6", password="pass1234", role=UserRole.SUPER_ADMIN)
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()
    m1 = Meter(
        serial_number="202001000010", is_call_home=True, status=MeterStatus.ACTIVE, gateway_id=gateway.id,
        res_name="Кок-Арт РЭС",
    )
    m2 = Meter(
        serial_number="202001000011", is_call_home=True, status=MeterStatus.ACTIVE, gateway_id=gateway.id,
        res_name="Кок-Арт РЭС", is_active=False,
    )
    m3 = Meter(
        serial_number="202001000012", is_call_home=True, status=MeterStatus.ACTIVE, gateway_id=gateway.id,
        res_name="Сузак РЭС",
    )
    m4 = Meter(serial_number="202001000013", is_call_home=True, status=MeterStatus.ACTIVE, gateway_id=gateway.id)
    db_session.add_all([m1, m2, m3, m4])
    await db_session.commit()
    token = await _login(client, "root6", "pass1234")

    resp = await client.get("/api/meters/res-stats", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    by_name = {row["res_name"]: row for row in resp.json()}
    assert by_name["Кок-Арт РЭС"]["meters_total"] == 2
    assert by_name["Кок-Арт РЭС"]["meters_active"] == 1
    assert by_name["Сузак РЭС"]["meters_total"] == 1
    assert "None" not in by_name  # счётчик без res_name (m4) не попадает в статистику


@pytest.mark.asyncio
async def test_res_stats_computes_read_percentages(client, db_session):
    """2026-09-11 (по просьбе пользователя) — "Процент чтения сегодня"/
    "за 3 дня" в "РЭСы и Объекты", доля АКТИВНЫХ счётчиков с показанием
    за период (знаменатель не считает неактивные/без res_name)."""
    root = await _seed_user(db_session, username="root7", password="pass1234", role=UserRole.SUPER_ADMIN)
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    read_today = Meter(
        serial_number="202001000020", is_call_home=True, status=MeterStatus.ACTIVE, gateway_id=gateway.id,
        res_name="Аксы РЭС", last_read_at=now,
    )
    read_2days_ago = Meter(
        serial_number="202001000021", is_call_home=True, status=MeterStatus.ACTIVE, gateway_id=gateway.id,
        res_name="Аксы РЭС", last_read_at=now - timedelta(days=2),
    )
    never_read = Meter(
        serial_number="202001000022", is_call_home=True, status=MeterStatus.ACTIVE, gateway_id=gateway.id,
        res_name="Аксы РЭС",
    )
    db_session.add_all([read_today, read_2days_ago, never_read])
    await db_session.commit()
    token = await _login(client, "root7", "pass1234")

    resp = await client.get("/api/meters/res-stats", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    row = next(r for r in resp.json() if r["res_name"] == "Аксы РЭС")
    # 1 из 3 активных читан сегодня -> 33.3%; 2 из 3 читаны за 3 дня -> 66.7%
    assert row["pct_read_today"] == pytest.approx(33.3, abs=0.1)
    assert row["pct_read_3d"] == pytest.approx(66.7, abs=0.1)
