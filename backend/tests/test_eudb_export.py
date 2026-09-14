"""Ежедневный экспорт профиля нагрузки (Profile1) во внешнюю БД ЕЭБД
(2026-09-14, по прямому указанию пользователя). ЕЭБД — реальный внешний
сервер, здесь не поднимается — соединение подменяется фейком, SQL и
маппинг колонок проверяются напрямую."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.core.security import encrypt_secret, hash_password
from app.models import EudbExportRun, EudbExportRunStatus, Gateway, LoadProfileData, Meter, MeterStatus, ProtocolProfile, User, UserRole
from app.services.eudb_export import _VALUE_INDEX_TO_DESCRIPTION, create_export_run, run_export_once
from app.services.load_profile import DEFAULT_LOAD_PROFILE_OBIS


class _FakeEudbConn:
    def __init__(self, *, guid_by_serial: dict[str, str]):
        self._guid_by_serial = guid_by_serial
        self.inserted: list[tuple] = []
        self.closed = False

    async def fetchrow(self, sql: str, producer_guid: str, serial: str):
        guid = self._guid_by_serial.get(serial)
        return {"mtr_guid": guid} if guid else None

    async def execute(self, sql: str, *args):
        self.inserted.append(args)

    @asynccontextmanager
    async def transaction(self):
        yield

    async def close(self):
        self.closed = True


async def _seed_gateway(db_session) -> Gateway:
    gateway = Gateway(name="Test GW", grpc_target="localhost:50051")
    db_session.add(gateway)
    await db_session.commit()
    await db_session.refresh(gateway)
    return gateway


async def _seed_meter(db_session, gateway, *, serial: str) -> Meter:
    meter = Meter(
        serial_number=serial, status=MeterStatus.ACTIVE,
        protocol_profile=ProtocolProfile.HDLC_DLMS, password_encrypted=encrypt_secret(b"12345678"),
        gateway_id=gateway.id,
    )
    db_session.add(meter)
    await db_session.commit()
    await db_session.refresh(meter)
    return meter


async def _seed_profile_row(db_session, meter: Meter, *, values: list, ts: datetime | None = None):
    row = LoadProfileData(
        meter_id=meter.id, obis_code=DEFAULT_LOAD_PROFILE_OBIS,
        timestamp=ts or datetime(2026, 9, 13, 18, 30, tzinfo=timezone.utc),
        values_json=values, job_id=None,
    )
    db_session.add(row)
    await db_session.commit()


_FULL_ROW = [220.1, 221.2, 222.3, 1.1, 1.2, 1.3, 0.91, 0.92, 0.93, 1000.5, 999.9]


@pytest.mark.asyncio
async def test_execute_export_run_exports_all_expected_metrics(db_session):
    gateway = await _seed_gateway(db_session)
    meter = await _seed_meter(db_session, gateway, serial="202006003607")
    await _seed_profile_row(db_session, meter, values=_FULL_ROW)

    fake_conn = _FakeEudbConn(guid_by_serial={"202006003607": "11111111-1111-1111-1111-111111111111"})
    with patch("app.services.eudb_export._connect_eudb", new=AsyncMock(return_value=fake_conn)), \
         patch("app.services.eudb_export.eudb_export_configured", return_value=True):
        run = await run_export_once(db_session, triggered_manually=True)

    assert run.status == EudbExportRunStatus.SUCCEEDED
    assert run.meters_total == 1
    assert run.meters_succeeded == 1
    assert run.meters_failed == 0
    assert len(fake_conn.inserted) == len(_VALUE_INDEX_TO_DESCRIPTION) == 10
    descriptions = {call[3] for call in fake_conn.inserted}
    assert descriptions == set(_VALUE_INDEX_TO_DESCRIPTION.values())
    # значение активной энергии (индекс 9) действительно ушло с меткой
    # "Активная энергия (кВт*ч)", а не перепутано с другим полем
    energy_call = next(c for c in fake_conn.inserted if c[3] == "Активная энергия (кВт*ч)")
    assert energy_call[2] == pytest.approx(1000.5)
    assert fake_conn.closed is True
    # Реальный баг, найден 2026-09-14 на живом прогоне (~2200 из 2376
    # счётчиков): asyncpg отказывается биндить tz-aware datetime в
    # "timestamp without time zone" колонку ЕЭБД. Момент должен уйти
    # НАИВНЫМ (без tzinfo) — иначе фейковый конн в этом тесте не поймал
    # бы то, что реальный asyncpg ловит на уровне протокола.
    assert all(call[1].tzinfo is None for call in fake_conn.inserted)


@pytest.mark.asyncio
async def test_execute_export_run_converts_timestamp_to_bishkek_local(db_session):
    """UTC 18:30 -> Asia/Bishkek (UTC+6) 00:30 СЛЕДУЮЩИХ суток, наивный
    (без tzinfo) — именно то, что не приняла ЕЭБД (timestamp without
    time zone) при tz-aware значении."""
    gateway = await _seed_gateway(db_session)
    meter = await _seed_meter(db_session, gateway, serial="202006003607")
    await _seed_profile_row(db_session, meter, values=_FULL_ROW, ts=datetime(2026, 9, 13, 18, 30, tzinfo=timezone.utc))

    fake_conn = _FakeEudbConn(guid_by_serial={"202006003607": "11111111-1111-1111-1111-111111111111"})
    with patch("app.services.eudb_export._connect_eudb", new=AsyncMock(return_value=fake_conn)), \
         patch("app.services.eudb_export.eudb_export_configured", return_value=True):
        await run_export_once(db_session, triggered_manually=True)

    assert fake_conn.inserted, "не должно быть пропущено — тест проверяет только конвертацию времени"
    sent_timestamp = fake_conn.inserted[0][1]
    assert sent_timestamp == datetime(2026, 9, 14, 0, 30)
    assert sent_timestamp.tzinfo is None


@pytest.mark.asyncio
async def test_execute_export_run_reports_meter_not_found_in_eudb(db_session):
    gateway = await _seed_gateway(db_session)
    meter = await _seed_meter(db_session, gateway, serial="202006003607")
    await _seed_profile_row(db_session, meter, values=_FULL_ROW)

    fake_conn = _FakeEudbConn(guid_by_serial={})  # счётчик "не найден" в ЕЭБД
    with patch("app.services.eudb_export._connect_eudb", new=AsyncMock(return_value=fake_conn)), \
         patch("app.services.eudb_export.eudb_export_configured", return_value=True):
        run = await run_export_once(db_session, triggered_manually=True)

    assert run.status == EudbExportRunStatus.FAILED
    assert run.meters_failed == 1
    assert fake_conn.inserted == []


@pytest.mark.asyncio
async def test_execute_export_run_skips_meter_with_short_profile_row(db_session):
    """Другая конфигурация захвата (меньше полей, чем ожидается) —
    явная ошибка, а не угадывание по неверным индексам."""
    gateway = await _seed_gateway(db_session)
    meter = await _seed_meter(db_session, gateway, serial="202006003607")
    await _seed_profile_row(db_session, meter, values=[1.0, 2.0, 3.0])  # только 3 значения

    fake_conn = _FakeEudbConn(guid_by_serial={"202006003607": "11111111-1111-1111-1111-111111111111"})
    with patch("app.services.eudb_export._connect_eudb", new=AsyncMock(return_value=fake_conn)), \
         patch("app.services.eudb_export.eudb_export_configured", return_value=True):
        run = await run_export_once(db_session, triggered_manually=True)

    assert run.status == EudbExportRunStatus.FAILED
    assert fake_conn.inserted == []
    items_result = await db_session.get(EudbExportRun, run.id)
    assert items_result.meters_failed == 1


@pytest.mark.asyncio
async def test_execute_export_run_skips_meter_without_any_profile_data(db_session):
    gateway = await _seed_gateway(db_session)
    await _seed_meter(db_session, gateway, serial="202006003607")  # без единой строки профиля

    fake_conn = _FakeEudbConn(guid_by_serial={"202006003607": "11111111-1111-1111-1111-111111111111"})
    with patch("app.services.eudb_export._connect_eudb", new=AsyncMock(return_value=fake_conn)), \
         patch("app.services.eudb_export.eudb_export_configured", return_value=True):
        run = await run_export_once(db_session, triggered_manually=True)

    assert run.meters_failed == 1
    assert fake_conn.inserted == []


@pytest.mark.asyncio
async def test_execute_export_run_skips_already_exported_today(db_session):
    """Повторный прогон в те же сутки не пишет в ЕЭБД дубликаты — там
    есть уникальное ограничение (ref_meter, mpg_date, mpg_description),
    но лучше не долбить чужую БД заведомо обречёнными на конфликт
    вставками при повторном "Запустить сейчас"."""
    gateway = await _seed_gateway(db_session)
    meter = await _seed_meter(db_session, gateway, serial="202006003607")
    await _seed_profile_row(db_session, meter, values=_FULL_ROW)

    fake_conn = _FakeEudbConn(guid_by_serial={"202006003607": "11111111-1111-1111-1111-111111111111"})
    with patch("app.services.eudb_export._connect_eudb", new=AsyncMock(return_value=fake_conn)), \
         patch("app.services.eudb_export.eudb_export_configured", return_value=True):
        first_run = await run_export_once(db_session, triggered_manually=True)
        assert first_run.meters_succeeded == 1

        second_conn = _FakeEudbConn(guid_by_serial={"202006003607": "11111111-1111-1111-1111-111111111111"})
        with patch("app.services.eudb_export._connect_eudb", new=AsyncMock(return_value=second_conn)):
            second_run = await run_export_once(db_session, triggered_manually=True)

    assert second_run.meters_total == 1
    assert second_run.meters_succeeded == 0
    assert second_run.meters_failed == 0
    assert second_conn.inserted == []


@pytest.mark.asyncio
async def test_execute_export_run_fails_cleanly_when_not_configured(db_session):
    gateway = await _seed_gateway(db_session)
    meter = await _seed_meter(db_session, gateway, serial="202006003607")
    await _seed_profile_row(db_session, meter, values=_FULL_ROW)

    with patch("app.services.eudb_export.eudb_export_configured", return_value=False):
        run = await run_export_once(db_session, triggered_manually=True)

    assert run.status == EudbExportRunStatus.FAILED
    assert "не настроено" in run.error_message


@pytest.mark.asyncio
async def test_create_export_run_returns_immediately_running(db_session):
    run = await create_export_run(db_session, triggered_manually=True)
    assert run.status == EudbExportRunStatus.RUNNING
    assert run.id is not None


@pytest.mark.asyncio
async def test_run_now_endpoint_requires_manage_scheduled_jobs_permission(client, db_session):
    user = User(username="op", password_hash=hash_password("pass1234"), role=UserRole.OPERATOR)
    db_session.add(user)
    await db_session.commit()

    login = await client.post("/api/auth/login", data={"username": "op", "password": "pass1234"})
    token = login.json()["access_token"]

    resp = await client.post("/api/eudb-export/run-now", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_run_now_endpoint_creates_running_run_immediately(client, db_session):
    admin = User(username="root", password_hash=hash_password("pass1234"), role=UserRole.SUPER_ADMIN)
    db_session.add(admin)
    await db_session.commit()

    login = await client.post("/api/auth/login", data={"username": "root", "password": "pass1234"})
    token = login.json()["access_token"]

    with patch("app.api.eudb_export.execute_export_run", new=AsyncMock()):
        resp = await client.post("/api/eudb-export/run-now", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 202
    assert resp.json()["status"] == "running"

    list_resp = await client.get("/api/eudb-export/runs", headers={"Authorization": f"Bearer {token}"})
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1
