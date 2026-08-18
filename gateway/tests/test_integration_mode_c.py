"""Интеграционные тесты профиля IEC 62056-21 режим C против программного эмулятора.

ВНИМАНИЕ: эмулятор — программная имитация счётчика для целей теста, а не
замена реальным полевым испытаниям (см. README.md, Promt_MMWS.md Этап 0, п.4).
"""

import pytest

from mmws_gateway.emulators.common import ConnectionCounter, ErrorInjection, ThreadedEmulatorServer
from mmws_gateway.emulators.mode_c_emulator import make_mode_c_handler
from mmws_gateway.errors import AuthFailedError, ConnectionLostError, MeterTimeoutError
from mmws_gateway.protocols import mode_c
from mmws_gateway.session import run_with_retries
from mmws_gateway.transport import TcpTransport, TransportConfig

SERIAL = "202006003607"
READINGS = {"1.8.0": "001234.567", "32.7.0": "230.1"}


def _start_server(error_injection: ErrorInjection) -> ThreadedEmulatorServer:
    counter = ConnectionCounter()
    handler = make_mode_c_handler(
        serial=SERIAL, readings=READINGS, error_injection=error_injection, counter=counter
    )
    return ThreadedEmulatorServer(handler)


def _read(server: ThreadedEmulatorServer, *, timeout_ms: int = 1000, retries: int = 1) -> float:
    config = TransportConfig(host=server.host, port=server.port, timeout_ms=timeout_ms, max_retries=1)

    def operation():
        with TcpTransport(config) as transport:
            return mode_c.read_value(transport, serial=SERIAL, obis_5="1.8.0")

    return run_with_retries(operation, max_retries=retries)


def test_happy_path_reads_value():
    with _start_server(ErrorInjection()) as server:
        value = _read(server)
    assert value == pytest.approx(1234.567)


def test_timeout_scenario():
    with _start_server(ErrorInjection(force_timeout=True)) as server:
        with pytest.raises(MeterTimeoutError):
            _read(server, timeout_ms=100, retries=1)


def test_auth_failed_scenario_not_retried():
    with _start_server(ErrorInjection(force_auth_fail=True)) as server:
        with pytest.raises(AuthFailedError):
            _read(server, retries=3)


def test_partial_disconnect_scenario_marks_is_partial():
    with _start_server(ErrorInjection(force_partial_disconnect=True, fail_attempts=99)) as server:
        with pytest.raises(ConnectionLostError) as exc_info:
            _read(server, retries=1)
    assert exc_info.value.is_partial is True


def test_crc_error_recovers_after_retry():
    with _start_server(ErrorInjection(force_crc_error=True, fail_attempts=1)) as server:
        value = _read(server, retries=3)
    assert value == pytest.approx(1234.567)
