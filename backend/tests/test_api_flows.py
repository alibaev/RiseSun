"""Интеграционные тесты API поверх тестовой БД (mmws_test, см. conftest.py).

Не поднимает воркер задач/gRPC — проверяет синхронные эндпоинты
(auth, RBAC, регистрация gateway, справочник счётчиков).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models import Gateway, GatewayStatus, User, UserRole


async def _seed_user(db: AsyncSession, *, username: str, password: str, role: UserRole) -> User:
    user = User(username=username, password_hash=hash_password(password), role=role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client, username: str, password: str) -> str:
    resp = await client.post(
        "/api/auth/login", data={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.mark.asyncio
async def test_login_rejects_wrong_password(client, db_session):
    await _seed_user(db_session, username="alice", password="correcthorse", role=UserRole.OPERATOR)
    resp = await client.post("/api/auth/login", data={"username": "alice", "password": "wrong"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_success_returns_tokens(client, db_session):
    await _seed_user(db_session, username="alice", password="correcthorse", role=UserRole.OPERATOR)
    resp = await client.post("/api/auth/login", data={"username": "alice", "password": "correcthorse"})
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body and "refresh_token" in body


@pytest.mark.asyncio
async def test_refresh_token_issues_new_access_token(client, db_session):
    await _seed_user(db_session, username="alice", password="correcthorse", role=UserRole.OPERATOR)
    login_resp = await client.post("/api/auth/login", data={"username": "alice", "password": "correcthorse"})
    refresh_token = login_resp.json()["refresh_token"]

    resp = await client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200
    assert "access_token" in resp.json()


@pytest.mark.asyncio
async def test_gateway_registration_requires_super_admin(client, db_session):
    await _seed_user(db_session, username="admin1", password="pass1234", role=UserRole.ADMIN)
    token = await _login(client, "admin1", "pass1234")

    resp = await client.post(
        "/api/gateways",
        json={"name": "GW", "grpc_target": "localhost:50051"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # ТЗ п. 4.1.1: даже роль «Администратор» не может регистрировать Gateway.
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_gateway_registration_and_approval_by_super_admin(client, db_session):
    await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    token = await _login(client, "root", "pass1234")
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/gateways", json={"name": "GW", "grpc_target": "localhost:50051"}, headers=headers
    )
    assert create_resp.status_code == 201
    gw = create_resp.json()
    assert gw["status"] == "pending"

    approve_resp = await client.post(f"/api/gateways/{gw['id']}/approve", headers=headers)
    assert approve_resp.status_code == 200
    assert approve_resp.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_meter_creation_rejects_unapproved_gateway(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    db_session.add(
        Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.PENDING, registered_by_id=root.id)
    )
    await db_session.commit()
    token = await _login(client, "root", "pass1234")

    resp = await client.post(
        "/api/meters",
        json={
            "serial_number": "202006003607",
            "ip_address": "127.0.0.1",
            "port": 4059,
            "protocol_profile": "hdlc_dlms",
            "password": "12345678",
            "gateway_id": 1,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_meter_password_never_returned_in_api_response(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    db_session.add(
        Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    )
    await db_session.commit()
    token = await _login(client, "root", "pass1234")

    resp = await client.post(
        "/api/meters",
        json={
            "serial_number": "202006003607",
            "ip_address": "127.0.0.1",
            "port": 4059,
            "protocol_profile": "hdlc_dlms",
            "password": "12345678",
            "gateway_id": 1,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    assert "12345678" not in resp.text
    assert "password" not in resp.json()


@pytest.mark.asyncio
async def test_observer_can_list_meters_but_not_trigger_read(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    await _seed_user(db_session, username="obs", password="pass1234", role=UserRole.OBSERVER)
    db_session.add(
        Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    )
    await db_session.commit()

    root_token = await _login(client, "root", "pass1234")
    await client.post(
        "/api/meters",
        json={
            "serial_number": "202006003607",
            "ip_address": "127.0.0.1",
            "port": 4059,
            "protocol_profile": "hdlc_dlms",
            "password": "12345678",
            "gateway_id": 1,
        },
        headers={"Authorization": f"Bearer {root_token}"},
    )

    obs_token = await _login(client, "obs", "pass1234")
    obs_headers = {"Authorization": f"Bearer {obs_token}"}

    list_resp = await client.get("/api/meters", headers=obs_headers)
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    read_resp = await client.post(
        "/api/meters/1/read", json={"obis": "1.1.1.8.0.ff"}, headers=obs_headers
    )
    assert read_resp.status_code == 403


@pytest.mark.asyncio
async def test_unauthenticated_request_rejected(client):
    resp = await client.get("/api/meters")
    assert resp.status_code == 401
