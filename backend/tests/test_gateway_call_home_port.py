"""Этап 6 (панель суперадминистратора) — PUT /api/gateways/{id}/call-home-port."""

from __future__ import annotations

from concurrent import futures

import grpc
import pytest

from app.core.security import hash_password
from app.models import Gateway, GatewayStatus, User, UserRole
from mmws_gateway.grpc_generated import gateway_pb2, gateway_pb2_grpc


class _FakeGatewayService(gateway_pb2_grpc.GatewayServiceServicer):
    def __init__(self, *, reject: bool = False) -> None:
        self._reject = reject

    def SetCallHomePort(self, request, context):
        if self._reject:
            return gateway_pb2.SetCallHomePortResponse(
                error=gateway_pb2.ReadError(code="GATEWAY_ERROR", message="порт занят")
            )
        return gateway_pb2.SetCallHomePortResponse(success=gateway_pb2.SetCallHomePortSuccess(port=request.port))


def _start_fake_server(*, reject: bool = False):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    gateway_pb2_grpc.add_GatewayServiceServicer_to_server(_FakeGatewayService(reject=reject), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return server, f"127.0.0.1:{port}"


async def _seed_user(db, *, username: str, password: str, role: UserRole) -> User:
    user = User(username=username, password_hash=hash_password(password), role=role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client, username: str, password: str) -> str:
    resp = await client.post("/api/auth/login", data={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.mark.asyncio
async def test_set_call_home_port_success(client, db_session):
    fake_server, target = _start_fake_server()
    try:
        root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
        gateway = Gateway(name="GW", grpc_target=target, status=GatewayStatus.APPROVED, registered_by_id=root.id)
        db_session.add(gateway)
        await db_session.commit()
        await db_session.refresh(gateway)
        token = await _login(client, "root", "pass1234")

        resp = await client.put(
            f"/api/gateways/{gateway.id}/call-home-port",
            json={"port": 2222},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["call_home_port"] == 2222

        await db_session.refresh(gateway)
        assert gateway.call_home_port == 2222
    finally:
        fake_server.stop(None)


@pytest.mark.asyncio
async def test_set_call_home_port_rejected_by_gateway(client, db_session):
    fake_server, target = _start_fake_server(reject=True)
    try:
        root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
        gateway = Gateway(name="GW", grpc_target=target, status=GatewayStatus.APPROVED, registered_by_id=root.id)
        db_session.add(gateway)
        await db_session.commit()
        await db_session.refresh(gateway)
        token = await _login(client, "root", "pass1234")

        resp = await client.put(
            f"/api/gateways/{gateway.id}/call-home-port",
            json={"port": 2222},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 502

        await db_session.refresh(gateway)
        assert gateway.call_home_port is None  # не сохранено при отказе
    finally:
        fake_server.stop(None)


@pytest.mark.asyncio
async def test_set_call_home_port_unreachable_gateway(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    gateway = Gateway(
        name="GW", grpc_target="127.0.0.1:1", status=GatewayStatus.APPROVED, registered_by_id=root.id
    )
    db_session.add(gateway)
    await db_session.commit()
    await db_session.refresh(gateway)
    token = await _login(client, "root", "pass1234")

    resp = await client.put(
        f"/api/gateways/{gateway.id}/call-home-port",
        json={"port": 2222},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_set_call_home_port_requires_manage_gateways(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    await _seed_user(db_session, username="admin", password="pass1234", role=UserRole.ADMIN)
    gateway = Gateway(name="GW", grpc_target="localhost:50051", status=GatewayStatus.APPROVED, registered_by_id=root.id)
    db_session.add(gateway)
    await db_session.commit()
    await db_session.refresh(gateway)
    admin_token = await _login(client, "admin", "pass1234")

    resp = await client.put(
        f"/api/gateways/{gateway.id}/call-home-port",
        json={"port": 2222},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 403  # MANAGE_GATEWAYS — только Супер-администратор
