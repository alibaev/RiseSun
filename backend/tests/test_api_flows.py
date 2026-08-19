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


@pytest.mark.asyncio
async def test_delete_meter_with_history_returns_409_not_500(client, db_session):
    # Регрессия (найдена при ручной проверке 2026-08-18): удаление
    # счётчика с показаниями роняло 500 через необработанный FK-конфликт
    # вместо понятной ошибки.
    from app.models import Job, JobStatus, Meter, MeterReading, ProtocolProfile
    from app.core.security import encrypt_secret

    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    db_session.add(
        Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    )
    await db_session.flush()
    meter = Meter(
        serial_number="202006003607",
        ip_address="127.0.0.1",
        port=4059,
        protocol_profile=ProtocolProfile.HDLC_DLMS,
        password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=1,
    )
    db_session.add(meter)
    await db_session.flush()
    db_session.add(MeterReading(meter_id=meter.id, obis_code="1.1.1.8.0.ff", value_json=123))
    db_session.add(Job(job_type="read_current", meter_id=meter.id, status=JobStatus.SUCCEEDED, payload={}))
    await db_session.commit()

    token = await _login(client, "root", "pass1234")
    resp = await client.delete(f"/api/meters/{meter.id}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 409
    assert "деактивируйте" in resp.text.lower()

    # Счётчик по-прежнему на месте — DELETE не оставил БД в промежуточном состоянии.
    get_resp = await client.get(f"/api/meters/{meter.id}", headers={"Authorization": f"Bearer {token}"})
    assert get_resp.status_code == 200


@pytest.mark.asyncio
async def test_call_home_meter_does_not_require_ip_and_port(client, db_session):
    # Звонящий домой счётчик (подтверждённое расхождение с ТЗ Table 1,
    # см. DECISIONS.md) — Gateway опознаёт его по serial_number через
    # свой call-home пул, ip_address/port ему не нужны.
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    db_session.add(
        Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    )
    await db_session.commit()
    token = await _login(client, "root", "pass1234")

    resp = await client.post(
        "/api/meters",
        json={
            "serial_number": "202306004113",
            "is_call_home": True,
            "protocol_profile": "hdlc_dlms",
            "password": "12345678",
            "gateway_id": 1,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["is_call_home"] is True
    assert body["ip_address"] is None
    assert body["port"] is None


@pytest.mark.asyncio
async def test_observer_cannot_trigger_write_datetime_but_engineer_can(client, db_session):
    """Этап 2 (ТЗ п.4.2.4): запись параметров — доступно ролям «Инженер»
    и «Администратор» (Permission.WRITE_PARAMETER), не «Наблюдателю»."""
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    await _seed_user(db_session, username="eng", password="pass1234", role=UserRole.ENGINEER)
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
    obs_resp = await client.post(
        "/api/meters/1/write-datetime", headers={"Authorization": f"Bearer {obs_token}"}
    )
    assert obs_resp.status_code == 403

    eng_token = await _login(client, "eng", "pass1234")
    eng_resp = await client.post(
        "/api/meters/1/write-datetime", headers={"Authorization": f"Bearer {eng_token}"}
    )
    assert eng_resp.status_code == 202, eng_resp.text
    body = eng_resp.json()
    assert body["job_type"] == "write_datetime"
    assert body["status"] == "queued"


@pytest.mark.asyncio
async def test_write_parameter_rejects_unknown_parameter_and_accepts_known(client, db_session):
    """Этап 2, итерация 2: /write-parameter/{parameter} — 400 для параметра
    вне реестра WRITABLE_INT_PARAMETERS, 202 для известного (settlement_no)."""
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
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

    unknown_resp = await client.post(
        "/api/meters/1/write-parameter/tariff_schedule",
        json={"value": 1},
        headers={"Authorization": f"Bearer {root_token}"},
    )
    assert unknown_resp.status_code == 400

    known_resp = await client.post(
        "/api/meters/1/write-parameter/settlement_no",
        json={"value": 3},
        headers={"Authorization": f"Bearer {root_token}"},
    )
    assert known_resp.status_code == 202, known_resp.text
    body = known_resp.json()
    assert body["job_type"] == "write_parameter"

    out_of_range_resp = await client.post(
        "/api/meters/1/write-parameter/settlement_no",
        json={"value": 256},
        headers={"Authorization": f"Bearer {root_token}"},
    )
    assert out_of_range_resp.status_code == 422


@pytest.mark.asyncio
async def test_write_parameter_accepts_all_registered_parameters(client, db_session):
    """Этап 2, итерация 3: параметры, найденные в Read_Tree_полное_дерево
    (режимы отображения, тарифное расписание, профиль нагрузки) — все
    принимаются эндпоинтом наравне с settlement_no/available_settlement_no."""
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
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

    for parameter in ("load_profile_interval", "display_mode_count", "weekend_rate_type"):
        resp = await client.post(
            f"/api/meters/1/write-parameter/{parameter}",
            json={"value": 5},
            headers={"Authorization": f"Bearer {root_token}"},
        )
        assert resp.status_code == 202, f"{parameter}: {resp.text}"


@pytest.mark.asyncio
async def test_read_load_profile_trigger_and_list(client, db_session):
    """Этап 3 (ТЗ п.4.2.3): POST .../read-load-profile ставит job в очередь
    (202, job_type='read_load_profile'), GET .../load-profile отдаёт уже
    сохранённые строки (пусто, пока воркер не выполнил job — этот тест не
    поднимает воркер, только проверяет форму API)."""
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
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

    trigger_resp = await client.post(
        "/api/meters/1/read-load-profile",
        json={"from_iso": "2026-08-01T00:00:00", "to_iso": "2026-08-19T00:00:00"},
        headers={"Authorization": f"Bearer {root_token}"},
    )
    assert trigger_resp.status_code == 202, trigger_resp.text
    assert trigger_resp.json()["job_type"] == "read_load_profile"

    list_resp = await client.get(
        "/api/meters/1/load-profile?from_iso=2026-08-01T00:00:00&to_iso=2026-08-19T00:00:00",
        headers={"Authorization": f"Bearer {root_token}"},
    )
    assert list_resp.status_code == 200
    assert list_resp.json() == []


@pytest.mark.asyncio
async def test_non_call_home_meter_requires_ip_and_port(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    db_session.add(
        Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    )
    await db_session.commit()
    token = await _login(client, "root", "pass1234")

    resp = await client.post(
        "/api/meters",
        json={
            "serial_number": "202306004113",
            "protocol_profile": "hdlc_dlms",
            "password": "12345678",
            "gateway_id": 1,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
