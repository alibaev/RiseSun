"""ТЗ п.4.2.6/4.2.11 — REST API расписаний автоопроса."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.security import encrypt_secret, hash_password
from app.models import Gateway, GatewayStatus, Meter, ProtocolProfile, ScheduledJobRun, ScheduledJobRunStatus, User, UserRole


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


async def _seed_gateway_and_meter(db, root_id: int) -> int:
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root_id)
    db.add(gateway)
    await db.flush()
    meter = Meter(
        serial_number="202006003607", ip_address="127.0.0.1", port=4059,
        protocol_profile=ProtocolProfile.HDLC_DLMS, password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=gateway.id,
    )
    db.add(meter)
    await db.commit()
    await db.refresh(meter)
    return meter.id


@pytest.mark.asyncio
async def test_create_rejects_invalid_cron_expression(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    meter_id = await _seed_gateway_and_meter(db_session, root.id)
    token = await _login(client, "root", "pass1234")

    resp = await client.post(
        "/api/scheduled-jobs",
        json={"name": "x", "cron_expression": "not a cron", "job_type": "read_current", "meter_ids": [meter_id]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_rejects_unknown_meter(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    token = await _login(client, "root", "pass1234")

    resp = await client.post(
        "/api/scheduled-jobs",
        json={"name": "x", "cron_expression": "*/5 * * * *", "job_type": "read_current", "meter_ids": [9999]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_get_update_delete_roundtrip(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    meter_id = await _seed_gateway_and_meter(db_session, root.id)
    token = await _login(client, "root", "pass1234")

    create_resp = await client.post(
        "/api/scheduled-jobs",
        json={
            "name": "Ежедневный опрос",
            "cron_expression": "0 3 * * *",
            "job_type": "read_current",
            "operation_params": {"obis": "1.1.1.8.0.ff"},
            "meter_ids": [meter_id],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_resp.status_code == 201, create_resp.text
    body = create_resp.json()
    scheduled_job_id = body["id"]
    assert body["next_run_at"] is not None
    assert body["last_run_at"] is None

    get_resp = await client.get(f"/api/scheduled-jobs/{scheduled_job_id}", headers={"Authorization": f"Bearer {token}"})
    assert get_resp.status_code == 200

    list_resp = await client.get("/api/scheduled-jobs", headers={"Authorization": f"Bearer {token}"})
    assert len(list_resp.json()) == 1

    disable_resp = await client.put(
        f"/api/scheduled-jobs/{scheduled_job_id}",
        json={"is_enabled": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert disable_resp.status_code == 200
    assert disable_resp.json()["is_enabled"] is False
    assert disable_resp.json()["next_run_at"] is None  # выключенное расписание не сработает

    delete_resp = await client.delete(f"/api/scheduled-jobs/{scheduled_job_id}", headers={"Authorization": f"Bearer {token}"})
    assert delete_resp.status_code == 204


@pytest.mark.asyncio
async def test_engineer_cannot_manage_nor_view(client, db_session):
    """2026-09-12 (по прямому указанию пользователя — "доступ к
    расписанию только Суперадминистратор и Администратор") — до этой
    правки инженер мог хотя бы просматривать расписания (VIEW_METERS),
    теперь весь роутер требует MANAGE_SCHEDULED_JOBS."""
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    meter_id = await _seed_gateway_and_meter(db_session, root.id)
    await _seed_user(db_session, username="eng", password="pass1234", role=UserRole.ENGINEER)
    eng_token = await _login(client, "eng", "pass1234")

    create_resp = await client.post(
        "/api/scheduled-jobs",
        json={"name": "x", "cron_expression": "*/5 * * * *", "job_type": "read_current", "meter_ids": [meter_id]},
        headers={"Authorization": f"Bearer {eng_token}"},
    )
    assert create_resp.status_code == 403

    list_resp = await client.get("/api/scheduled-jobs", headers={"Authorization": f"Bearer {eng_token}"})
    assert list_resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_blocked_when_runs_exist(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    meter_id = await _seed_gateway_and_meter(db_session, root.id)
    token = await _login(client, "root", "pass1234")

    create_resp = await client.post(
        "/api/scheduled-jobs",
        json={"name": "x", "cron_expression": "*/5 * * * *", "job_type": "read_current", "meter_ids": [meter_id]},
        headers={"Authorization": f"Bearer {token}"},
    )
    scheduled_job_id = create_resp.json()["id"]

    db_session.add(ScheduledJobRun(scheduled_job_id=scheduled_job_id, status=ScheduledJobRunStatus.SUCCEEDED, meters_total=1))
    await db_session.commit()

    delete_resp = await client.delete(f"/api/scheduled-jobs/{scheduled_job_id}", headers={"Authorization": f"Bearer {token}"})
    assert delete_resp.status_code == 409

    disable_resp = await client.put(
        f"/api/scheduled-jobs/{scheduled_job_id}",
        json={"is_enabled": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert disable_resp.status_code == 200


@pytest.mark.asyncio
async def test_list_runs_and_run_jobs(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    meter_id = await _seed_gateway_and_meter(db_session, root.id)
    token = await _login(client, "root", "pass1234")

    create_resp = await client.post(
        "/api/scheduled-jobs",
        json={"name": "x", "cron_expression": "*/5 * * * *", "job_type": "read_current", "meter_ids": [meter_id]},
        headers={"Authorization": f"Bearer {token}"},
    )
    scheduled_job_id = create_resp.json()["id"]

    from app.models import Job

    run = ScheduledJobRun(scheduled_job_id=scheduled_job_id, status=ScheduledJobRunStatus.SUCCEEDED, meters_total=1, meters_succeeded=1)
    db_session.add(run)
    await db_session.flush()
    db_session.add(Job(job_type="read_current", meter_id=meter_id, payload={}, scheduled_job_run_id=run.id, status="succeeded"))
    await db_session.commit()

    runs_resp = await client.get(f"/api/scheduled-jobs/{scheduled_job_id}/runs", headers={"Authorization": f"Bearer {token}"})
    assert runs_resp.status_code == 200
    assert len(runs_resp.json()) == 1
    run_id = runs_resp.json()[0]["id"]

    jobs_resp = await client.get(
        f"/api/scheduled-jobs/{scheduled_job_id}/runs/{run_id}/jobs", headers={"Authorization": f"Bearer {token}"}
    )
    assert jobs_resp.status_code == 200
    assert len(jobs_resp.json()) == 1
    assert jobs_resp.json()[0]["meter_id"] == meter_id
