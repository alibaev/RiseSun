"""app/services/scheduler.py — вычисление срабатывания cron-расписаний
и создание Job/ScheduledJobRun (ТЗ п.4.2.6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.security import encrypt_secret, hash_password
from app.models import (
    Gateway,
    GatewayStatus,
    Job,
    JobStatus,
    Meter,
    MeterReading,
    PollProfile,
    ProtocolProfile,
    ScheduledJob,
    ScheduledJobRun,
    User,
    UserRole,
)
from app.services.scheduler import _is_due, _resolve_payloads, _run_once, _trigger_one, next_fire_time


async def _seed_gateway_and_meters(db, n: int = 2) -> list[int]:
    user = User(username="root", password_hash=hash_password("x"), role=UserRole.SUPER_ADMIN)
    db.add(user)
    await db.flush()
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=user.id)
    db.add(gateway)
    await db.flush()
    ids = []
    for i in range(n):
        meter = Meter(
            serial_number=f"20200600360{i}",
            ip_address="127.0.0.1",
            port=4059,
            protocol_profile=ProtocolProfile.HDLC_DLMS,
            password_encrypted=encrypt_secret(b"12345678"),
            gateway_id=gateway.id,
        )
        db.add(meter)
        await db.flush()
        ids.append(meter.id)
    await db.commit()
    return ids


def test_next_fire_time_every_minute():
    base = datetime(2026, 8, 19, 10, 0, 30, tzinfo=timezone.utc)
    fire = next_fire_time("* * * * *", base)
    assert fire == datetime(2026, 8, 19, 10, 1, tzinfo=timezone.utc)


def test_is_due_true_when_overdue():
    job = ScheduledJob(
        name="x", cron_expression="* * * * *", job_type="read_current", operation_params={}, meter_ids=[],
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    assert _is_due(job, datetime.now(timezone.utc)) is True


def test_is_due_false_when_last_run_recent():
    now = datetime.now(timezone.utc)
    job = ScheduledJob(
        name="x", cron_expression="0 0 * * *", job_type="read_current", operation_params={}, meter_ids=[],
        created_at=now - timedelta(days=1), last_run_at=now,
    )
    assert _is_due(job, now) is False


def test_is_due_false_for_invalid_cron_expression():
    job = ScheduledJob(
        name="x", cron_expression="not a cron", job_type="read_current", operation_params={}, meter_ids=[],
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    assert _is_due(job, datetime.now(timezone.utc)) is False


@pytest.mark.asyncio
async def test_resolve_payloads_read_current_default_obis(db_session):
    job = ScheduledJob(name="x", cron_expression="* * * * *", job_type="read_current", operation_params={}, meter_ids=[])
    assert await _resolve_payloads(db_session, job) == [{"obis": "1.1.1.8.0.ff"}]


@pytest.mark.asyncio
async def test_resolve_payloads_read_current_with_class_id_override():
    """2026-09-10 (см. DECISIONS.md, "постоянное чтение profile1"):
    class_id из operation_params должен пробрасываться в payload для
    простого GET объектов не-Register класса (напр. 7 = ProfileGeneric,
    буфер профиля нагрузки без диапазона дат)."""
    job = ScheduledJob(
        name="x", cron_expression="* * * * *", job_type="read_current",
        operation_params={"obis": "1.1.63.1.0.ff", "class_id": 7}, meter_ids=[],
    )
    assert await _resolve_payloads(None, job) == [{"obis": "1.1.63.1.0.ff", "class_id": 7}]


@pytest.mark.asyncio
async def test_resolve_payloads_read_load_profile_window(db_session):
    job = ScheduledJob(
        name="x", cron_expression="* * * * *", job_type="read_load_profile",
        operation_params={"window_hours": 6}, meter_ids=[],
    )
    payloads = await _resolve_payloads(db_session, job)
    assert len(payloads) == 1
    from_dt = datetime.fromisoformat(payloads[0]["from_iso"])
    to_dt = datetime.fromisoformat(payloads[0]["to_iso"])
    assert (to_dt - from_dt) == timedelta(hours=6)


@pytest.mark.asyncio
async def test_resolve_payloads_read_rated_current_is_empty(db_session):
    job = ScheduledJob(
        name="x", cron_expression="* * * * *", job_type="read_rated_current",
        operation_params={}, meter_ids=[],
    )
    assert await _resolve_payloads(db_session, job) == [{}]


@pytest.mark.asyncio
async def test_resolve_payloads_poll_profile_returns_enabled_obis_only(db_session):
    """2026-09-08, по просьбе пользователя — профиль опроса даёт по
    одному payload на каждый ВКЛЮЧЁННЫЙ пункт, выключенные пропускаются."""
    profile = PollProfile(
        name="Профиль1",
        items=[
            {"obis": "1.1.1.8.0.ff", "label": "Активная энергия", "enabled": True},
            {"obis": "1.1.32.7.0.ff", "label": "Напряжение фаза A", "enabled": True},
            {"obis": "1.1.52.7.0.ff", "label": "Напряжение фаза B", "enabled": False},
        ],
    )
    db_session.add(profile)
    await db_session.commit()

    job = ScheduledJob(
        name="x", cron_expression="* * * * *", job_type="read_current",
        operation_params={"poll_profile_id": profile.id}, meter_ids=[],
    )
    payloads = await _resolve_payloads(db_session, job)
    assert payloads == [{"obis": "1.1.1.8.0.ff"}, {"obis": "1.1.32.7.0.ff"}]


@pytest.mark.asyncio
async def test_resolve_payloads_missing_poll_profile_returns_empty(db_session):
    job = ScheduledJob(
        name="x", cron_expression="* * * * *", job_type="read_current",
        operation_params={"poll_profile_id": 999}, meter_ids=[],
    )
    assert await _resolve_payloads(db_session, job) == []


@pytest.mark.asyncio
async def test_trigger_one_creates_run_and_one_job_per_meter(db_session):
    meter_ids = await _seed_gateway_and_meters(db_session, n=3)
    scheduled_job = ScheduledJob(
        name="Опрос всех", cron_expression="*/5 * * * *", job_type="read_current",
        operation_params={"obis": "1.1.1.8.0.ff"}, meter_ids=meter_ids,
    )
    db_session.add(scheduled_job)
    await db_session.commit()

    await _trigger_one(db_session, scheduled_job)

    runs = (await db_session.execute(select(ScheduledJobRun))).scalars().all()
    assert len(runs) == 1
    assert runs[0].meters_total == 3

    jobs = (await db_session.execute(select(Job).where(Job.scheduled_job_run_id == runs[0].id))).scalars().all()
    assert len(jobs) == 3
    assert {j.meter_id for j in jobs} == set(meter_ids)
    assert all(j.status == JobStatus.QUEUED for j in jobs)
    assert all(j.payload == {"obis": "1.1.1.8.0.ff"} for j in jobs)
    assert scheduled_job.last_run_at is not None


@pytest.mark.asyncio
async def test_trigger_one_all_active_meters_includes_meter_not_in_meter_ids(db_session):
    """2026-09-11 (по просьбе пользователя — "сохрани для новых счётчиков
    на будущее"): при operation_params.all_active_meters=true список
    meter_ids игнорируется, берутся ВСЕ активные счётчики — включая те,
    что появились/активировались ПОСЛЕ создания расписания."""
    all_ids = await _seed_gateway_and_meters(db_session, n=3)
    meter_ids, later_meter_ids = all_ids[:2], all_ids[2:]
    scheduled_job = ScheduledJob(
        name="Весь парк", cron_expression="*/5 * * * *", job_type="read_current",
        operation_params={"obis": "1.1.1.8.0.ff", "all_active_meters": True},
        meter_ids=meter_ids,  # намеренно НЕ включает later_meter_ids — имитирует
        # счётчик, активированный/созданный уже ПОСЛЕ создания расписания.
    )
    db_session.add(scheduled_job)
    await db_session.commit()

    await _trigger_one(db_session, scheduled_job)

    jobs = (await db_session.execute(select(Job))).scalars().all()
    assert {j.meter_id for j in jobs} == set(meter_ids) | set(later_meter_ids)


@pytest.mark.asyncio
async def test_trigger_one_skips_deactivated_meters(db_session):
    """2026-09-08 — найдено при разборе вкладки «Некорректные данные»:
    _trigger_one раньше вообще не проверял is_active, поэтому
    деактивированный счётчик, всё ещё числящийся в scheduled_job.meter_ids,
    продолжал бы получать job'ы (и впустую тратить попытки воркеров),
    даже будучи выключенным из обслуживания."""
    meter_ids = await _seed_gateway_and_meters(db_session, n=3)
    deactivated = await db_session.get(Meter, meter_ids[0])
    deactivated.is_active = False
    await db_session.commit()

    scheduled_job = ScheduledJob(
        name="Опрос всех", cron_expression="*/5 * * * *", job_type="read_current",
        operation_params={"obis": "1.1.1.8.0.ff"}, meter_ids=meter_ids,
    )
    db_session.add(scheduled_job)
    await db_session.commit()

    await _trigger_one(db_session, scheduled_job)

    runs = (await db_session.execute(select(ScheduledJobRun))).scalars().all()
    assert runs[0].meters_total == 2

    jobs = (await db_session.execute(select(Job).where(Job.scheduled_job_run_id == runs[0].id))).scalars().all()
    assert {j.meter_id for j in jobs} == set(meter_ids[1:])


@pytest.mark.asyncio
async def test_trigger_one_poll_profile_creates_one_job_per_meter_per_obis(db_session):
    """2026-09-08, по просьбе пользователя — расписание с профилем
    опроса создаёт по отдельной Job на каждую пару счётчик×OBIS
    (включённый пункт профиля), но meters_total в ScheduledJobRun
    остаётся числом РАЗНЫХ счётчиков, не числом задач."""
    meter_ids = await _seed_gateway_and_meters(db_session, n=2)
    profile = PollProfile(
        name="Профиль1",
        items=[
            {"obis": "1.1.1.8.0.ff", "label": "Активная энергия", "enabled": True},
            {"obis": "1.1.32.7.0.ff", "label": "Напряжение фаза A", "enabled": True},
        ],
    )
    db_session.add(profile)
    await db_session.commit()

    scheduled_job = ScheduledJob(
        name="По профилю", cron_expression="*/5 * * * *", job_type="read_current",
        operation_params={"poll_profile_id": profile.id}, meter_ids=meter_ids,
    )
    db_session.add(scheduled_job)
    await db_session.commit()

    await _trigger_one(db_session, scheduled_job)

    runs = (await db_session.execute(select(ScheduledJobRun))).scalars().all()
    assert len(runs) == 1
    assert runs[0].meters_total == 2  # счётчиков, не задач

    jobs = (await db_session.execute(select(Job).where(Job.scheduled_job_run_id == runs[0].id))).scalars().all()
    assert len(jobs) == 4  # 2 счётчика × 2 OBIS
    obis_per_meter = {}
    for j in jobs:
        obis_per_meter.setdefault(j.meter_id, set()).add(j.payload["obis"])
    assert obis_per_meter == {
        meter_ids[0]: {"1.1.1.8.0.ff", "1.1.32.7.0.ff"},
        meter_ids[1]: {"1.1.1.8.0.ff", "1.1.32.7.0.ff"},
    }


@pytest.mark.asyncio
async def test_trigger_one_does_not_duplicate_outstanding_jobs(db_session):
    """Найденный баг (2026-09-08): частый cron (`*/30 * * * *`) на
    медленной call-home очереди раньше плодил дубликаты — счётчик,
    предыдущий Job которого воркер ещё не успел взять (QUEUED) или
    сейчас обрабатывает (RUNNING), не должен получить ещё одну Job на
    следующем срабатывании того же расписания."""
    meter_ids = await _seed_gateway_and_meters(db_session, n=3)
    queued_id, running_id, fresh_id = meter_ids
    db_session.add(Job(job_type="read_current", meter_id=queued_id, payload={}, status=JobStatus.QUEUED))
    db_session.add(Job(job_type="read_current", meter_id=running_id, payload={}, status=JobStatus.RUNNING))
    await db_session.commit()

    scheduled_job = ScheduledJob(
        name="Опрос всех", cron_expression="*/30 * * * *", job_type="read_current",
        operation_params={"obis": "1.1.1.8.0.ff"}, meter_ids=meter_ids,
    )
    db_session.add(scheduled_job)
    await db_session.commit()

    await _trigger_one(db_session, scheduled_job)

    new_jobs = (
        await db_session.execute(select(Job).where(Job.scheduled_job_run_id.is_not(None)))
    ).scalars().all()
    assert {j.meter_id for j in new_jobs} == {fresh_id}


@pytest.mark.asyncio
async def test_trigger_one_skip_if_read_today_excludes_already_read_meters(db_session):
    """skip_if_read_today (2026-09-07) — счётчик, у которого уже есть
    MeterReading по тому же OBIS за сегодня (Asia/Bishkek), не должен
    получить новую Job; счётчик без такого чтения — должен."""
    meter_ids = await _seed_gateway_and_meters(db_session, n=2)
    already_read_id, pending_id = meter_ids
    db_session.add(
        MeterReading(
            meter_id=already_read_id, obis_code="1.1.1.8.0.ff", value_json=123,
            read_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    scheduled_job = ScheduledJob(
        name="Ежедневный", cron_expression="*/30 * * * *", job_type="read_current",
        operation_params={"obis": "1.1.1.8.0.ff", "skip_if_read_today": True}, meter_ids=meter_ids,
    )
    db_session.add(scheduled_job)
    await db_session.commit()

    await _trigger_one(db_session, scheduled_job)

    jobs = (await db_session.execute(select(Job))).scalars().all()
    assert {j.meter_id for j in jobs} == {pending_id}


@pytest.mark.asyncio
async def test_trigger_one_skip_if_read_today_creates_no_run_when_all_covered(db_session):
    meter_ids = await _seed_gateway_and_meters(db_session, n=1)
    db_session.add(
        MeterReading(
            meter_id=meter_ids[0], obis_code="1.1.1.8.0.ff", value_json=123,
            read_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    scheduled_job = ScheduledJob(
        name="Ежедневный", cron_expression="*/30 * * * *", job_type="read_current",
        operation_params={"obis": "1.1.1.8.0.ff", "skip_if_read_today": True}, meter_ids=meter_ids,
    )
    db_session.add(scheduled_job)
    await db_session.commit()

    await _trigger_one(db_session, scheduled_job)

    assert (await db_session.execute(select(ScheduledJobRun))).scalars().all() == []
    assert (await db_session.execute(select(Job))).scalars().all() == []
    assert scheduled_job.last_run_at is not None  # иначе тик планировщика повторялся бы немедленно


@pytest.mark.asyncio
async def test_trigger_one_read_load_profile_skip_if_read_today_excludes_succeeded(db_session):
    """2026-09-10 ("постоянное чтение profile1", см. DECISIONS.md):
    skip_if_read_today для read_load_profile проверяет успешно
    завершённую Job (нет единой строки-показания с read_at, в отличие
    от read_current/MeterReading)."""
    meter_ids = await _seed_gateway_and_meters(db_session, n=2)
    already_read_id, pending_id = meter_ids
    db_session.add(
        Job(
            job_type="read_load_profile", meter_id=already_read_id, status=JobStatus.SUCCEEDED,
            payload={}, finished_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    scheduled_job = ScheduledJob(
        name="Профиль1 постоянно", cron_expression="*/5 * * * *", job_type="read_load_profile",
        operation_params={"skip_if_read_today": True, "window_hours": 6}, meter_ids=meter_ids,
    )
    db_session.add(scheduled_job)
    await db_session.commit()

    await _trigger_one(db_session, scheduled_job)

    jobs = (await db_session.execute(select(Job).where(Job.status == JobStatus.QUEUED))).scalars().all()
    assert {j.meter_id for j in jobs} == {pending_id}


@pytest.mark.asyncio
async def test_trigger_one_read_rated_current_skips_meters_with_known_value(db_session):
    """read_rated_current (2026-09-07) — в отличие от skip_if_read_today,
    пропуск НАВСЕГДА (не по суткам): счётчик с уже заполненным
    rated_current_amps исключается независимо от того, когда это
    значение было записано."""
    meter_ids = await _seed_gateway_and_meters(db_session, n=2)
    known_id, unknown_id = meter_ids
    known_meter = await db_session.get(Meter, known_id)
    known_meter.rated_current_amps = 100.0
    await db_session.commit()

    scheduled_job = ScheduledJob(
        name="Токовый класс", cron_expression="*/30 * * * *", job_type="read_rated_current",
        operation_params={}, meter_ids=meter_ids,
    )
    db_session.add(scheduled_job)
    await db_session.commit()

    await _trigger_one(db_session, scheduled_job)

    jobs = (await db_session.execute(select(Job))).scalars().all()
    assert {j.meter_id for j in jobs} == {unknown_id}
    assert all(j.payload == {} for j in jobs)


@pytest.mark.asyncio
async def test_run_once_skips_disabled_and_not_due(db_session):
    meter_ids = await _seed_gateway_and_meters(db_session, n=1)
    overdue = ScheduledJob(
        name="overdue", cron_expression="* * * * *", job_type="read_current",
        operation_params={}, meter_ids=meter_ids,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    disabled = ScheduledJob(
        name="disabled", cron_expression="* * * * *", job_type="read_current",
        operation_params={}, meter_ids=meter_ids, is_enabled=False,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    not_due = ScheduledJob(
        name="not_due", cron_expression="0 0 1 1 *", job_type="read_current",
        operation_params={}, meter_ids=meter_ids,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    db_session.add_all([overdue, disabled, not_due])
    await db_session.commit()

    await _run_once()

    runs = (await db_session.execute(select(ScheduledJobRun))).scalars().all()
    assert len(runs) == 1
    assert runs[0].scheduled_job_id == overdue.id
