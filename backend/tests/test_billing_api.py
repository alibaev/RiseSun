"""ТЗ п.4.2.9/4.2.10, реализовано строго по техрегламенту API.docx —
сегмент /api/v1/billing/*: аутентификация, rate limiting, единый формат
ошибок, показания/тарифы, пакетное отключение/подключение."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.auth.billing_deps import _REQUEST_LOG
from app.core.security import encrypt_secret, hash_password
from app.models import (
    DisconnectBatch,
    Gateway,
    GatewayStatus,
    Job,
    LoadProfileData,
    Meter,
    MeterReading,
    ProtocolProfile,
    User,
    UserRole,
)


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    """``_REQUEST_LOG`` — состояние в памяти процесса, переживает сброс
    тестовой БД между тестами (см. app/auth/billing_deps.py); без сброса
    здесь id API-ключа, переиспользуемый из-за AUTOINCREMENT после
    пересоздания схемы, мог бы «наследовать» лимит от предыдущего теста."""
    _REQUEST_LOG.clear()
    yield
    _REQUEST_LOG.clear()


async def _seed_admin_and_login(client, db) -> str:
    user = User(username="root", password_hash=hash_password("pass1234"), role=UserRole.SUPER_ADMIN)
    db.add(user)
    await db.commit()
    resp = await client.post("/api/auth/login", data={"username": "root", "password": "pass1234"})
    return resp.json()["access_token"]


async def _create_billing_key(client, admin_token, **kwargs) -> str:
    body = {"client_id": kwargs.pop("client_id", "billing-test")}
    body.update(kwargs)
    resp = await client.post(
        "/api/billing-keys", json=body, headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"]


async def _seed_meter(db, gateway_id: int, serial: str = "202006003607") -> Meter:
    meter = Meter(
        serial_number=serial, ip_address="127.0.0.1", port=4059,
        protocol_profile=ProtocolProfile.HDLC_DLMS, password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=gateway_id,
    )
    db.add(meter)
    await db.flush()
    return meter


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.asyncio
async def test_missing_authorization_header_returns_unified_401(client, db_session):
    resp = await client.get("/api/v1/billing/readings?date_from=2026-08-01T00:00:00%2B00:00&date_to=2026-08-02T00:00:00%2B00:00")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert "request_id" in body["error"]
    assert resp.headers["X-Request-Id"] == body["error"]["request_id"]


@pytest.mark.asyncio
async def test_invalid_key_returns_401(client, db_session):
    resp = await client.get(
        "/api/v1/billing/readings?date_from=2026-08-01T00:00:00%2B00:00&date_to=2026-08-02T00:00:00%2B00:00",
        headers=_auth("not-a-real-key"),
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoked_key_returns_401(client, db_session):
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token)
    keys_resp = await client.get("/api/billing-keys", headers={"Authorization": f"Bearer {admin_token}"})
    key_id = keys_resp.json()[0]["id"]
    await client.post(f"/api/billing-keys/{key_id}/revoke", headers={"Authorization": f"Bearer {admin_token}"})

    resp = await client.get(
        "/api/v1/billing/readings?date_from=2026-08-01T00:00:00%2B00:00&date_to=2026-08-02T00:00:00%2B00:00",
        headers=_auth(api_key),
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_readings_invalid_date_range_returns_unified_400(client, db_session):
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token)

    resp = await client.get(
        "/api/v1/billing/readings?date_from=2026-08-05T00:00:00%2B00:00&date_to=2026-08-01T00:00:00%2B00:00",
        headers=_auth(api_key),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_DATE_RANGE"


@pytest.mark.asyncio
async def test_readings_range_exceeding_31_days_rejected(client, db_session):
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token)

    resp = await client.get(
        "/api/v1/billing/readings?date_from=2026-01-01T00:00:00%2B00:00&date_to=2026-03-01T00:00:00%2B00:00",
        headers=_auth(api_key),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_DATE_RANGE"


@pytest.mark.asyncio
async def test_readings_missing_required_param_returns_unified_format(client, db_session):
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token)

    resp = await client.get("/api/v1/billing/readings?date_from=2026-08-01T00:00:00%2B00:00", headers=_auth(api_key))
    assert resp.status_code == 400
    assert "error" in resp.json()


@pytest.mark.asyncio
async def test_readings_returns_stored_meter_readings(client, db_session):
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token)

    root = (await db_session.execute(select(User))).scalars().first()
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()
    meter = await _seed_meter(db_session, gateway.id)
    db_session.add(
        MeterReading(
            meter_id=meter.id, obis_code="1.0.1.8.0.ff", value_json=15234.7, unit="kWh",
            read_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        )
    )
    await db_session.commit()

    resp = await client.get(
        "/api/v1/billing/readings?date_from=2026-08-01T00:00:00%2B00:00&date_to=2026-08-10T00:00:00%2B00:00",
        headers=_auth(api_key),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["meter_serial"] == meter.serial_number
    assert body["items"][0]["value"] == 15234.7


@pytest.mark.asyncio
async def test_profile_returns_stored_rows(client, db_session):
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token)

    root = (await db_session.execute(select(User))).scalars().first()
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()
    meter = await _seed_meter(db_session, gateway.id)
    db_session.add(
        LoadProfileData(
            meter_id=meter.id, obis_code="1.1.63.1.0.ff",
            timestamp=datetime(2026, 8, 5, tzinfo=timezone.utc), values_json=[225.76, 0.1],
        )
    )
    await db_session.commit()

    resp = await client.get(
        "/api/v1/billing/profile?date_from=2026-08-01T00:00:00%2B00:00&date_to=2026-08-10T00:00:00%2B00:00",
        headers=_auth(api_key),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["meter_serial"] == meter.serial_number
    assert body["items"][0]["values"] == [225.76, 0.1]


@pytest.mark.asyncio
async def test_profile_returns_empty_items_when_no_data(client, db_session):
    """Нет данных за период — просто пустой список ("нет данных"), без
    подмены на ближайшие доступные записи."""
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token)

    resp = await client.get(
        "/api/v1/billing/profile?date_from=2026-08-01T00:00:00%2B00:00&date_to=2026-08-10T00:00:00%2B00:00",
        headers=_auth(api_key),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []


@pytest.mark.asyncio
async def test_profile_invalid_date_range_returns_unified_400(client, db_session):
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token)

    resp = await client.get(
        "/api/v1/billing/profile?date_from=2026-08-05T00:00:00%2B00:00&date_to=2026-08-01T00:00:00%2B00:00",
        headers=_auth(api_key),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_DATE_RANGE"


@pytest.mark.asyncio
async def test_tariffs_requires_meter_ids_or_region(client, db_session):
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token)

    resp = await client.get("/api/v1/billing/tariffs", headers=_auth(api_key))
    assert resp.status_code == 400

    ok_resp = await client.get("/api/v1/billing/tariffs?region=Moscow", headers=_auth(api_key))
    assert ok_resp.status_code == 200
    assert ok_resp.json()["items"] == []


@pytest.mark.asyncio
async def test_disconnect_batch_requires_idempotency_key(client, db_session):
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token)

    resp = await client.post(
        "/api/v1/billing/disconnect-batch", json={"meters": ["202006003607"]}, headers=_auth(api_key)
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


@pytest.mark.asyncio
async def test_disconnect_batch_too_large_rejected(client, db_session):
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token)

    resp = await client.post(
        "/api/v1/billing/disconnect-batch",
        json={"meters": [f"m{i}" for i in range(501)]},
        headers={**_auth(api_key), "Idempotency-Key": "k1"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BATCH_TOO_LARGE"


@pytest.mark.asyncio
async def test_disconnect_batch_unknown_meter_marked_failed_immediately(client, db_session):
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token)

    accept_resp = await client.post(
        "/api/v1/billing/disconnect-batch",
        json={"reason": "non_payment", "meters": ["unknown-serial"]},
        headers={**_auth(api_key), "Idempotency-Key": "k-unknown"},
    )
    assert accept_resp.status_code == 202, accept_resp.text
    batch_id = accept_resp.json()["batch_id"]
    assert accept_resp.json()["accepted_count"] == 1

    status_resp = await client.get(f"/api/v1/billing/disconnect-batch/{batch_id}", headers=_auth(api_key))
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] == "failed"  # единственный элемент — сразу failed
    assert body["items"][0]["status"] == "failed"
    assert body["items"][0]["error_code"] == "METER_NOT_FOUND"


@pytest.mark.asyncio
async def test_disconnect_batch_creates_job_for_known_meter(client, db_session):
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token)

    root = (await db_session.execute(select(User))).scalars().first()
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.flush()
    meter = await _seed_meter(db_session, gateway.id)
    await db_session.commit()

    accept_resp = await client.post(
        "/api/v1/billing/disconnect-batch",
        json={"meters": [meter.serial_number]},
        headers={**_auth(api_key), "Idempotency-Key": "k-known"},
    )
    assert accept_resp.status_code == 202
    batch_id = accept_resp.json()["batch_id"]

    status_resp = await client.get(f"/api/v1/billing/disconnect-batch/{batch_id}", headers=_auth(api_key))
    body = status_resp.json()
    assert body["status"] == "queued"
    assert body["items"][0]["status"] == "queued"

    jobs = (await db_session.execute(select(Job).where(Job.meter_id == meter.id))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].job_type == "disconnect"
    assert jobs[0].payload["source"] == "billing"


@pytest.mark.asyncio
async def test_disconnect_batch_idempotent_replay_same_body(client, db_session):
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token)

    first = await client.post(
        "/api/v1/billing/disconnect-batch",
        json={"meters": ["a", "b"]},
        headers={**_auth(api_key), "Idempotency-Key": "replay-1"},
    )
    second = await client.post(
        "/api/v1/billing/disconnect-batch",
        json={"meters": ["a", "b"]},
        headers={**_auth(api_key), "Idempotency-Key": "replay-1"},
    )
    assert first.json()["batch_id"] == second.json()["batch_id"]

    batches = (await db_session.execute(select(DisconnectBatch))).scalars().all()
    assert len(batches) == 1  # не создан второй пакет


@pytest.mark.asyncio
async def test_disconnect_batch_duplicate_idempotency_key_different_body_rejected(client, db_session):
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token)

    await client.post(
        "/api/v1/billing/disconnect-batch",
        json={"meters": ["a"]},
        headers={**_auth(api_key), "Idempotency-Key": "dup-key"},
    )
    second = await client.post(
        "/api/v1/billing/disconnect-batch",
        json={"meters": ["b"]},
        headers={**_auth(api_key), "Idempotency-Key": "dup-key"},
    )
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "DUPLICATE_IDEMPOTENCY_KEY"


@pytest.mark.asyncio
async def test_get_unknown_batch_returns_404(client, db_session):
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token)

    resp = await client.get("/api/v1/billing/disconnect-batch/does-not-exist", headers=_auth(api_key))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BATCH_NOT_FOUND"


@pytest.mark.asyncio
async def test_rate_limit_returns_429_with_retry_after(client, db_session):
    admin_token = await _seed_admin_and_login(client, db_session)
    api_key = await _create_billing_key(client, admin_token, rate_limit_per_minute=2)

    ok1 = await client.get("/api/v1/billing/tariffs?region=x", headers=_auth(api_key))
    ok2 = await client.get("/api/v1/billing/tariffs?region=x", headers=_auth(api_key))
    limited = await client.get("/api/v1/billing/tariffs?region=x", headers=_auth(api_key))

    assert ok1.status_code == 200
    assert ok2.status_code == 200
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert "Retry-After" in limited.headers
