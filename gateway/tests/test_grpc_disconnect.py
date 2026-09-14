"""Интеграционный тест gRPC-обёртки удалённого отключения/подключения
(Этап 5, ТЗ п.4.2.10): реальный gRPC-сервер (grpc_server.serve) + реальный
клиентский стаб, поверх программного эмулятора счётчика."""

import socket

import grpc

from mmws_gateway import grpc_server
from mmws_gateway.emulators.common import ConnectionCounter, ErrorInjection, ThreadedEmulatorServer
from mmws_gateway.emulators.hdlc_dlms_emulator import make_hdlc_dlms_handler
from mmws_gateway.grpc_generated import gateway_pb2, gateway_pb2_grpc
from mmws_gateway.protocols import dlms

SERIAL = "202006003607"
PASSWORD = "12345678"


def _start_emulator(
    *, action_state=None, action_parameters_state=None, action_force_result=None
) -> ThreadedEmulatorServer:
    counter = ConnectionCounter()
    handler = make_hdlc_dlms_handler(
        password=PASSWORD.encode("ascii"),
        obis_values={},
        error_injection=ErrorInjection(),
        counter=counter,
        action_state=action_state,
        action_parameters_state=action_parameters_state,
        action_force_result=action_force_result,
    )
    return ThreadedEmulatorServer(handler)


def _free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_disconnect_meter_success():
    action_state: dict = {}
    grpc_port = _free_port()
    with _start_emulator(action_state=action_state) as emulator:
        server, _ = grpc_server.serve(host="127.0.0.1", port=grpc_port, call_home_port=None)
        try:
            channel = grpc.insecure_channel(f"127.0.0.1:{grpc_port}")
            stub = gateway_pb2_grpc.GatewayServiceStub(channel)
            request = gateway_pb2.DisconnectMeterRequest(
                profile="hdlc_dlms", host=emulator.host, port=emulator.port,
                serial=SERIAL, password=PASSWORD, operation="disconnect", timeout_ms=1000,
            )
            response = stub.DisconnectMeter(request)
        finally:
            server.stop(None)

    assert response.WhichOneof("result") == "success"
    assert response.success.ok is True
    assert action_state[dlms.parse_obis(dlms.DISCONNECT_CONTROL_OBIS)] == dlms.METHOD_REMOTE_DISCONNECT


def test_disconnect_meter_sends_empty_structure_parameter():
    """2026-09-12 (декомпилированный референс IECMeterManage,
    ResetCommand_DLMS.cs — см. DECISIONS.md) — реальный клиент ВСЕГДА
    шлёт параметр {0x0F, 0x00} ("пустая структура") с ACTION на реле,
    иначе счётчик отвечает отказом. Раньше наш код не слал параметров
    вовсе — эта регрессия проверяет, что параметр действительно уходит
    на провод, а не только что он объявлен константой."""
    action_state: dict = {}
    action_parameters_state: dict = {}
    grpc_port = _free_port()
    with _start_emulator(action_state=action_state, action_parameters_state=action_parameters_state) as emulator:
        server, _ = grpc_server.serve(host="127.0.0.1", port=grpc_port, call_home_port=None)
        try:
            channel = grpc.insecure_channel(f"127.0.0.1:{grpc_port}")
            stub = gateway_pb2_grpc.GatewayServiceStub(channel)
            request = gateway_pb2.DisconnectMeterRequest(
                profile="hdlc_dlms", host=emulator.host, port=emulator.port,
                serial=SERIAL, password=PASSWORD, operation="disconnect", timeout_ms=1000,
            )
            response = stub.DisconnectMeter(request)
        finally:
            server.stop(None)

    assert response.success.ok is True
    obis = dlms.parse_obis(dlms.DISCONNECT_CONTROL_OBIS)
    assert action_parameters_state[obis] == dlms.DISCONNECT_ACTION_PARAMETERS


def test_reconnect_meter_success():
    action_state: dict = {}
    grpc_port = _free_port()
    with _start_emulator(action_state=action_state) as emulator:
        server, _ = grpc_server.serve(host="127.0.0.1", port=grpc_port, call_home_port=None)
        try:
            channel = grpc.insecure_channel(f"127.0.0.1:{grpc_port}")
            stub = gateway_pb2_grpc.GatewayServiceStub(channel)
            request = gateway_pb2.DisconnectMeterRequest(
                profile="hdlc_dlms", host=emulator.host, port=emulator.port,
                serial=SERIAL, password=PASSWORD, operation="reconnect", timeout_ms=1000,
            )
            response = stub.DisconnectMeter(request)
        finally:
            server.stop(None)

    assert response.WhichOneof("result") == "success"
    assert action_state[dlms.parse_obis(dlms.DISCONNECT_CONTROL_OBIS)] == dlms.METHOD_REMOTE_RECONNECT


def test_disconnect_meter_failure_yields_error():
    grpc_port = _free_port()
    with _start_emulator(action_force_result=3) as emulator:  # read-write-denied
        server, _ = grpc_server.serve(host="127.0.0.1", port=grpc_port, call_home_port=None)
        try:
            channel = grpc.insecure_channel(f"127.0.0.1:{grpc_port}")
            stub = gateway_pb2_grpc.GatewayServiceStub(channel)
            request = gateway_pb2.DisconnectMeterRequest(
                profile="hdlc_dlms", host=emulator.host, port=emulator.port,
                serial=SERIAL, password=PASSWORD, operation="disconnect", timeout_ms=1000,
            )
            response = stub.DisconnectMeter(request)
        finally:
            server.stop(None)

    assert response.WhichOneof("result") == "error"
    assert response.error.code == "GATEWAY_ERROR"


def test_disconnect_meter_unknown_operation_yields_error():
    grpc_port = _free_port()
    with _start_emulator() as emulator:
        server, _ = grpc_server.serve(host="127.0.0.1", port=grpc_port, call_home_port=None)
        try:
            channel = grpc.insecure_channel(f"127.0.0.1:{grpc_port}")
            stub = gateway_pb2_grpc.GatewayServiceStub(channel)
            request = gateway_pb2.DisconnectMeterRequest(
                profile="hdlc_dlms", host=emulator.host, port=emulator.port,
                serial=SERIAL, password=PASSWORD, operation="delete_everything", timeout_ms=1000,
            )
            response = stub.DisconnectMeter(request)
        finally:
            server.stop(None)

    assert response.WhichOneof("result") == "error"
