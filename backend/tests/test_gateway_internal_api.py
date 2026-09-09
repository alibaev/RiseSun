"""Тесты app/api/gateway_internal.py — внутренний HTTP-канал
Gateway -> Backend для событийного чтения call-home счётчиков сразу
при подключении (см. DECISIONS.md и план ticklish-popping-bear.md)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.core.security import encrypt_secret, hash_password
from app.models import Gateway, GatewayStatus, Job, JobStatus, Meter, MeterReading, MeterStatus, ProtocolProfile, User, UserRole

_HEADERS = {"X-Internal-Secret": settings.gateway_internal_secret}


async def _seed_meter(db, *, serial: str = "202306004113", protocol_profile=ProtocolProfile.HDLC_DLMS) -> Meter:
    user = User(username="root", password_hash=hash_password("x"), role=UserRole.SUPER_ADMIN)
    db.add(user)
    await db.flush()
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=user.id)
    db.add(gateway)
    await db.flush()
    meter = Meter(
        serial_number=serial, is_call_home=True, status=MeterStatus.ACTIVE,
        protocol_profile=protocol_profile, password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=gateway.id,
    )
    db.add(meter)
    await db.commit()
    await db.refresh(meter)
    return meter


@pytest.fixture(autouse=True)
def _immediate_read_enabled():
    """Большинство тестов этого файла проверяют поведение ПРИ включённой
    фиче — дефолт (immediate_read_enabled=False) отдельно проверяется
    своим тестом. Восстанавливаем исходное состояние после теста, чтобы
    не протекать в другие тестовые модули (settings — синглтон процесса)."""
    original_enabled = settings.immediate_read_enabled
    original_allowed = settings.immediate_read_allowed_serials
    settings.immediate_read_enabled = True
    settings.immediate_read_allowed_serials = []
    yield
    settings.immediate_read_enabled = original_enabled
    settings.immediate_read_allowed_serials = original_allowed


@pytest.mark.asyncio
async def test_claim_jobs_rejects_missing_secret(client, db_session):
    resp = await client.post("/api/internal/gateway/meters/202306004113/claim-jobs", json={})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_claim_jobs_rejects_wrong_secret(client, db_session):
    resp = await client.post(
        "/api/internal/gateway/meters/202306004113/claim-jobs",
        json={}, headers={"X-Internal-Secret": "wrong"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_claim_jobs_disabled_globally_returns_not_found(client, db_session):
    meter = await _seed_meter(db_session)
    db_session.add(Job(job_type="read_current", meter_id=meter.id, payload={"obis": "1.1.1.8.0.ff"}))
    await db_session.commit()

    settings.immediate_read_enabled = False
    resp = await client.post(
        f"/api/internal/gateway/meters/{meter.serial_number}/claim-jobs", json={}, headers=_HEADERS
    )
    assert resp.status_code == 200
    assert resp.json() == {"meter_found": False, "meter_id": None, "protocol_profile": None, "password": None, "jobs": []}


@pytest.mark.asyncio
async def test_claim_jobs_respects_allowlist(client, db_session):
    meter = await _seed_meter(db_session)
    db_session.add(Job(job_type="read_current", meter_id=meter.id, payload={"obis": "1.1.1.8.0.ff"}))
    await db_session.commit()

    settings.immediate_read_allowed_serials = ["some_other_serial"]
    resp = await client.post(
        f"/api/internal/gateway/meters/{meter.serial_number}/claim-jobs", json={}, headers=_HEADERS
    )
    assert resp.json()["meter_found"] is False


@pytest.mark.asyncio
async def test_claim_jobs_happy_path_returns_password_and_jobs(client, db_session):
    meter = await _seed_meter(db_session)
    db_session.add(Job(job_type="read_current", meter_id=meter.id, payload={"obis": "1.1.1.8.0.ff"}))
    db_session.add(Job(job_type="read_current", meter_id=meter.id, payload={"obis": "1.1.32.7.0.ff"}))
    db_session.add(Job(job_type="read_rated_current", meter_id=meter.id, payload={}))
    await db_session.commit()

    resp = await client.post(
        f"/api/internal/gateway/meters/{meter.serial_number}/claim-jobs", json={}, headers=_HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["meter_found"] is True
    assert body["meter_id"] == meter.id
    assert body["protocol_profile"] == "hdlc_dlms"
    assert body["password"] == "12345678"
    assert {j["obis"] for j in body["jobs"]} == {"1.1.1.8.0.ff", "1.1.32.7.0.ff", "1.1.0.6.3.ff"}

    await db_session.refresh(meter)
    result = await db_session.execute(select(Job).where(Job.meter_id == meter.id))
    for job in result.scalars().all():
        assert job.status == JobStatus.RUNNING


@pytest.mark.asyncio
async def test_claim_jobs_does_not_double_claim_concurrent_calls(client, db_session):
    """Атомарность — второй вызов claim-jobs (имитация гонки двух
    почти одновременных call-home подключений того же счётчика) не
    должен вернуть уже захваченную первым вызовом job'у."""
    meter = await _seed_meter(db_session)
    db_session.add(Job(job_type="read_current", meter_id=meter.id, payload={"obis": "1.1.1.8.0.ff"}))
    await db_session.commit()

    resp1 = await client.post(
        f"/api/internal/gateway/meters/{meter.serial_number}/claim-jobs", json={}, headers=_HEADERS
    )
    resp2 = await client.post(
        f"/api/internal/gateway/meters/{meter.serial_number}/claim-jobs", json={}, headers=_HEADERS
    )
    assert len(resp1.json()["jobs"]) == 1
    assert len(resp2.json()["jobs"]) == 0


