"""Ежедневный экспорт профиля нагрузки (Profile1) во внешнюю БД ЕЭБД
(2026-09-14, по прямому указанию пользователя). ЕЭБД — ЧУЖАЯ система
(отдельный сервер, Postgres 11, своя схема), не наша — подключаемся
напрямую через ``asyncpg`` в обход SQLAlchemy/Alembic (те управляют
только нашей собственной БД).

Источник данных — уже читаемый по расписанию буфер профиля нагрузки
(``load_profile_data``, OBIS Profile1), а не отдельный опрос: по
подтверждению пользователя ("у тебя в чтении профиля есть эти
данные" / "коротко, записываем туда профиль 1"), захватываемые колонки
этого буфера — напряжения L1-L3, токи L1-L3, коэффициент мощности
(cosφ) L1-L3 и суммарная активная энергия, ровно те 9 (+доп.) значений,
что нужны ЕЭБД (см. DECISIONS.md, разбор ``RS_ParseProfileGenericsCell``
и реальные данные ``load_profile_data``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import asyncpg
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal
from ..models import EudbExportItem, EudbExportRun, EudbExportRunStatus, LoadProfileData, Meter, MeterStatus
from .load_profile import DEFAULT_LOAD_PROFILE_OBIS

logger = logging.getLogger("mmws_backend.eudb_export")

_BISHKEK_TZ = ZoneInfo("Asia/Bishkek")

# Порядок колонок в буфере Profile1 у этого парка счётчиков (Risesun
# DTZY217) — см. докстринг модуля. Строки с МЕНЬШИМ числом значений
# (другая конфигурация захвата у конкретного счётчика/партии — такой
# случай уже встречался в этом проекте, см. DECISIONS.md) пропускаются
# с явной, видимой в интерфейсе ошибкой — не берём "что есть" наугад.
_EXPECTED_MIN_VALUES = 10
_VALUE_INDEX_TO_DESCRIPTION: dict[int, str] = {
    0: "Напряжение L1 (В)",
    1: "Напряжение L2 (В)",
    2: "Напряжение L3 (В)",
    3: "Сила тока L1 (А)",
    4: "Сила тока L2 (А)",
    5: "Сила тока L3 (А)",
    6: "Коэффициент мощности L1",
    7: "Коэффициент мощности L2",
    8: "Коэффициент мощности L3",
    9: "Активная энергия (кВт*ч)",
}

# SQL по спецификации пользователя (2026-09-14) — дословно, только
# параметризовано ($1/$2 вместо литералов).
_UUID_LOOKUP_SQL = """
select
  wm.mtr_guid
from
  cd.ctl_meter_models cmm,
  emacs.wrk_meters wm,
  emacs.wrk_meter_params wmp
where
  cmm.ref_producer = $1
  and wm.ref_model = cmm.mtr_guid
  and wmp.ref_meter = wm.mtr_guid
  and wmp.ref_param = '98b20dc8-aedf-4324-874b-0ccae8193397'
  and wmp.string_value = $2
