"""Интеграционный тест gRPC-обёртки профиля нагрузки (Этап 3, ТЗ п.4.2.3):
реальный gRPC-сервер (grpc_server.serve) + реальный клиентский стаб,
поверх программного эмулятора счётчика (ThreadedEmulatorServer) — та же
связка, что и test_load_profile.py, но проверяет именно транспортный
слой Backend <-> Gateway (server-streaming RPC), а не протокол DLMS
напрямую.
"""

import json
import socket
from datetime import datetime, timedelta

import grpc

from mmws_gateway import grpc_server
from mmws_gateway.emulators.common import ConnectionCounter, ErrorInjection, ThreadedEmulatorServer
from mmws_gateway.emulators.hdlc_dlms_emulator import make_hdlc_dlms_handler
from mmws_gateway.grpc_generated import gateway_pb2, gateway_pb2_grpc
from mmws_gateway.protocols import dlms

SERIAL = "202006003607"
PASSWORD = "12345678"
LOAD_PROFILE_OBIS = "1.1.63.1.0.ff"  # см. test_load_profile.py — decimal 1-1:99.1.0.255 в hex-нотации
FROM_DT = datetime(2026, 8, 1)


def _row(index: int, value: int) -> tuple[datetime, list[tuple[float, int, int]]]:
    return FROM_DT + timedelta(minutes=index), [(float(value), 4, 0)]


def _start_emulator(
    rows: list[tuple[datetime, list[tuple[float, int, int]]]], *, block_size: int
) -> ThreadedEmulatorServer:
    counter = ConnectionCounter()
    handler = make_hdlc_dlms_handler(
        password=PASSWORD.encode("ascii"),
        obis_values={},
        error_injection=ErrorInjection(),
        counter=counter,
        load_profile_obis=dlms.parse_obis(LOAD_PROFILE_OBIS),
        load_profile_rows=rows,
        load_profile_block_size=block_size,
    )
    return ThreadedEmulatorServer(handler)


def _free_port() -> int:
    """Выбирает свободный порт заранее (bind-probe-release) — тот же
    приём, что ``ThreadedEmulatorServer`` делает изнутри через
    ``bind(("127.0.0.1", 0))``, но нужен здесь отдельно: адрес gRPC-
    сервера должен быть известен ДО вызова ``grpc_server.serve()``,
    чтобы клиентский стаб мог к нему подключиться."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_read_load_profile_streams_rows_over_grpc():
    rows = [_row(h, 3000 + h) for h in range(5)]
    grpc_port = _free_port()

    with _start_emulator(rows, block_size=12) as emulator:  # маленький block_size — форсирует блочную передачу
        server, _ = grpc_server.serve(host="127.0.0.1", port=grpc_port, call_home_port=None)
        try:
            channel = grpc.insecure_channel(f"127.0.0.1:{grpc_port}")
            stub = gateway_pb2_grpc.GatewayServiceStub(channel)
            request = gateway_pb2.ReadLoadProfileRequest(
                profile="hdlc_dlms",
                host=emulator.host,
                port=emulator.port,
                serial=SERIAL,
                password=PASSWORD,
                obis=LOAD_PROFILE_OBIS,
                class_id=dlms.PROFILE_GENERIC_CLASS_ID,
                from_iso="2026-08-01T00:00:00",
                to_iso="2026-08-19T00:00:00",
                timeout_ms=1000,
            )
            responses = list(stub.ReadLoadProfile(request))
        finally:
            server.stop(None)

    assert len(responses) == 5
    for i, response in enumerate(responses):
        assert response.WhichOneof("result") == "row"
        expected_ts = FROM_DT + timedelta(minutes=i)
        assert response.row.timestamp_iso == expected_ts.isoformat()
        (value,) = json.loads(response.row.values_json)
        assert value == 3000 + i


def test_read_load_profile_unknown_profile_yields_error():
    grpc_port = _free_port()
    server, _ = grpc_server.serve(host="127.0.0.1", port=grpc_port, call_home_port=None)
    try:
        channel = grpc.insecure_channel(f"127.0.0.1:{grpc_port}")
        stub = gateway_pb2_grpc.GatewayServiceStub(channel)
        request = gateway_pb2.ReadLoadProfileRequest(
            profile="mode_c",  # профиль нагрузки реализован только для hdlc_dlms
            host="127.0.0.1",
            port=1,
            serial=SERIAL,
            password=PASSWORD,
            obis=LOAD_PROFILE_OBIS,
            from_iso="2026-08-01T00:00:00",
            to_iso="2026-08-19T00:00:00",
        )
        responses = list(stub.ReadLoadProfile(request))
    finally:
        server.stop(None)

    assert len(responses) == 1
    assert responses[0].WhichOneof("result") == "error"
    assert responses[0].error.code == "GATEWAY_ERROR"
