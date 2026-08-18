"""Интеграционные тесты профиля IEC 62056-21 режим E против программного эмулятора.

ВНИМАНИЕ: эмулятор — программная имитация счётчика для целей теста, а не
замена реальным полевым испытаниям (см. README.md, Promt_MMWS.md Этап 0, п.4).
"""

import pytest

from mmws_gateway.emulators.common import ConnectionCounter, ErrorInjection, ThreadedEmulatorServer
from mmws_gateway.emulators.mode_e_emulator import make_mode_e_handler
from mmws_gateway.errors import AuthFailedError, ConnectionLostError, MeterTimeoutError
from mmws_gateway.protocols import dlms, mode_e
from mmws_gateway.session import run_with_retries
from mmws_gateway.transport import TcpTransport, TransportConfig

SERIAL = "202006003607"
PASSWORD = b"12345678"
OBIS = "1.1.1.8.0.ff"
OBIS_VALUES = {dlms.parse_obis(OBIS): 1234567}


def _start_server(error_injection: ErrorInjection) -> ThreadedEmulatorServer:
    counter = ConnectionCounter()
    handler = make_mode_e_handler(
        serial=SERIAL,
        password=PASSWORD,
        obis_values=OBIS_VALUES,
        error_injection=error_injection,
        counter=counter,
    )
    return ThreadedEmulatorServer(handler)


def _read(
    server: ThreadedEmulatorServer,
    *,
    password: bytes = PASSWORD,
    timeout_ms: int = 1000,
    retries: int = 1,
):
    config = TransportConfig(host=server.host, port=server.port, timeout_ms=timeout_ms, max_retries=1)

    def operation():
        with TcpTransport(config) as transport:
            return mode_e.read_register(transport, serial=SERIAL, password=password, obis=OBIS)

    return run_with_retries(operation, max_retries=retries)


def test_happy_path_reads_value():
    with _start_server(ErrorInjection()) as server:
        value = _read(server)
    assert value == 1234567


def test_wrong_password_raises_auth_failed():
    with _start_server(ErrorInjection()) as server:
        with pytest.raises(AuthFailedError):
            _read(server, password=b"WRONGPASS", retries=3)


def test_timeout_scenario():
    with _start_server(ErrorInjection(force_timeout=True)) as server:
        with pytest.raises(MeterTimeoutError):
            _read(server, timeout_ms=100, retries=1)


def test_partial_disconnect_scenario_marks_is_partial():
    with _start_server(ErrorInjection(force_partial_disconnect=True, fail_attempts=99)) as server:
        with pytest.raises(ConnectionLostError) as exc_info:
            _read(server, retries=1)
    assert exc_info.value.is_partial is True


def test_crc_error_recovers_after_retry():
    with _start_server(ErrorInjection(force_crc_error=True, fail_attempts=1)) as server:
        value = _read(server, retries=3)
    assert value == 1234567