"""

_INSERT_SQL = """
insert into emacs.wrk_meter_profile_generics (ref_meter, mpg_date, mpg_value, mpg_description)
values ($1, $2, $3, $4)
"""


def eudb_export_configured() -> bool:
    return bool(settings.eudb_host and settings.eudb_database and settings.eudb_user)


async def _connect_eudb() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=settings.eudb_host,
        port=settings.eudb_port,
        database=settings.eudb_database,
        user=settings.eudb_user,
        password=settings.eudb_password,
        timeout=15,
    )


def _bishkek_today_start_utc(now: datetime) -> datetime:
    return now.astimezone(_BISHKEK_TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def _already_exported_today(db: AsyncSession, *, meter_id: int, day_start_utc: datetime) -> bool:
    """Идемпотентность экспорта (не в самой ЕЭБД — там нет уникального
    ограничения на (счётчик, дата, тип значения), значит повторный
    прогон за те же сутки задвоил бы строки). Успешный экспорт этого
    счётчика СЕГОДНЯ (по Бишкеку) — не повторяем; проваленный —
    повторяем при следующем прогоне/по кнопке "Запустить сейчас"."""
    result = await db.execute(
        select(EudbExportItem.id)
        .join(EudbExportRun, EudbExportRun.id == EudbExportItem.run_id)
        .where(
            EudbExportItem.meter_id == meter_id,
            EudbExportItem.ok.is_(True),
            EudbExportRun.started_at >= day_start_utc,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _latest_profile_row(db: AsyncSession, meter_id: int) -> tuple[datetime, object] | None:
    result = await db.execute(
        select(LoadProfileData)
        .where(LoadProfileData.meter_id == meter_id, LoadProfileData.obis_code == DEFAULT_LOAD_PROFILE_OBIS)
        .order_by(LoadProfileData.timestamp.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return row.timestamp, row.values_json


async def _export_one_meter(
    db: AsyncSession, eudb_conn: asyncpg.Connection, meter: Meter,
) -> tuple[bool, int, str | None]:
    latest = await _latest_profile_row(db, meter.id)
    if latest is None:
        return False, 0, "Нет ни одной записи профиля нагрузки (Profile1) для этого счётчика"
    profile_timestamp, values = latest
    if not isinstance(values, list) or len(values) < _EXPECTED_MIN_VALUES:
        got = len(values) if isinstance(values, list) else 0
        return False, 0, (
            f"Профиль нагрузки в неожиданном формате (получено {got} значений, ожидалось не меньше "
            f"{_EXPECTED_MIN_VALUES}) — вероятно, другая конфигурация захвата у этого счётчика"
        )

    try:
        uuid_row = await eudb_conn.fetchrow(
            _UUID_LOOKUP_SQL, settings.eudb_risesun_producer_guid, meter.serial_number,
        )
    except Exception as exc:  # noqa: BLE001 — любая ошибка чужой БД должна попасть в error_message, не уронить весь прогон
        return False, 0, f"Ошибка поиска UUID в ЕЭБД: {exc}"
    if uuid_row is None:
        return False, 0, "Счётчик не найден в ЕЭБД (не совпал серийный номер под производителем Risesun)"
    meter_guid = uuid_row["mtr_guid"]

    # ``emacs.wrk_meter_profile_generics.mpg_date`` — timestamp БЕЗ
    # часового пояса; asyncpg отказывается биндить туда tz-aware
    # datetime ("can't subtract offset-naive and offset-aware
    # datetimes" — реальная ошибка, найдена 2026-09-14 на живом прогоне,
    # свалила ~2200 из 2376 счётчиков). Наш ``timestamp`` хранится как
    # правильный UTC-момент (см. job_worker._insert_load_profile_row) —
    # переводим в Asia/Bishkek (собственное время счётчика/ЕЭБД, тот же
    # принцип, что и везде в проекте) и убираем tzinfo, а не берём UTC
    # как есть.
    naive_local_timestamp = profile_timestamp.astimezone(_BISHKEK_TZ).replace(tzinfo=None)

    rows_exported = 0
    try:
        async with eudb_conn.transaction():
            for index, description in _VALUE_INDEX_TO_DESCRIPTION.items():
                await eudb_conn.execute(
                    _INSERT_SQL, meter_guid, naive_local_timestamp, float(values[index]), description,
                )
                rows_exported += 1
    except Exception as exc:  # noqa: BLE001
        return False, 0, f"Ошибка записи в ЕЭБД: {exc}"

    return True, rows_exported, None


async def run_export_once(db: AsyncSession, *, triggered_manually: bool) -> EudbExportRun:
    """Один прогон экспорта по всему активному парку. Не поднимает
    исключение при ошибках отдельных счётчиков или самой ЕЭБД —
    результат виден в ``EudbExportRun``/``EudbExportItem`` (интерфейс),
    падение всего прогона возможно только если ПОДКЛЮЧЕНИЕ к ЕЭБД не
    удалось установить вообще. Тонкая обёртка вокруг ``create_export_
    run``+``execute_export_run`` — используется циклом по расписанию
    (eudb_export_loop), которому не нужно видеть RUNNING-строку до
    завершения. Ручной запуск из API (см. api/eudb_export.py) создаёт
    и исполняет эти два шага раздельно, чтобы сразу вернуть 202 с уже
    существующей записью, не дожидаясь конца прогона."""
    run = await create_export_run(db, triggered_manually=triggered_manually)
    return await execute_export_run(db, run)


async def create_export_run(db: AsyncSession, *, triggered_manually: bool) -> EudbExportRun:
    run = EudbExportRun(triggered_manually=triggered_manually)
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def execute_export_run(db: AsyncSession, run: EudbExportRun) -> EudbExportRun:
    now = datetime.now(timezone.utc)
    day_start_utc = _bishkek_today_start_utc(now)

    if not eudb_export_configured():
        run.status = EudbExportRunStatus.FAILED
        run.error_message = "Подключение к ЕЭБД не настроено (MMWS_EUDB_HOST/MMWS_EUDB_DATABASE/MMWS_EUDB_USER)"
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(run)
        return run

    try:
        eudb_conn = await _connect_eudb()
    except Exception as exc:  # noqa: BLE001
        run.status = EudbExportRunStatus.FAILED
        run.error_message = f"Не удалось подключиться к ЕЭБД: {exc}"
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(run)
        return run

    succeeded = 0
    failed = 0
    try:
        meters_result = await db.execute(select(Meter).where(Meter.status == MeterStatus.ACTIVE))
        meters = list(meters_result.scalars().all())
        run.meters_total = len(meters)
        await db.flush()

        for meter in meters:
            if await _already_exported_today(db, meter_id=meter.id, day_start_utc=day_start_utc):
                continue
            ok, rows_exported, error_message = await _export_one_meter(db, eudb_conn, meter)
            db.add(EudbExportItem(
                run_id=run.id, meter_id=meter.id, ok=ok, rows_exported=rows_exported, error_message=error_message,
            ))
            if ok:
                succeeded += 1
            else:
                failed += 1
            await db.flush()
    finally:
        await eudb_conn.close()

    run.meters_succeeded = succeeded
    run.meters_failed = failed
    run.status = (
        EudbExportRunStatus.SUCCEEDED if failed == 0
        else EudbExportRunStatus.PARTIAL_FAILURE if succeeded > 0
        else EudbExportRunStatus.FAILED
    )
    run.finished_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(run)
    logger.info(
        "Экспорт в ЕЭБД завершён: всего=%d успех=%d ошибка=%d (%s)",
        run.meters_total, succeeded, failed, "вручную" if run.triggered_manually else "по расписанию",
    )
    return run


async def _todays_scheduled_run_exists(db: AsyncSession, day_start_utc: datetime) -> bool:
    result = await db.execute(
        select(EudbExportRun.id)
        .where(EudbExportRun.triggered_manually.is_(False), EudbExportRun.started_at >= day_start_utc)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def eudb_export_loop(stop_event: asyncio.Event) -> None:
    """Раз в 5 минут проверяет: наступило ли настроенное время
    (``MMWS_EUDB_EXPORT_HOUR/MINUTE_BISHKEK``, по умолчанию 23:50) и не
    было ли уже автоматического прогона сегодня — если да, запускает
    ``run_export_once``. Та же частота проверки, что и у
    ``stale_queued_reaper_loop`` (смена суток/расписание не требуют
    более частого опроса)."""
    logger.info(
        "Ежедневный экспорт профиля нагрузки в ЕЭБД запущен (%02d:%02d по Asia/Bishkek)",
        settings.eudb_export_hour_bishkek, settings.eudb_export_minute_bishkek,
    )
    while not stop_event.is_set():
        try:
            now = datetime.now(timezone.utc)
            now_bishkek = now.astimezone(_BISHKEK_TZ)
            target_reached = (now_bishkek.hour, now_bishkek.minute) >= (
                settings.eudb_export_hour_bishkek, settings.eudb_export_minute_bishkek,
            )
            if eudb_export_configured() and target_reached:
                day_start_utc = _bishkek_today_start_utc(now)
                async with SessionLocal() as db:
                    if not await _todays_scheduled_run_exists(db, day_start_utc):
                        await run_export_once(db, triggered_manually=False)
        except Exception:
            logger.exception("Ошибка цикла ежедневного экспорта в ЕЭБД")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=300.0)
        except asyncio.TimeoutError:
            pass
    logger.info("Ежедневный экспорт профиля нагрузки в ЕЭБД остановлен")
