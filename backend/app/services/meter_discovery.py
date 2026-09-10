"""Обнаружение новых счётчиков по call-home (Этап 6) — фоновый цикл
(тот же паттерн, что и ``heartbeat.py``), опрашивающий
``Gateway.ListCallHomeSerials`` на каждом APPROVED экземпляре Gateway и
заводящий в справочник ранее неизвестные серийные номера.

Раньше добавление счётчика требовало заранее знать его серийный номер
откуда-то ещё; теперь звонящий домой счётчик сам появляется в
справочнике. С 2026-09-10 (по просьбе пользователя, ожидающего
подключение ~300 новых счётчиков) ЛЮБОЙ новый call-home серийник
автоактивируется сразу — пароль/профиль единые для всего парка
(подтверждено реальным трафиком, см. DECISIONS.md, «Массовая
активация 141 счётчика»), ручная активация через app/api/meters.py
больше не нужна как обязательный шаг.
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

# Пароль/профиль подтверждены реальным трафиком для звонящих домой
# счётчиков Risesun (см. DECISIONS.md, «Массовая активация 141
# счётчика») — единый дефолт для ВСЕХ call-home счётчиков парка,
# включая новые (см. докстринг модуля, 2026-09-10).
_AUTO_ACTIVATE_PASSWORD = b"12345678"
_AUTO_ACTIVATE_PROFILE = ProtocolProfile.HDLC_DLMS
# По словам пользователя (2026-09-10): вся новая партия (~300 счётчиков,
# ожидаемая этим заходом) — токовый класс 100А, в отличие от старого
# парка (5-7.5А, смешанно, устанавливается фактическим чтением
# read_rated_current — см. finalize_read_rated_current_job). Ставим
# сразу при автообнаружении вместо ожидания первого удачного чтения;
# реальное чтение (если случится) всё равно перезапишет этим же
# значением или уточнит его.
_NEW_BATCH_RATED_CURRENT_AMPS = 100.0


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
                meter = Meter(
                    serial_number=serial, is_call_home=True, gateway_id=gateway.id,
                    status=MeterStatus.ACTIVE, protocol_profile=_AUTO_ACTIVATE_PROFILE,
                    password_encrypted=encrypt_secret(_AUTO_ACTIVATE_PASSWORD),
                    rated_current_amps=_NEW_BATCH_RATED_CURRENT_AMPS,
                )
                auto_activated.append(meter)
                db.add(meter)
            if new_serials:
                logger.info(
                    "Обнаружено и автоактивировано %d новых счётчиков через call-home "
                    "на gateway_id=%s: %s",
                    len(new_serials), gateway.id, new_serials,
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
