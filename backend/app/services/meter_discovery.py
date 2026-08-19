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

from ..db import SessionLocal
from ..models import Gateway, GatewayStatus, Meter, MeterStatus
from .gateway_client import list_call_home_serials

logger = logging.getLogger("mmws_backend.meter_discovery")

_CHECK_INTERVAL_S = 30.0


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
            for serial in new_serials:
                db.add(
                    Meter(
                        serial_number=serial, is_call_home=True,
                        status=MeterStatus.INSTALLED, gateway_id=gateway.id,
                    )
                )
            if new_serials:
                logger.info(
                    "Обнаружено %d новых счётчиков через call-home на gateway_id=%s: %s",
                    len(new_serials), gateway.id, new_serials,
                )
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
