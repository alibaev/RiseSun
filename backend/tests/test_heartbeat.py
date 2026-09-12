"""Регрессионный тест на heartbeat-опрос Gateway (ТЗ п. 4.1.1).

Обнаруженный при ревью баг: `Gateway.last_heartbeat_at` никогда не
обновлялся — ничего не вызывало HealthCheck. `is_online` был всегда
False независимо от реального состояния Gateway.
"""

from __future__ import annotations

import asyncio
from concurrent import futures

import grpc
import pytest

from app.models import Gateway, GatewayStatus, User, UserRole
from app.services.heartbeat import _run_once
from mmws_gateway.grpc_generated import gateway_pb2, gateway_pb2_grpc


class _FakeGatewayService(gateway_pb2_grpc.GatewayServiceServicer):
    def HealthCheck(self, request, context):
        return gateway_pb2.HealthCheckResponse(
            ok=True, driver_version="test", call_home_port=2009, call_home_ports=[2009, 2010, 2011],
        )


@pytest.fixture
def fake_grpc_server():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    gateway_pb2_grpc.add_GatewayServiceServicer_to_server(_FakeGatewayService(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    yield f"127.0.0.1:{port}"
    server.stop(None)


@pytest.mark.asyncio
async def test_heartbeat_marks_reachable_gateway_online(db_session, fake_grpc_server):
    root = User(username="root", password_hash="x", role=UserRole.SUPER_ADMIN)
    db_session.add(root)
    await db_session.flush()
    gateway = Gateway(
        name="GW", grpc_target=fake_grpc_server, status=GatewayStatus.APPROVED, registered_by_id=root.id
    )
    db_session.add(gateway)
    await db_session.commit()
    await db_session.refresh(gateway)

    assert gateway.is_online is False  # до первого опроса

    await _run_once()

    await db_session.refresh(gateway)
    assert gateway.last_heartbeat_at is not None
    assert gateway.is_online is True
    assert gateway.call_home_port == 2009  # Этап 6 — отражён фактический порт из HealthCheck
    # 2026-09-12 — все реально слушаемые порты, не только основной
    # (см. models.Gateway.call_home_ports).
    assert gateway.call_home_ports == [2009, 2010, 2011]


@pytest.mark.asyncio
async def test_heartbeat_leaves_unreachable_gateway_offline(db_session):
    root = User(username="root", password_hash="x", role=UserRole.SUPER_ADMIN)
    db_session.add(root)
    await db_session.flush()
    gateway = Gateway(
        name="GW", grpc_target="127.0.0.1:1", status=GatewayStatus.APPROVED, registered_by_id=root.id
    )
    db_session.add(gateway)
    await db_session.commit()
    await db_session.refresh(gateway)

    await _run_once()

    await db_session.refresh(gateway)
    assert gateway.last_heartbeat_at is None
    assert gateway.is_online is False