@pytest.mark.asyncio
async def test_claim_jobs_meter_not_active_returns_not_found(client, db_session):
    meter = await _seed_meter(db_session)
    meter.is_active = False
    await db_session.commit()
    db_session.add(Job(job_type="read_current", meter_id=meter.id, payload={"obis": "1.1.1.8.0.ff"}))
    await db_session.commit()

    resp = await client.post(
        f"/api/internal/gateway/meters/{meter.serial_number}/claim-jobs", json={}, headers=_HEADERS
    )
    assert resp.json()["meter_found"] is False


@pytest.mark.asyncio
async def test_job_results_happy_path_finalizes_job_and_reading(client, db_session):
    meter = await _seed_meter(db_session)
    job = Job(job_type="read_current", meter_id=meter.id, status=JobStatus.RUNNING, payload={"obis": "1.1.1.8.0.ff"})
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    resp = await client.post(
        "/api/internal/gateway/job-results",
        json={
            "serial": meter.serial_number,
            "results": [{"job_id": job.id, "obis": "1.1.1.8.0.ff", "ok": True, "value": 1234.5}],
        },
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "skipped": 0}

    await db_session.refresh(job)
    assert job.status == JobStatus.SUCCEEDED
    assert job.result == {"obis": "1.1.1.8.0.ff", "value": 1234.5}
    await db_session.refresh(meter)
    assert meter.last_seen_at is not None
    assert meter.last_read_at is not None

    readings = (await db_session.execute(
        select(MeterReading).where(MeterReading.meter_id == meter.id)
    )).scalars().all()
    assert len(readings) == 1
    assert readings[0].value_json == 1234.5


@pytest.mark.asyncio
async def test_job_results_skips_job_not_running(client, db_session):
    """Анти-задвоение: job уже не RUNNING (например, старый путь после
    реанимации stale_job_reaper_loop успел обработать её первым) —
    отчёт не должен повторно применяться."""
    meter = await _seed_meter(db_session)
    job = Job(
        job_type="read_current", meter_id=meter.id, status=JobStatus.SUCCEEDED,
        payload={"obis": "1.1.1.8.0.ff"}, result={"obis": "1.1.1.8.0.ff", "value": 999},
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    resp = await client.post(
        "/api/internal/gateway/job-results",
        json={
            "serial": meter.serial_number,
            "results": [{"job_id": job.id, "obis": "1.1.1.8.0.ff", "ok": True, "value": 1}],
        },
        headers=_HEADERS,
    )
    assert resp.json() == {"accepted": 0, "skipped": 1}

    await db_session.refresh(job)
    assert job.result == {"obis": "1.1.1.8.0.ff", "value": 999}  # не затёрто


@pytest.mark.asyncio
async def test_job_results_failure_marks_job_failed(client, db_session):
    meter = await _seed_meter(db_session)
    job = Job(job_type="read_current", meter_id=meter.id, status=JobStatus.RUNNING, payload={"obis": "1.1.1.8.0.ff"})
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    resp = await client.post(
        "/api/internal/gateway/job-results",
        json={
            "serial": meter.serial_number,
            "results": [
                {"job_id": job.id, "obis": "1.1.1.8.0.ff", "ok": False, "error_code": "TIMEOUT", "error_message": "нет ответа"}
            ],
        },
        headers=_HEADERS,
    )
    assert resp.json() == {"accepted": 1, "skipped": 0}
    await db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.error["code"] == "TIMEOUT"
