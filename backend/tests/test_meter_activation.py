"""ТЗ раздел 6 (обнаружение новых счётчиков) — POST /api/meters/{id}/activate."""

from __future__ import annotations

import pytest

from app.core.security import hash_password
from app.models import Gateway, GatewayStatus, Meter, MeterStatus, User, UserRole


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
