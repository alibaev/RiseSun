"""Тесты app/api/gateway_internal.py — внутренний HTTP-канал
Gateway -> Backend для событийного чтения call-home счётчиков сразу
при подключении (см. DECISIONS.md и план ticklish-popping-bear.md)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.core.security import encrypt_secret, hash_password
from app.models import (
    Gateway,
    GatewayStatus,
    Job,
    JobStatus,
    LoadProfileData,
    Meter,
    MeterReading,
    MeterStatus,
    ProtocolProfile,
    ScheduledJob,
    ScheduledJobRun,
    ScheduledJobRunStatus,
    User,
    UserRole,
)

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
    assert resp.json() == {
        "meter_found": False, "meter_id": None, "protocol_profile": None, "password": None,
        "jobs": [], "load_profile_jobs": [], "control_jobs": [],
    }


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
async def test_claim_jobs_passes_through_payload_class_id(client, db_session):
    """2026-09-10: class_id из payload раньше не пробрасывался — все
    read_current уходили Gateway'ю как class_id=0 (Register), из-за чего
    диагностическое чтение объектов других классов (напр. Data class_id=1
    для OBIS 0.0.60.32.77.ff) через событийный путь было невозможно."""
    meter = await _seed_meter(db_session)
    db_session.add(
        Job(
            job_type="read_current",
            meter_id=meter.id,
            payload={"obis": "0.0.60.32.77.ff", "class_id": 1},
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/internal/gateway/meters/{meter.serial_number}/claim-jobs", json={}, headers=_HEADERS
    )
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["obis"] == "0.0.60.32.77.ff"
    assert jobs[0]["class_id"] == 1


@pytest.mark.asyncio
async def test_claim_jobs_records_peer_ip(client, db_session):
    """2026-09-11 (по просьбе пользователя): call-home-счётчики сами
    инициируют соединение, их ip_address иначе никогда не сохраняется —
    Gateway передаёт peer_ip при КАЖДОМ опознании, Backend пишет его в
    Meter.ip_address."""
    meter = await _seed_meter(db_session)
    assert meter.ip_address is None

    resp = await client.post(
        f"/api/internal/gateway/meters/{meter.serial_number}/claim-jobs",
        json={"peer_ip": "10.86.14.39"}, headers=_HEADERS,
    )
    assert resp.status_code == 200

    await db_session.refresh(meter)
    assert meter.ip_address == "10.86.14.39"


@pytest.mark.asyncio
async def test_claim_jobs_updates_peer_ip_even_without_due_jobs(client, db_session):
    """IP пишется независимо от того, есть ли due job'ы — счётчик может
    звонить часто без задач в очереди, IP всё равно интересен."""
    meter = await _seed_meter(db_session)
    # Никаких Job для этого счётчика не создано — meter_found всё равно
    # True (счётчик активен, is_call_home), просто jobs=[].

    resp = await client.post(
        f"/api/internal/gateway/meters/{meter.serial_number}/claim-jobs",
        json={"peer_ip": "10.86.14.40"}, headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["jobs"] == []

    await db_session.refresh(meter)
    assert meter.ip_address == "10.86.14.40"


@pytest.mark.asyncio
async def test_claim_jobs_records_res_name_from_local_port(client, db_session):
    """2026-09-11 (по просьбе пользователя) — счётчики физически
    разведены по РЭС через call-home порт (см. services/res_mapping.py);
    Gateway передаёт local_port, Backend пишет соответствующий res_name."""
    meter = await _seed_meter(db_session)
    assert meter.res_name is None

    resp = await client.post(
        f"/api/internal/gateway/meters/{meter.serial_number}/claim-jobs",
        json={"local_port": 2010}, headers=_HEADERS,
    )
    assert resp.status_code == 200

    await db_session.refresh(meter)
    assert meter.res_name == "Кок-Арт РЭС"


@pytest.mark.asyncio
async def test_claim_jobs_ignores_unknown_local_port(client, db_session):
    meter = await _seed_meter(db_session)

    resp = await client.post(
        f"/api/internal/gateway/meters/{meter.serial_number}/claim-jobs",
        json={"local_port": 9999}, headers=_HEADERS,
    )
    assert resp.status_code == 200

    await db_session.refresh(meter)
    assert meter.res_name is None


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


@pytest.mark.asyncio
async def test_claim_jobs_returns_load_profile_jobs(client, db_session):
    """2026-09-11 — перенос read_load_profile на событийный путь (см.
    DECISIONS.md)."""
    meter = await _seed_meter(db_session)
    db_session.add(
        Job(
            job_type="read_load_profile", meter_id=meter.id,
            payload={"from_iso": "2026-09-11T00:00:00", "to_iso": "2026-09-11T01:00:00"},
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/internal/gateway/meters/{meter.serial_number}/claim-jobs", json={}, headers=_HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["jobs"] == []
    assert len(body["load_profile_jobs"]) == 1
    lp_job = body["load_profile_jobs"][0]
    assert lp_job["obis"] == "1.1.63.1.0.ff"  # DEFAULT_LOAD_PROFILE_OBIS, payload его не задавал
    assert lp_job["from_iso"] == "2026-09-11T00:00:00"
    assert lp_job["to_iso"] == "2026-09-11T01:00:00"

    result = await db_session.execute(select(Job).where(Job.meter_id == meter.id))
    job = result.scalars().one()
    assert job.status == JobStatus.RUNNING


@pytest.mark.asyncio
async def test_claim_jobs_control_command_takes_priority_over_routine_read(client, db_session):
    """2026-09-12 (по просьбе пользователя — "все такие команды/запросы
    (отключение/подключение, запрос состояния реле) должны выполняться
    немедленно, не ждать очереди. если есть очередь, то только из этих
    команд") — disconnect/reconnect/read_relay_state обгоняют рядовое
    чтение и отдаются ОТДЕЛЬНО (обычные jobs пусты, пока есть control_jobs)."""
    meter = await _seed_meter(db_session)
    db_session.add(Job(job_type="read_current", meter_id=meter.id, payload={"obis": "1.1.1.8.0.ff"}))
    db_session.add(Job(job_type="disconnect", meter_id=meter.id, payload={"source": "web"}))
    await db_session.commit()

    resp = await client.post(
        f"/api/internal/gateway/meters/{meter.serial_number}/claim-jobs", json={}, headers=_HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["jobs"] == []
    assert body["load_profile_jobs"] == []
    assert len(body["control_jobs"]) == 1
    assert body["control_jobs"][0]["job_type"] == "disconnect"

    jobs = (await db_session.execute(select(Job).where(Job.meter_id == meter.id))).scalars().all()
    statuses = {j.job_type: j.status for j in jobs}
    assert statuses["disconnect"] == JobStatus.RUNNING
    assert statuses["read_current"] == JobStatus.QUEUED  # не тронут — ждёт следующего дозвона


@pytest.mark.asyncio
async def test_claim_jobs_returns_read_relay_state_as_ordinary_get(client, db_session):
    """read_relay_state — концептуально обычное чтение (GET), просто
    другой OBIS/class_id — batch'уется с read_current, если само по
    себе (без disconnect/reconnect в очереди одновременно)."""
    meter = await _seed_meter(db_session)
    db_session.add(Job(job_type="read_relay_state", meter_id=meter.id, payload={}))
    await db_session.commit()

    resp = await client.post(
        f"/api/internal/gateway/meters/{meter.serial_number}/claim-jobs", json={}, headers=_HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["obis"] == "0.0.60.a.1.ff"
    assert body["jobs"][0]["class_id"] == 1


@pytest.mark.asyncio
async def test_claim_jobs_operator_action_takes_priority_over_scheduled(client, db_session):
    """2026-09-12 (по просьбе пользователя — "после этих задач [команд
    управления], но впереди планового опроса, стоят действия оператора/
    пользователя ... непосредственный диалог пользователя через
    интерфейс") — job без scheduled_job_run_id (прямой запуск оператором)
    обгоняет job, порождённый расписанием, при ограниченном max_jobs."""
    meter = await _seed_meter(db_session)
    scheduled_job = ScheduledJob(
        name="Опрос", cron_expression="*/5 * * * *", job_type="read_current",
        operation_params={}, meter_ids=[meter.id],
    )
    db_session.add(scheduled_job)
    await db_session.flush()
    run = ScheduledJobRun(scheduled_job_id=scheduled_job.id, status=ScheduledJobRunStatus.RUNNING, meters_total=1)
    db_session.add(run)
    await db_session.flush()

    db_session.add(
        Job(
            job_type="read_current", meter_id=meter.id, payload={"obis": "1.1.1.8.0.ff"},
            scheduled_job_run_id=run.id,
        )
    )
    db_session.add(Job(job_type="read_current", meter_id=meter.id, payload={"obis": "1.1.32.7.0.ff"}))
    await db_session.commit()

    resp = await client.post(
        f"/api/internal/gateway/meters/{meter.serial_number}/claim-jobs",
        json={"max_jobs": 1}, headers=_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["obis"] == "1.1.32.7.0.ff"  # операторский, не плановый


@pytest.mark.asyncio
async def test_job_results_disconnect_success_finalizes_job_and_audit(client, db_session):
    meter = await _seed_meter(db_session)
    job = Job(job_type="disconnect", meter_id=meter.id, status=JobStatus.RUNNING, payload={"source": "web"})
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    resp = await client.post(
        "/api/internal/gateway/job-results",
        json={"serial": meter.serial_number, "results": [{"job_id": job.id, "ok": True}]},
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "skipped": 0}

    await db_session.refresh(job)
    assert job.status == JobStatus.SUCCEEDED
    assert job.result == {"operation": "disconnect", "ok": True}


@pytest.mark.asyncio
async def test_job_results_reconnect_failure_marks_job_failed(client, db_session):
    meter = await _seed_meter(db_session)
    job = Job(job_type="reconnect", meter_id=meter.id, status=JobStatus.RUNNING, payload={"source": "web"})
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    resp = await client.post(
        "/api/internal/gateway/job-results",
        json={
            "serial": meter.serial_number,
            "results": [{"job_id": job.id, "ok": False, "error_code": "GATEWAY_ERROR", "error_message": "боль"}],
        },
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "skipped": 0}

    await db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.error == {"code": "GATEWAY_ERROR", "message": "боль"}


@pytest.mark.asyncio
async def test_load_profile_results_inserts_rows_and_succeeds(client, db_session):
    meter = await _seed_meter(db_session)
    job = Job(job_type="read_load_profile", meter_id=meter.id, status=JobStatus.RUNNING, payload={})
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    resp = await client.post(
        "/api/internal/gateway/load-profile-results",
        json={
            "serial": meter.serial_number,
            "job_id": job.id,
            "obis": "1.1.63.1.0.ff",
            "ok": True,
            "rows": [
                {"timestamp_iso": "2026-09-11T00:15:00", "values": [100]},
                {"timestamp_iso": "2026-09-11T00:30:00", "values": [105]},
            ],
        },
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": True, "rows_written": 2}

    await db_session.refresh(job)
    assert job.status == JobStatus.SUCCEEDED
    rows = (await db_session.execute(select(LoadProfileData).where(LoadProfileData.meter_id == meter.id))).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_load_profile_results_partial_failure_keeps_collected_rows(client, db_session):
    """Обрыв связи посреди передачи датаблоков — уже собранные Gateway'ем
    строки не должны теряться, даже когда итоговый исход job'а FAILED."""
    meter = await _seed_meter(db_session)
    job = Job(job_type="read_load_profile", meter_id=meter.id, status=JobStatus.RUNNING, payload={})
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    resp = await client.post(
        "/api/internal/gateway/load-profile-results",
        json={
            "serial": meter.serial_number,
            "job_id": job.id,
            "obis": "1.1.63.1.0.ff",
            "ok": False,
            "error_code": "CONNECTION_LOST",
            "error_message": "обрыв соединения",
            "is_partial": True,
            "rows": [{"timestamp_iso": "2026-09-11T00:15:00", "values": [100]}],
        },
        headers=_HEADERS,
    )
    assert resp.json() == {"accepted": True, "rows_written": 1}

    await db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.error["code"] == "CONNECTION_LOST"
    assert job.error["is_partial"] is True
    rows = (await db_session.execute(select(LoadProfileData).where(LoadProfileData.meter_id == meter.id))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_load_profile_results_skips_job_not_running(client, db_session):
    meter = await _seed_meter(db_session)
    job = Job(job_type="read_load_profile", meter_id=meter.id, status=JobStatus.SUCCEEDED, payload={})
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    resp = await client.post(
        "/api/internal/gateway/load-profile-results",
        json={"serial": meter.serial_number, "job_id": job.id, "obis": "1.1.63.1.0.ff", "ok": True, "rows": []},
        headers=_HEADERS,
    )
    assert resp.json() == {"accepted": False, "rows_written": 0}
