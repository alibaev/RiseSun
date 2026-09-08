"""Обнаружение новых счётчиков по call-home (Этап 6) — фоновый цикл
(тот же паттерн, что и ``heartbeat.py``), опрашивающий
``Gateway.ListCallHomeSerials`` на каждом APPROVED экземпляре Gateway и
заводящий в справочник ранее неизвестные серийные номера со статусом
``MeterStatus.INSTALLED`` — раньше добавление счётчика требовало заранее
знать его серийный номер откуда-то ещё; теперь звонящий домой счётчик
сам появляется в справочнике, администратору остаётся только
активировать его (указать пароль/протокол, см. app/api/meters.py).
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from ..core.security import encrypt_secret
from ..db import SessionLocal
from ..models import Gateway, GatewayStatus, Meter, MeterStatus, ProtocolProfile, ScheduledJob
from .gateway_client import list_call_home_serials

logger = logging.getLogger("mmws_backend.meter_discovery")

_CHECK_INTERVAL_S = 30.0

# Серийники 6 тестовых счётчиков, удалённых 2026-09-08 (см. DECISIONS.md,
# «Чистка БД: 6 тестовых счётчиков и битый gateway_id=1») — среди них
# были счётчики, реально использовавшиеся для первых тестов, не только
# синтетические заглушки. По просьбе пользователя: если ОДИН ИЗ ЭТИХ
# серийников когда-либо позвонит домой снова, заводить его сразу
# ГОТОВЫМ к работе (пароль/профиль/ACTIVE), а не оставлять как обычное
# автообнаружение в INSTALLED — для всех остальных, ранее неизвестных
# серийников поведение не меняется.
_KNOWN_TEST_SERIALS = frozenset(
    {"202006003607", "999000111222", "202099009999", "900000000001", "900000000002", "999888777666"}
)
# Пароль/профиль подтверждены реальным трафиком для звонящих домой
# счётчиков Risesun (см. DECISIONS.md, «Массовая активация 141
# счётчика»), тот же дефолт используется для остальных call-home
# счётчиков парка.
_AUTO_ACTIVATE_PASSWORD = b"12345678"
_AUTO_ACTIVATE_PROFILE = ProtocolProfile.HDLC_DLMS


async def _add_to_catchall_schedules(db, meter_id: int) -> None:
    """Активного статуса самого по себе недостаточно, чтобы счётчик
    реально опрашивался — планировщик (``scheduler._trigger_one``)
    берёт группу строго по ``ScheduledJob.meter_ids`` (статический
    список), без какой-либо фильтрации по ``status``/``is_active``.
    Эвристика вместо жёстко зашитых id расписаний: для каждого
    job_type берём расписание с САМЫМ БОЛЬШИМ существующим списком
    счётчиков — это и есть общая группа "все счётчики" (в отличие от
    узких вроде "Smoke test"), устойчиво к переименованию/добавлению
    расписаний."""
    for job_type in ("read_current", "read_rated_current"):
        schedules = (
            await db.execute(select(ScheduledJob).where(ScheduledJob.job_type == job_type))
        ).scalars().all()
        if not schedules:
            continue
        biggest = max(schedules, key=lambda s: len(s.meter_ids))
        if meter_id not in biggest.meter_ids:
            biggest.meter_ids = [*biggest.meter_ids, meter_id]


async def _run_once() -> None:
    async with SessionLocal() as db:
        gateways = (
            await db.execute(select(Gateway).where(Gateway.status == GatewayStatus.APPROVED))
        ).scalars().all()

    for gateway in gateways:
        try:
            seen = await list_call_home_serials(grpc_target=gateway.grpc_target)
        except Exception:  # noqa: BLE001 — недоступность одного Gateway не должна останавливать цикл
            logger.warning(
                "Не удалось опросить ListCallHomeSerials на gateway_id=%s (%s)",
                gateway.id, gateway.grpc_target, exc_info=True,
            )
            continue
        if not seen:
            continue

        async with SessionLocal() as db:
            existing_serials = set((await db.execute(select(Meter.serial_number))).scalars().all())
            new_serials = [s.serial for s in seen if s.serial not in existing_serials]
            auto_activated = []
            for serial in new_serials:
                if serial in _KNOWN_TEST_SERIALS:
                    meter = Meter(
                        serial_number=serial, is_call_home=True, gateway_id=gateway.id,
                        status=MeterStatus.ACTIVE, protocol_profile=_AUTO_ACTIVATE_PROFILE,
                        password_encrypted=encrypt_secret(_AUTO_ACTIVATE_PASSWORD),
                    )
                    auto_activated.append(meter)
                else:
                    meter = Meter(
                        serial_number=serial, is_call_home=True,
                        status=MeterStatus.INSTALLED, gateway_id=gateway.id,
                    )
                db.add(meter)
            if new_serials:
                logger.info(
                    "Обнаружено %d новых счётчиков через call-home на gateway_id=%s: %s",
                    len(new_serials), gateway.id, new_serials,
                )
                if auto_activated:
                    logger.info(
                        "Из них %d — известные ранее удалённые тестовые счётчики, "
                        "автоактивированы: %s",
                        len(auto_activated), [m.serial_number for m in auto_activated],
                    )
                await db.flush()  # нужны meter.id для добавления в расписания
                for meter in auto_activated:
                    await _add_to_catchall_schedules(db, meter.id)
                await db.commit()


async def meter_discovery_loop(stop_event: asyncio.Event) -> None:
    logger.info("Цикл обнаружения новых счётчиков запущен (interval=%.0fs)", _CHECK_INTERVAL_S)
    while not stop_event.is_set():
        try:
            await _run_once()
        except Exception:  # noqa: BLE001 — один сбойный тик не должен останавливать цикл
            logger.exception("Ошибка цикла обнаружения новых счётчиков")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_CHECK_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
    logger.info("Цикл обнаружения новых счётчиков остановлен")
