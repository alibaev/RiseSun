"""Периодический heartbeat-опрос зарегистрированных Gateway (ТЗ п. 4.1.1 —
статус online/offline, время последнего heartbeat в реестре).

Без этого `Gateway.last_heartbeat_at` никогда не обновлялся бы, и
`is_online` был бы всегда False независимо от реального состояния
Gateway (обнаружено при ревью, 2026-08-18).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import grpc
from sqlalchemy import select

from mmws_gateway.grpc_generated import gateway_pb2, gateway_pb2_grpc

from ..db import SessionLocal
from ..models import Gateway, GatewayStatus

logger = logging.getLogger("mmws_backend.heartbeat")

_HEARTBEAT_INTERVAL_S = 30.0
_CALL_TIMEOUT_S = 5.0


async def _check_one(gateway_id: int, grpc_target: str) -> object | None:
    """Возвращает HealthCheckResponse при успехе, иначе None."""
    try:
        async with grpc.aio.insecure_channel(grpc_target) as channel:
            stub = gateway_pb2_grpc.GatewayServiceStub(channel)
            response = await stub.HealthCheck(
                gateway_pb2.HealthCheckRequest(), timeout=_CALL_TIMEOUT_S
            )
            return response if response.ok else None
    except grpc.RpcError as exc:
        logger.warning("Heartbeat gateway_id=%s (%s) не удался: %s", gateway_id, grpc_target, exc)
        return None


async def _run_once() -> None:
    async with SessionLocal() as db:
        result = await db.execute(select(Gateway).where(Gateway.status == GatewayStatus.APPROVED))
        gateways = list(result.scalars().all())

    if not gateways:
        return

    responses = await asyncio.gather(
        *(_check_one(g.id, g.grpc_target) for g in gateways), return_exceptions=False
    )

    async with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        for gateway, response in zip(gateways, responses):
            if response is None:
                continue
            db_gateway = await db.get(Gateway, gateway.id)
            if db_gateway is not None:
                db_gateway.last_heartbeat_at = now
                # Этап 6 — отражаем ФАКТИЧЕСКИЙ порт call-home (может быть
                # изменён в обход панели, например переменной окружения
                # при перезапуске контейнера, не только через
                # SetCallHomePort — см. app/api/gateways.py).
                db_gateway.call_home_port = response.call_home_port or None
                # 2026-09-12 — все реально слушаемые порты (см.
                # models.Gateway.call_home_ports), не только основной.
                db_gateway.call_home_ports = list(response.call_home_ports) or None
        await db.commit()


async def heartbeat_loop(stop_event: asyncio.Event) -> None:
    logger.info("Heartbeat-опрос Gateway запущен (interval=%.0fs)", _HEARTBEAT_INTERVAL_S)
    while not stop_event.is_set():
        try:
            await _run_once()
        except Exception:  # noqa: BLE001 — один сбойный опрос не должен останавливать цикл
            logger.exception("Ошибка цикла heartbeat-опроса")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_HEARTBEAT_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
    logger.info("Heartbeat-опрос Gateway остановлен")
