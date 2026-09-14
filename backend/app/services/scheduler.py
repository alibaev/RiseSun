"""Планировщик автоматического опроса по расписанию (ТЗ п.4.2.6).

Фоновый цикл (тот же паттерн, что и ``heartbeat.py``/``job_worker.py``),
проверяющий каждые ``settings.scheduler_check_interval_s`` секунд, какие
включённые ``ScheduledJob`` должны сработать (croniter, следующий момент
срабатывания после ``last_run_at`` не позже текущего времени).
Срабатывание создаёт ``ScheduledJobRun`` и по одной ``Job`` на каждый
счётчик группы — сами задачи выполняет обычный ``job_worker`` (планировщик
только ставит их в очередь, не читает счётчики сам — то же разделение
ответственности, что и у ручного запуска через API,
Promt_MMWS.md, раздел 3, принцип 3).

``operation_params.skip_if_read_today`` (2026-09-07, согласовано с
пользователем для ежедневного опроса всех счётчиков) — опциональный
флаг для ``job_type="read_current"``: если включён, счётчики, у которых
уже есть ``MeterReading`` по тому же OBIS за ТЕКУЩИЕ сутки по времени
Asia/Bishkek (UTC+6, без перехода на летнее), из очередного запуска
исключаются. Это вместе с частым cron (например, каждые 30 минут)
реализует «зафиксировать показание на 00:00 Бишкек, если не вышло — на
00:30, потом на 01:00 и так далее, пока не получится, но не опрашивать
повторно счётчик, который уже отчитался за эти сутки» — без этого
флага (по умолчанию выключен) поведение прежнее: расписание всегда
опрашивает весь список ``meter_ids``.

``job_type="read_rated_current"`` (2026-09-07, согласовано с
пользователем) — аналогичный принцип, но БЕЗ суточного окна: токовый
класс счётчика (``Meter.rated_current_amps``) — статичный параметр, не
меняется со временем, поэтому счётчик с уже известным значением
исключается из группы НАВСЕГДА, не только на текущие сутки.

``job_type="read_load_profile"`` + ``operation_params.skip_if_read_today``
(2026-09-10, "постоянное чтение profile1", см. DECISIONS.md) — тот же
принцип суточного окна, что и у ``read_current``, но проверка идёт по
факту успешного завершения Job (``_already_succeeded_today_meter_ids``),
а не по ``MeterReading`` — у GetRowsByRange нет единой строки-показания
с временем чтения."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal
from ..models import (
    Job,
    JobStatus,
    Meter,
    MeterReading,
    PollProfile,
    ScheduledJob,
    ScheduledJobRun,
    ScheduledJobRunStatus,
)

logger = logging.getLogger("mmws_backend.scheduler")

_DEFAULT_LOAD_PROFILE_WINDOW_HOURS = 24
_BISHKEK_TZ = ZoneInfo("Asia/Bishkek")


def next_fire_time(cron_expression: str, base: datetime) -> datetime:
    itr = croniter(cron_expression, base)
    next_fire = itr.get_next(datetime)
    if next_fire.tzinfo is None:
        next_fire = next_fire.replace(tzinfo=timezone.utc)
    return next_fire


def _is_due(scheduled_job: ScheduledJob, now: datetime) -> bool:
    base = scheduled_job.last_run_at or scheduled_job.created_at
    try:
        return next_fire_time(scheduled_job.cron_expression, base) <= now
    except (ValueError, KeyError):
        logger.error(
            "Расписание id=%s имеет некорректное cron-выражение %r — пропускаю",
            scheduled_job.id, scheduled_job.cron_expression,
        )
        return False


async def _resolve_payloads(db: AsyncSession, scheduled_job: ScheduledJob) -> list[dict]:
    """Возвращает список payload для Job этого расписания — как правило
    один элемент (как было исторически), но для ``read_current`` с
    привязанным профилем опроса (``operation_params.poll_profile_id`` —
    2026-09-08, по просьбе пользователя) один на каждый ВКЛЮЧЁННЫЙ пункт
    профиля: каждый OBIS читается отдельной Job (тот же принцип, что и
    у ``parameter_schemes.apply_scheme`` — одна пара параметр×счётчик =
    одна независимо наблюдаемая/переповторяемая задача), а не одним
    payload на весь тик. Пустой список означает «профиль не найден —
    пропустить тик» (см. ``_trigger_one``), а не «нет счётчиков»."""
    if scheduled_job.job_type == "read_load_profile":
        # from_iso/to_iso уходят в GET-диапазон НАПРЯМУЮ как метка
        # СОБСТВЕННЫХ часов счётчика (Asia/Bishkek местное время, см.
        # DECISIONS.md, "Профиль нагрузки: метка времени сохранялась
        # как UTC вместо Asia/Bishkek") — раньше здесь ошибочно
        # использовалось «сейчас по UTC» с отброшенным поясом (тот же
        # класс бага, что уже был найден и исправлен в других местах;
        # этот код был не замечен раньше, т.к. расписание не было
        # включено). Теперь — «сейчас по Бишкеку», наивно.
        now_bishkek = datetime.now(_BISHKEK_TZ).replace(tzinfo=None)
        if scheduled_job.operation_params.get("since_midnight_bishkek"):
            # 2026-09-12 (по просьбе пользователя — окно "показания
            # 00:00-03:00, профиль 03:00-09:00, дальше добивать
            # параллельно") — окно "с начала текущих суток (Бишкек) до
            # сейчас", РАСТУЩЕЕ с каждым тиком, а не скользящее
            # window_hours назад от "сейчас": иначе повторные попытки в
            # конце окна (например, в 08:50) теряли бы полночь из
            # диапазона, хотя именно полночь и была целью опроса.
            from_dt = now_bishkek.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            window_hours = scheduled_job.operation_params.get("window_hours", _DEFAULT_LOAD_PROFILE_WINDOW_HOURS)
            from_dt = now_bishkek - timedelta(hours=window_hours)
        payload = {"from_iso": from_dt.isoformat(), "to_iso": now_bishkek.isoformat()}
        if scheduled_job.operation_params.get("obis"):
            payload["obis"] = scheduled_job.operation_params["obis"]
        return [payload]
    if scheduled_job.job_type == "read_rated_current":
        return [{}]  # OBIS фиксирован в job_worker.RATED_CURRENT_OBIS, не параметризуется
    # read_current
    profile_id = scheduled_job.operation_params.get("poll_profile_id")
    if profile_id is not None:
        profile = await db.get(PollProfile, profile_id)
        if profile is None:
            logger.warning(
                "Расписание id=%s (%s) ссылается на удалённый профиль опроса id=%s — пропуск тика",
                scheduled_job.id, scheduled_job.name, profile_id,
            )
            return []
        return [{"obis": item["obis"]} for item in profile.items if item.get("enabled", True)]
    payload = {"obis": scheduled_job.operation_params.get("obis", "1.1.1.8.0.ff")}
    # class_id (2026-09-10, "постоянное чтение profile1") — опциональный
    # override для read_current, когда OBIS принадлежит не Register'у
    # (класс по умолчанию, 0 → Register), а другому классу — например,
    # 7 (ProfileGeneric) для простого GET текущего буфера профиля
    # нагрузки без диапазона дат, в отличие от отдельного job_type
    # read_load_profile (GetRowsByRange). Пробрасывается дальше в
    # job_worker._run_read_current так же, как уже пробрасывается для
    # ручных диагностических job (см. DECISIONS.md, Association View).
    if scheduled_job.operation_params.get("class_id") is not None:
        payload["class_id"] = scheduled_job.operation_params["class_id"]
    return [payload]


def _bishkek_day_bounds_utc(now: datetime) -> tuple[datetime, datetime]:
    day_start_bishkek = now.astimezone(_BISHKEK_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = day_start_bishkek.astimezone(timezone.utc)
    return day_start_utc, day_start_utc + timedelta(days=1)


async def _already_read_today_meter_ids(
    db: AsyncSession, meter_ids: list[int], obis: str, now: datetime
) -> set[int]:
    day_start_utc, day_end_utc = _bishkek_day_bounds_utc(now)
    result = await db.execute(
        select(MeterReading.meter_id).where(
            MeterReading.meter_id.in_(meter_ids),
            MeterReading.obis_code == obis,
            MeterReading.read_at >= day_start_utc,
            MeterReading.read_at < day_end_utc,
        )
    )
    return set(result.scalars().all())


async def _already_succeeded_today_meter_ids(
    db: AsyncSession, meter_ids: list[int], job_type: str, now: datetime
) -> set[int]:
    """Аналог ``_already_read_today_meter_ids``, но по факту завершения
    Job'ы (``JobStatus.SUCCEEDED``), а не по ``MeterReading`` — для
    ``read_load_profile`` (2026-09-10, см. DECISIONS.md, "постоянное
    чтение profile1") нет единой строки-показания с ``read_at``, есть
    множество строк буфера с ``timestamp`` — временем самой ЗАПИСИ на
    счётчике, а не временем НАШЕГО чтения, поэтому для суточного окна
    годится только время завершения самой Job."""
    day_start_utc, day_end_utc = _bishkek_day_bounds_utc(now)
    result = await db.execute(
        select(Job.meter_id).where(
            Job.meter_id.in_(meter_ids),
            Job.job_type == job_type,
            Job.status == JobStatus.SUCCEEDED,
            Job.finished_at >= day_start_utc,
            Job.finished_at < day_end_utc,
        )
    )
    return set(result.scalars().all())


async def _outstanding_job_meter_ids(db: AsyncSession, meter_ids: list[int], job_type: str) -> set[int]:
    """Счётчики, у которых уже есть НЕЗАВЕРШЁННЫЙ (QUEUED/RUNNING) Job
    этого же job_type — от предыдущего срабатывания этого же
    расписания, если воркер ещё не успел до него дойти. Без этой
    проверки частый cron (`*/30 * * * *`) на медленной call-home
    очереди (сотни секунд на попытку, низкий процент успеха) плодит
    дубликаты быстрее, чем воркеры успевают их разбирать — найденный
    баг, 2026-09-08: у части счётчиков накопилось по 14-15 одинаковых
    QUEUED job'ов, а другие счётчики из той же группы так ни разу и не
    были опробованы за ночь (FIFO-очередь тонет в дублях одних и тех
    же счётчиков)."""
    result = await db.execute(
        select(Job.meter_id).where(
            Job.meter_id.in_(meter_ids),
            Job.job_type == job_type,
            Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
        )
    )
    return set(result.scalars().all())


async def _trigger_one(db: AsyncSession, scheduled_job: ScheduledJob) -> None:
    now = datetime.now(timezone.utc)
    # is_active=False здесь — не косметика: без этого фильтра деактиви-
    # рованный администратором счётчик продолжал бы попадать в jobs из
    # старого scheduled_job.meter_ids и вхолостую жечь попытки воркеров
    # каждый цикл расписания.
    #
    # operation_params.all_active_meters (2026-09-11, по просьбе
    # пользователя — "сохрани эти функции для новых счётчиков на
    # будущее") — meter_ids фиксируется один раз в момент создания
    # расписания и НЕ растёт сам по себе, когда meter_discovery.py
    # активирует новые звонящие счётчики. Для расписаний "читать весь
    # активный парк" (показания, токовый класс) это неверно — счётчик,
    # активированный ПОСЛЕ создания расписания, должен тоже попадать в
    # опрос без ручного редактирования meter_ids. При этом флаге список
    # счётчиков считается заново на каждом тике по Meter.is_active,
    # meter_ids расписания игнорируется.
    if scheduled_job.operation_params.get("all_active_meters"):
        meters = (await db.execute(select(Meter).where(Meter.is_active.is_(True)))).scalars().all()
    else:
        meters = (
            await db.execute(
                select(Meter).where(Meter.id.in_(scheduled_job.meter_ids), Meter.is_active.is_(True))
            )
        ).scalars().all()

    already_outstanding = await _outstanding_job_meter_ids(
        db, [m.id for m in meters], scheduled_job.job_type
    )
    meters = [m for m in meters if m.id not in already_outstanding]

    payloads = await _resolve_payloads(db, scheduled_job)
    if not payloads:
        # Профиль опроса удалён — уже залогировано в _resolve_payloads.
        scheduled_job.last_run_at = now
        await db.commit()
        return

    # (meter, payload) — по одной паре на каждый OBIS профиля (обычно
    # payloads из одного элемента, как было исторически до профилей
    # опроса, 2026-09-08).
    pairs: list[tuple[Meter, dict]] = []
    skip_reason = "у всех счётчиков группы уже есть невыполненная задача этого типа"
    if scheduled_job.job_type == "read_current" and scheduled_job.operation_params.get("skip_if_read_today"):
        for payload in payloads:
            already_read = await _already_read_today_meter_ids(
                db, [m.id for m in meters], payload["obis"], now
            )
            pairs.extend((m, payload) for m in meters if m.id not in already_read)
        skip_reason = "все счётчики группы уже опрошены сегодня по всем OBIS профиля (Asia/Bishkek)"
    elif scheduled_job.job_type == "read_load_profile" and scheduled_job.operation_params.get("skip_if_read_today"):
        # 2026-09-10 ("постоянное чтение profile1", см. DECISIONS.md):
        # та же суточная логика, что и у read_current выше, но по факту
        # успешного завершения Job (см. _already_succeeded_today_meter_ids)
        # — GetRowsByRange не даёт одной строки-показания с read_at.
        already_read = await _already_succeeded_today_meter_ids(
            db, [m.id for m in meters], scheduled_job.job_type, now
        )
        pairs = [(m, payload) for m in meters if m.id not in already_read for payload in payloads]
        skip_reason = "все счётчики группы уже успешно прочитаны сегодня (Asia/Bishkek)"
    elif scheduled_job.job_type == "read_rated_current":
        # Токовый класс (rated_current_amps) — статичный паспортный
        # параметр, не меняется у счётчика со временем, поэтому здесь
        # НЕТ суточного окна — счётчик, у которого он уже известен,
        # исключается НАВСЕГДА, а не до конца текущих суток (в отличие
        # от skip_if_read_today выше).
        pairs = [(m, payloads[0]) for m in meters if m.rated_current_amps is None]
        skip_reason = "у всех счётчиков группы токовый класс уже известен"
    else:
        pairs = [(m, payload) for m in meters for payload in payloads]

    scheduled_job.last_run_at = now
    if not pairs:
        await db.commit()
        logger.info(
            "Расписание id=%s (%s) сработало — %s, пропуск",
            scheduled_job.id, scheduled_job.name, skip_reason or "пустая группа",
        )
        return

    distinct_meter_ids = {meter.id for meter, _ in pairs}
    run = ScheduledJobRun(
        scheduled_job_id=scheduled_job.id,
        status=ScheduledJobRunStatus.RUNNING,
        meters_total=len(distinct_meter_ids),
    )
    db.add(run)
    await db.flush()

    for meter, payload in pairs:
        db.add(
            Job(
                job_type=scheduled_job.job_type,
                meter_id=meter.id,
                payload=payload,
                scheduled_job_run_id=run.id,
            )
        )

    await db.commit()
    logger.info(
        "Расписание id=%s (%s) сработало — run id=%s, счётчиков: %d, задач: %d",
        scheduled_job.id, scheduled_job.name, run.id, len(distinct_meter_ids), len(pairs),
    )


async def _run_once() -> None:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        result = await db.execute(select(ScheduledJob).where(ScheduledJob.is_enabled.is_(True)))
        due = [j for j in result.scalars().all() if _is_due(j, now)]
        for scheduled_job in due:
            await _trigger_one(db, scheduled_job)


async def scheduler_loop(stop_event: asyncio.Event) -> None:
    logger.info("Планировщик расписаний запущен (interval=%.0fs)", settings.scheduler_check_interval_s)
    while not stop_event.is_set():
        try:
            await _run_once()
        except Exception:  # noqa: BLE001 — один сбойный тик не должен останавливать цикл
            logger.exception("Ошибка цикла планировщика расписаний")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.scheduler_check_interval_s)
        except asyncio.TimeoutError:
            pass
    logger.info("Планировщик расписаний остановлен")
