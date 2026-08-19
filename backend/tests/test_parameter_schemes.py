"""ТЗ п.4.2.5 — схемы параметров: CRUD + массовое применение к группе счётчиков."""

from __future__ import annotations

import pytest

from app.core.security import hash_password
from app.models import Gateway, GatewayStatus, User, UserRole


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


async def _seed_gateway_and_meters(db, root_id: int, n: int = 2) -> list[int]:
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root_id)
    db.add(gateway)
    await db.flush()
    from app.core.security import encrypt_secret
    from app.models import Meter, ProtocolProfile

    ids = []
    for i in range(n):
        meter = Meter(
            serial_number=f"20200600360{i}",
            ip_address="127.0.0.1",
            port=4059 + i,
            protocol_profile=ProtocolProfile.HDLC_DLMS,
            password_encrypted=encrypt_secret(b"12345678"),
            gateway_id=gateway.id,
        )
        db.add(meter)
        await db.flush()
        ids.append(meter.id)
    await db.commit()
    return ids


@pytest.mark.asyncio
async def test_create_scheme_rejects_unknown_parameter(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    token = await _login(client, "root", "pass1234")
    resp = await client.post(
        "/api/parameter-schemes",
        json={"name": "s1", "parameters": [{"parameter": "tariff_schedule", "value": 1}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_get_update_delete_scheme(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    token = await _login(client, "root", "pass1234")

    create_resp = await client.post(
        "/api/parameter-schemes",
        json={
            "name": "Стандартная",
            "description": "тест",
            "parameters": [{"parameter": "settlement_no", "value": 3}],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_resp.status_code == 201, create_resp.text
    scheme_id = create_resp.json()["id"]

    dup_resp = await client.post(
        "/api/parameter-schemes",
        json={"name": "Стандартная", "parameters": [{"parameter": "settlement_no", "value": 1}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert dup_resp.status_code == 409

    get_resp = await client.get(f"/api/parameter-schemes/{scheme_id}", headers={"Authorization": f"Bearer {token}"})
    assert get_resp.status_code == 200
    assert get_resp.json()["parameters"] == [{"parameter": "settlement_no", "value": 3}]

    list_resp = await client.get("/api/parameter-schemes", headers={"Authorization": f"Bearer {token}"})
    assert len(list_resp.json()) == 1

    update_resp = await client.put(
        f"/api/parameter-schemes/{scheme_id}",
        json={"parameters": [{"parameter": "settlement_no", "value": 7}, {"parameter": "available_settlement_no", "value": 9}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert update_resp.status_code == 200
    assert len(update_resp.json()["parameters"]) == 2

    delete_resp = await client.delete(f"/api/parameter-schemes/{scheme_id}", headers={"Authorization": f"Bearer {token}"})
    assert delete_resp.status_code == 204

    get_after_delete = await client.get(f"/api/parameter-schemes/{scheme_id}", headers={"Authorization": f"Bearer {token}"})
    assert get_after_delete.status_code == 404


@pytest.mark.asyncio
async def test_observer_can_view_but_not_create_scheme(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    await _seed_user(db_session, username="obs", password="pass1234", role=UserRole.OBSERVER)
    obs_token = await _login(client, "obs", "pass1234")

    list_resp = await client.get("/api/parameter-schemes", headers={"Authorization": f"Bearer {obs_token}"})
    assert list_resp.status_code == 200

    create_resp = await client.post(
        "/api/parameter-schemes",
        json={"name": "x", "parameters": [{"parameter": "settlement_no", "value": 1}]},
        headers={"Authorization": f"Bearer {obs_token}"},
    )
    assert create_resp.status_code == 403


@pytest.mark.asyncio
async def test_engineer_can_apply_but_not_manage_schemes(client, db_session):
    """ТЗ Приложение Б TABLE 0: «Инженер» — «применение схем параметров
    (в том числе массовое)», но создание/редактирование/удаление схем —
    в перечне прав только «Администратора»."""
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    meter_ids = await _seed_gateway_and_meters(db_session, root.id, n=1)
    await _seed_user(db_session, username="eng", password="pass1234", role=UserRole.ENGINEER)
    root_token = await _login(client, "root", "pass1234")
    eng_token = await _login(client, "eng", "pass1234")

    create_resp = await client.post(
        "/api/parameter-schemes",
        json={"name": "Инженерная", "parameters": [{"parameter": "settlement_no", "value": 2}]},
        headers={"Authorization": f"Bearer {root_token}"},
    )
    scheme_id = create_resp.json()["id"]

    eng_create_resp = await client.post(
        "/api/parameter-schemes",
        json={"name": "y", "parameters": [{"parameter": "settlement_no", "value": 1}]},
        headers={"Authorization": f"Bearer {eng_token}"},
    )
    assert eng_create_resp.status_code == 403

    eng_delete_resp = await client.delete(
        f"/api/parameter-schemes/{scheme_id}", headers={"Authorization": f"Bearer {eng_token}"}
    )
    assert eng_delete_resp.status_code == 403

    eng_apply_resp = await client.post(
        f"/api/parameter-schemes/{scheme_id}/apply",
        json={"meter_ids": meter_ids},
        headers={"Authorization": f"Bearer {eng_token}"},
    )
    assert eng_apply_resp.status_code == 202, eng_apply_resp.text


@pytest.mark.asyncio
async def test_apply_scheme_creates_one_job_per_parameter_per_meter(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    meter_ids = await _seed_gateway_and_meters(db_session, root.id, n=2)
    token = await _login(client, "root", "pass1234")

    create_resp = await client.post(
        "/api/parameter-schemes",
        json={
            "name": "Массовая",
            "parameters": [
                {"parameter": "settlement_no", "value": 3},
                {"parameter": "available_settlement_no", "value": 5},
            ],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    scheme_id = create_resp.json()["id"]

    apply_resp = await client.post(
        f"/api/parameter-schemes/{scheme_id}/apply",
        json={"meter_ids": meter_ids},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert apply_resp.status_code == 202, apply_resp.text
    jobs = apply_resp.json()
    assert len(jobs) == 4  # 2 параметра × 2 счётчика
    assert all(j["job_type"] == "write_parameter" for j in jobs)
    assert {j["meter_id"] for j in jobs} == set(meter_ids)


@pytest.mark.asyncio
async def test_apply_scheme_rejects_unknown_meter_id(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    token = await _login(client, "root", "pass1234")
    create_resp = await client.post(
        "/api/parameter-schemes",
        json={"name": "x", "parameters": [{"parameter": "settlement_no", "value": 1}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    scheme_id = create_resp.json()["id"]

    apply_resp = await client.post(
        f"/api/parameter-schemes/{scheme_id}/apply",
        json={"meter_ids": [9999]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert apply_resp.status_code == 400
