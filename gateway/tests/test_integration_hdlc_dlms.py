"""Интеграционные тесты профиля HDLC + DLMS/COSEM против программного эмулятора.

ВНИМАНИЕ: эмулятор — программная имитация счётчика для целей теста, а не
замена реальным полевым испытаниям (см. README.md, Promt_MMWS.md Этап 0, п.4).
"""

import pytest

from mmws_gateway.emulators.common import ConnectionCounter, ErrorInjection, ThreadedEmulatorServer
from mmws_gateway.emulators.hdlc_dlms_emulator import make_hdlc_dlms_handler
from mmws_gateway.errors import AuthFailedError, ConnectionLostError, GatewayError, MeterTimeoutError
from mmws_gateway.protocols import datatypes, dlms, hdlc_dlms
from mmws_gateway.session import run_with_retries
from mmws_gateway.transport import TcpTransport, TransportConfig

SERIAL = "202006003607"
PASSWORD = b"12345678"
OBIS = "1.1.1.8.0.ff"
OBIS_VALUES = {dlms.parse_obis(OBIS): 1234567}


def _start_server(error_injection: ErrorInjection) -> ThreadedEmulatorServer:
    counter = ConnectionCounter()
    handler = make_hdlc_dlms_handler(
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
            return hdlc_dlms.read_register(
                transport, serial=SERIAL, password=password, obis=OBIS
            )

    return run_with_retries(operation, max_retries=retries)


def test_happy_path_reads_value():
    with _start_server(ErrorInjection()) as server:
        value = _read(server)
    assert value == 1234567


def test_register_read_applies_scaler():
    """Найденный баг (2026-08-19, сообщение пользователя): показание
    4507.70 отображалось в MMWS как 450770 — атрибут 3 (scaler_unit)
    читался счётчиком реального трафика, но в коде не применялся. Сырое
    значение 450770 при scaler=-2 (0.01) должно превращаться в 4507.7."""
    counter = ConnectionCounter()
    handler = make_hdlc_dlms_handler(
        password=PASSWORD,
        obis_values={dlms.parse_obis(OBIS): 450770},
        error_injection=ErrorInjection(),
        counter=counter,
        register_scalers={dlms.parse_obis(OBIS): -2},
    )
    with ThreadedEmulatorServer(handler) as server:
        value = _read(server)
    assert value == 4507.7


def test_register_read_without_scaler_stays_integer():
    with _start_server(ErrorInjection()) as server:
        value = _read(server)
    assert value == 1234567
    assert isinstance(value, int)


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


def test_write_register_then_read_back_value():
    """Этап 2 (ТЗ п.4.2.4): SET параметра class-1 (Data), затем GET того
    же OBIS отдельной сессией — подтверждает, что записанное значение
    действительно сохранилось на «счётчике» (эмуляторе)."""
    obis = "1.0.0.9.1.ff"
    encoded_value = datatypes.encode_octet_string(bytes([12, 30, 0]))  # 12:30:00
    counter = ConnectionCounter()
    data_values: dict[bytes, bytes] = {}
    handler = make_hdlc_dlms_handler(
        password=PASSWORD, obis_values=OBIS_VALUES, error_injection=ErrorInjection(),
        counter=counter, data_values=data_values,
    )
    with ThreadedEmulatorServer(handler) as server:
        config = TransportConfig(host=server.host, port=server.port, timeout_ms=1000, max_retries=1)

        with TcpTransport(config) as transport:
            hdlc_dlms.write_register(
                transport, serial=SERIAL, password=PASSWORD, obis=obis,
                encoded_value=encoded_value, class_id=1,
            )

        with TcpTransport(config) as transport:
            value = hdlc_dlms.read_register(transport, serial=SERIAL, password=PASSWORD, obis=obis, class_id=1)

    assert value == bytes([12, 30, 0])


def test_execute_action_disconnect_round_trip():
    """Этап 5 (ТЗ п.4.2.10): ACTION.request remote_disconnect на объект
    Disconnect Control (класс 70) поверх эмулятора — эмулятор
    записывает вызванный method_id в ``action_state``."""
    counter = ConnectionCounter()
    action_state: dict[bytes, int] = {}
    handler = make_hdlc_dlms_handler(
        password=PASSWORD, obis_values=OBIS_VALUES, error_injection=ErrorInjection(),
        counter=counter, action_state=action_state,
    )
    with ThreadedEmulatorServer(handler) as server:
        config = TransportConfig(host=server.host, port=server.port, timeout_ms=1000, max_retries=1)
        with TcpTransport(config) as transport:
            hdlc_dlms.execute_action(
                transport, serial=SERIAL, password=PASSWORD, obis=dlms.DISCONNECT_CONTROL_OBIS,
                method_id=dlms.METHOD_REMOTE_DISCONNECT, class_id=dlms.DISCONNECT_CONTROL_CLASS_ID,
            )

    assert action_state[dlms.parse_obis(dlms.DISCONNECT_CONTROL_OBIS)] == dlms.METHOD_REMOTE_DISCONNECT


def test_execute_action_failure_raises_gateway_error():
    counter = ConnectionCounter()
    handler = make_hdlc_dlms_handler(
        password=PASSWORD, obis_values=OBIS_VALUES, error_injection=ErrorInjection(),
        counter=counter, action_force_result=3,  # read-write-denied
    )
    with ThreadedEmulatorServer(handler) as server:
        config = TransportConfig(host=server.host, port=server.port, timeout_ms=1000, max_retries=1)
        with TcpTransport(config) as transport:
            with pytest.raises(GatewayError):
                hdlc_dlms.execute_action(
                    transport, serial=SERIAL, password=PASSWORD, obis=dlms.DISCONNECT_CONTROL_OBIS,
                    method_id=dlms.METHOD_REMOTE_RECONNECT, class_id=dlms.DISCONNECT_CONTROL_CLASS_ID,
                )


def test_write_register_wrong_password_raises_auth_failed():
    obis = "1.0.0.9.1.ff"
    encoded_value = datatypes.encode_octet_string(bytes([12, 30, 0]))
    with _start_server(ErrorInjection()) as server:
        config = TransportConfig(host=server.host, port=server.port, timeout_ms=1000, max_retries=1)
        with pytest.raises(AuthFailedError):
            with TcpTransport(config) as transport:
                hdlc_dlms.write_register(
                    transport, serial=SERIAL, password=b"WRONGPASS", obis=obis,
                    encoded_value=encoded_value, class_id=1,
                )


def test_read_survives_serial_whose_frame_contains_embedded_flag_byte():
    """Регрессия, найденная на живой проверке 2026-08-18: для серийного
    номера 999000111222 второй байт HCS GET.request-кадра случайно
    совпадает с 0x7E — раньше это обрезало кадр и на клиенте, и на
    сервере (эмулятор использует тот же разбор кадра). Проверяем именно
    этот серийный номер целиком через эмулятор, а не только сборку
    кадра изолированно (test_hdlc.py)."""
    serial = "999000111222"
    obis_values = {dlms.parse_obis(OBIS): 1234567}
    counter = ConnectionCounter()
    handler = make_hdlc_dlms_handler(
        password=PASSWORD, obis_values=obis_values, error_injection=ErrorInjection(), counter=counter
    )
    with ThreadedEmulatorServer(handler) as server:
        config = TransportConfig(host=server.host, port=server.port, timeout_ms=1000, max_retries=1)
        with TcpTransport(config) as transport:
            value = hdlc_dlms.read_register(transport, serial=serial, password=PASSWORD, obis=OBIS)
    assert value == 1234567
