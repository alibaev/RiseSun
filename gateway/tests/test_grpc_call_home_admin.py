"""Этап 6 — обнаружение новых счётчиков и живая смена call-home порта:
ListCallHomeSerials/SetCallHomePort/HealthCheck.call_home_port."""

import socket
import time

import grpc
import pytest

from mmws_gateway import grpc_server
from mmws_gateway.grpc_generated import gateway_pb2, gateway_pb2_grpc


def _free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _build_dummy_dlt645_frame(addr6: bytes, data_len: int = 14) -> bytes:
    return bytes([0x68]) + addr6 + bytes([0x68, 0x15, data_len]) + bytes(data_len) + bytes([0x00, 0x16])


def test_health_check_reports_call_home_port_zero_when_disabled():
    grpc_port = _free_port()
    server, _ = grpc_server.serve(host="127.0.0.1", port=grpc_port, call_home_port=None)
    try:
        stub = gateway_pb2_grpc.GatewayServiceStub(grpc.insecure_channel(f"127.0.0.1:{grpc_port}"))
        response = stub.HealthCheck(gateway_pb2.HealthCheckRequest())
        assert response.call_home_port == 0
    finally:
        server.stop(None)


def test_health_check_reports_actual_call_home_port():
    grpc_port = _free_port()
    call_home_port = _free_port()
    server, pool = grpc_server.serve(host="127.0.0.1", port=grpc_port, call_home_port=call_home_port)
    try:
        stub = gateway_pb2_grpc.GatewayServiceStub(grpc.insecure_channel(f"127.0.0.1:{grpc_port}"))
        response = stub.HealthCheck(gateway_pb2.HealthCheckRequest())
        assert response.call_home_port == call_home_port
    finally:
        server.stop(None)
        pool.stop()


def test_list_call_home_serials_empty_when_no_pool():
    grpc_port = _free_port()
    server, _ = grpc_server.serve(host="127.0.0.1", port=grpc_port, call_home_port=None)
    try:
        stub = gateway_pb2_grpc.GatewayServiceStub(grpc.insecure_channel(f"127.0.0.1:{grpc_port}"))
        response = stub.ListCallHomeSerials(gateway_pb2.ListCallHomeSerialsRequest())
        assert list(response.serials) == []
    finally:
        server.stop(None)


def test_list_call_home_serials_returns_seen_meter():
    grpc_port = _free_port()
    call_home_port = _free_port()
    server, pool = grpc_server.serve(host="127.0.0.1", port=grpc_port, call_home_port=call_home_port)
    try:
        conn = socket.create_connection(("127.0.0.1", call_home_port), timeout=3)
        conn.sendall(_build_dummy_dlt645_frame(bytes.fromhex("522300012020")))  # -> 202001002352
        time.sleep(0.3)

        stub = gateway_pb2_grpc.GatewayServiceStub(grpc.insecure_channel(f"127.0.0.1:{grpc_port}"))
        response = stub.ListCallHomeSerials(gateway_pb2.ListCallHomeSerialsRequest())
        serials = {s.serial: s.first_seen_unix for s in response.serials}
        assert "202001002352" in serials
        assert abs(serials["202001002352"] - time.time()) < 5
        conn.close()
    finally:
        server.stop(None)
        pool.stop()


def test_set_call_home_port_switches_live_without_restart():
    grpc_port = _free_port()
    old_call_home_port = _free_port()
    new_call_home_port = _free_port()
    server, initial_pool = grpc_server.serve(host="127.0.0.1", port=grpc_port, call_home_port=old_call_home_port)
    try:
        stub = gateway_pb2_grpc.GatewayServiceStub(grpc.insecure_channel(f"127.0.0.1:{grpc_port}"))
        response = stub.SetCallHomePort(gateway_pb2.SetCallHomePortRequest(port=new_call_home_port))
        assert response.WhichOneof("result") == "success"
        assert response.success.port == new_call_home_port

        # старый порт больше не слушает
        with pytest.raises((ConnectionRefusedError, OSError)):
            socket.create_connection(("127.0.0.1", old_call_home_port), timeout=2)

        # новый порт живой
        conn = socket.create_connection(("127.0.0.1", new_call_home_port), timeout=2)
        conn.close()

        health = stub.HealthCheck(gateway_pb2.HealthCheckRequest())
        assert health.call_home_port == new_call_home_port
    finally:
        server.stop(None)
