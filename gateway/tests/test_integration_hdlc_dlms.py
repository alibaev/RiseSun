"""Интеграционные тесты профиля HDLC + DLMS/COSEM против программного эмулятора.

ВНИМАНИЕ: эмулятор — программная имитация счётчика для целей теста, а не
замена реальным полевым испытаниям (см. README.md, Promt_MMWS.md Этап 0, п.4).
"""

import socket
import threading

import pytest

from mmws_gateway.emulators.common import ConnectionCounter, ErrorInjection, ThreadedEmulatorServer
from mmws_gateway.emulators.hdlc_dlms_emulator import make_hdlc_dlms_handler
from mmws_gateway.errors import AuthFailedError, ConnectionLostError, GatewayError, MeterTimeoutError
from mmws_gateway.protocols import datatypes, dlms, hdlc_dlms
from mmws_gateway.protocols.hdlc import (
    CONTROL_DISC,
    CONTROL_UA,
    HdlcFrame,
    control_information_frame,
    read_frame_from_transport,
)
from mmws_gateway.session import run_with_retries
from mmws_gateway.transport import TcpTransport, TransportConfig

SERIAL = "202006003607"
PASSWORD = b"12345678"
# Родовой OBIS для тестов, не завязанных на конкретную семантику
# показания — намеренно НЕ "1.1.1.8.0.ff" (суммарная активная энергия),
# у которого с 2026-08-20 есть вендорский override value-OBIS
# (dlms.VALUE_OBIS_OVERRIDES) — см. test_register_read_applies_scaler
# ниже, где это специально проверяется.
OBIS = "1.1.1.7.0.ff"
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
    obis: str = OBIS,
):
    config = TransportConfig(host=server.host, port=server.port, timeout_ms=timeout_ms, max_retries=1)

    def operation():
        with TcpTransport(config) as transport:
            return hdlc_dlms.read_register(
                transport, serial=SERIAL, password=password, obis=obis
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
    generic_obis = "1.1.1.9.0.ff"
    counter = ConnectionCounter()
    handler = make_hdlc_dlms_handler(
        password=PASSWORD,
        obis_values={dlms.parse_obis(generic_obis): 450770},
        error_injection=ErrorInjection(),
        counter=counter,
        register_scalers={dlms.parse_obis(generic_obis): -2},
    )
    with ThreadedEmulatorServer(handler) as server:
        value = _read(server, obis=generic_obis)
    assert value == 4507.7


def test_register_read_applies_value_obis_override_for_risesun_energy():
    """Повторное появление того же бага на живом счётчике 202306004113
    (2026-08-20): для суммарной активной энергии (`1.1.1.8.0.ff`)
    value и scaler_unit одного и того же физического регистра на самом
    деле лежат по РАЗНЫМ OBIS (dlms.VALUE_OBIS_OVERRIDES, подтверждено
    реальным трафиком легитимного приложения, см. DECISIONS.md) — value
    по вендорскому `1.1.60.50.0.ff`, scaler_unit по стандартному
    `1.1.1.8.0.ff`. Эмулятор должен получить два разных GET на разные
    OBIS и результат должен быть верно масштабирован."""
    energy_obis = "1.1.1.8.0.ff"
    vendor_value_obis = "1.1.60.50.0.ff"
    counter = ConnectionCounter()
    handler = make_hdlc_dlms_handler(
        password=PASSWORD,
        obis_values={dlms.parse_obis(vendor_value_obis): 450770},
        error_injection=ErrorInjection(),
        counter=counter,
        register_scalers={dlms.parse_obis(energy_obis): -2},
    )
    with ThreadedEmulatorServer(handler) as server:
        value = _read(server, obis=energy_obis)
    assert value == 4507.7


class _FakeTransport:
    """Минимальный фейковый транспорт (без сокетов) — очередь заранее
    закодированных HDLC-кадров на recv, накопление отправленного на
    send. Нужен только 3-методный интерфейс, который использует
    ``_read_one_register_via_established_link``."""

    def __init__(self, frames_to_recv: list[bytes]) -> None:
        self._buf = b"".join(frames_to_recv)
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def reset_frame_seeking(self) -> None:
        pass

    def recv_exact(self, n: int) -> bytes:
        assert len(self._buf) >= n, "фейковый транспорт исчерпан раньше, чем ожидалось"
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk


def _hdlc_response_frame(information: bytes, *, send_seq: int = 1, recv_seq: int = 2) -> bytes:
    return HdlcFrame(
        destination=0x30, source=0x01, control=control_information_frame(send_seq, recv_seq),
        information=information,
    ).encode()


def test_register_read_uses_distinct_invoke_id_for_scaler_and_value():
    """2026-09-11, найдено на живом трафике (см. DECISIONS.md — "поймать
    байты одного отказа"): раньше scaler_unit и value внутри ОДНОЙ
    ассоциации всегда уходили с одинаковым invoke_id=1 — рабочая
    гипотеза в том, что часть прошивок (преимущественно новой партии
    счётчиков) путает повторный invoke_id с ретрансляцией уже
    обработанного запроса. Теперь второй GET должен иметь другой
    invoke_id."""
    scaler_info = dlms.wrap_llc_response(
        dlms.build_get_response_data(
            1, datatypes.encode_structure([datatypes.encode_integer(0), datatypes.encode_unsigned(30)])
        )
    )
    value_info = dlms.wrap_llc_response(
        dlms.build_get_response_data(1, datatypes.encode_double_long_unsigned(450770))
    )
    transport = _FakeTransport([_hdlc_response_frame(scaler_info), _hdlc_response_frame(value_info)])

    hdlc_dlms._read_one_register_via_established_link(
        transport, server_addr=0x01, client_addr=0x30,
        send_seq=1, obis="1.1.1.8.0.ff", class_id=dlms.REGISTER_CLASS_ID,
    )

    assert len(transport.sent) == 2
    invoke_ids = []
    for raw_frame in transport.sent:
        frame = HdlcFrame.decode(raw_frame)
        payload = dlms.unwrap_llc(frame.information)
        invoke_ids.append(payload[2])
    assert invoke_ids[0] != invoke_ids[1]


def test_register_read_fails_instead_of_returning_unscaled_raw_value_on_malformed_scaler():
    """Найденный баг на живых данных (2026-09-11, см. DECISIONS.md):
    счётчик 201901230052 получил в MeterReading значение "-1003" (без
    дробной части — прямой признак непроскейленного сырого значения)
    при исправном предыдущем показании 3271.18 — суммарная активная
    энергия физически не может уменьшаться. Причина — когда GET
    scaler_unit возвращал не ожидаемую структуру {scaler, unit}, а
    что-то другое (здесь: одиночное целое, не список), код тихо
    возвращал СЫРОЕ немасштабированное значение как будто оно валидное.
    Теперь это должно быть отказом чтения (GatewayError), а не мнимым
    успехом с недостоверным числом."""
    malformed_scaler_info = dlms.wrap_llc_response(
        dlms.build_get_response_data(1, datatypes.encode_integer(5))  # НЕ структура {scaler, unit}
    )
    value_info = dlms.wrap_llc_response(
        dlms.build_get_response_data(1, datatypes.encode_double_long_unsigned(450770))
    )
    transport = _FakeTransport(
        [_hdlc_response_frame(malformed_scaler_info), _hdlc_response_frame(value_info)]
    )

    value, next_send_seq, error = hdlc_dlms._read_one_register_via_established_link(
        transport, server_addr=0x01, client_addr=0x30,
        send_seq=1, obis="1.1.1.8.0.ff", class_id=dlms.REGISTER_CLASS_ID,
    )

    assert value is None
    assert error is not None
    assert isinstance(error, GatewayError)
    assert next_send_seq == 3  # нумерация кадров продвинулась штатно несмотря на отказ


def test_register_read_fails_instead_of_accepting_null_data_value():
    """2026-09-11 — найдено на новой партии счётчиков (масштабная
    деградация read_current: ~87% новой партии). Одна из причин: GET.
    response-Normal иногда приходит валидным (CRC/HCS сошлись), с
    выбором "data", но само значение — null-data (0x00), для которого
    раньше в decode_value() не было ветки разбора вообще (падало
    "Неподдержанный тег..."). Теперь decode_value() разбирает null-data
    как None, но для показания энергии None так же недостоверен, как
    немасштабированное сырое число (см. тест выше про scaler) —
    ожидается отказ чтения, а не показание со значением None."""
    scaler_info = dlms.wrap_llc_response(
        dlms.build_get_response_data(
            1, datatypes.encode_structure([datatypes.encode_integer(0), datatypes.encode_unsigned(30)])
        )
    )
    null_value_info = dlms.wrap_llc_response(dlms.build_get_response_data(1, datatypes.encode_null()))
    transport = _FakeTransport(
        [_hdlc_response_frame(scaler_info), _hdlc_response_frame(null_value_info)]
    )

    value, next_send_seq, error = hdlc_dlms._read_one_register_via_established_link(
        transport, server_addr=0x01, client_addr=0x30,
        send_seq=1, obis="1.1.1.8.0.ff", class_id=dlms.REGISTER_CLASS_ID,
    )

    assert value is None
    assert error is not None
    assert isinstance(error, GatewayError)
    assert next_send_seq == 3


def test_register_read_applies_watt_hour_to_kwh_conversion():
    """Найденный баг (2026-09-07, сообщение пользователя): показания
    приходили без дробного разделителя и в 1000 раз больше нужного —
    напр. счётчик 202308004436 живьём отдал scaler=1, сырое значение
    1005950, и код (без этого фикса) вернул бы 10059500 вместо верных
    10059.5. Причина — формула применяла только scaler, не учитывая, что
    unit=30 (Wh, Green Book) требует ещё /1000 для отображения в кВт·ч
    (везде в проекте, см. OBIS.xlsx). Числа здесь — из подтверждённого
    реального захвата (test_real_capture_replay.py,
    test_decode_real_value_response_and_apply_scaler): raw=66619,
    scaler=1, unit=30 -> 666.19 кВт·ч."""
    energy_obis = "1.1.1.8.0.ff"
    vendor_value_obis = "1.1.60.50.0.ff"
    counter = ConnectionCounter()
    handler = make_hdlc_dlms_handler(
        password=PASSWORD,
        obis_values={dlms.parse_obis(vendor_value_obis): 66619},
        error_injection=ErrorInjection(),
        counter=counter,
        register_scalers={dlms.parse_obis(energy_obis): 1},
        register_units={dlms.parse_obis(energy_obis): 30},
    )
    with ThreadedEmulatorServer(handler) as server:
        value = _read(server, obis=energy_obis)
    assert value == 666.19


def test_register_read_applies_watt_hour_conversion_even_with_zero_scaler():
    """Тот же баг, частный случай scaler=0: счётчик 202302003956 живьём
    отдавал показание 6101020 вместо верных 6101.02 — при scaler=0
    старая формула не делала вообще ничего (ранний return), хотя
    unit=30 (Wh) всё равно требует перевода в кВт·ч."""
    energy_obis = "1.1.1.8.0.ff"
    vendor_value_obis = "1.1.60.50.0.ff"
    counter = ConnectionCounter()
    handler = make_hdlc_dlms_handler(
        password=PASSWORD,
        obis_values={dlms.parse_obis(vendor_value_obis): 6101020},
        error_injection=ErrorInjection(),
        counter=counter,
        register_scalers={dlms.parse_obis(energy_obis): 0},
        register_units={dlms.parse_obis(energy_obis): 30},
    )
    with ThreadedEmulatorServer(handler) as server:
        value = _read(server, obis=energy_obis)
    assert value == 6101.02


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

# --- read_registers_via_established_link (2026-09-09, событийное
# чтение call-home сразу при подключении — см. DECISIONS.md и план
# ticklish-popping-bear.md): читает НЕСКОЛЬКО регистров за одну
# ассоциацию вместо одной на каждый OBIS. Общий emulators/
# hdlc_dlms_emulator рассчитан на ОДИН регистр за сессию (закрывает её
# сразу после первого GET), поэтому здесь — свой лёгкий "счётчик",
# обслуживающий произвольное число регистров подряд на одной
# ассоциации, с контролем момента обрыва (для теста частичного успеха).

OBIS_2 = "1.1.32.7.0.ff"
_OBJECT_UNDEFINED = 9  # data-access-result: object-undefined (Green Book), см. hdlc_dlms_emulator.py


class _ConnAdapter:
    """Минимальная обёртка socket -> интерфейс, ожидаемый
    ``read_frame_from_transport`` (``recv_exact`` + ``reset_frame_seeking``),
    для серверной стороны в тестах ниже — полноценный (клиентский,
    с ретраями) ``TcpTransport`` тут не нужен, только чтение кадра по
    длине на "сыром" сокете."""

    def __init__(self, sock):
        self._sock = sock

    def recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("EOF")
            buf += chunk
        return buf

    def reset_frame_seeking(self) -> None:
        pass


def _serve_batch_session(
    conn, *, obis_values: dict, disconnect_after_registers: int | None = None,
    attribute_ids_seen: list | None = None,
) -> None:
    """Обслуживает SNRM/UA + AARQ/AARE, затем произвольное число
    GET-пар (scaler_unit, отвечает scaler=0; value — по ``obis_values``,
    OBJECT_UNDEFINED если OBIS не найден) подряд на одной ассоциации, в
    том порядке, в каком их фактически запрашивает клиент. Если
    ``disconnect_after_registers`` задан — закрывает соединение сразу
    после этого числа полностью обслуженных регистров, не дожидаясь
    следующего запроса (имитация обрыва посреди батча). ``attribute_ids_seen``
    (2026-09-11) — если передан список, в него добавляется attribute_id
    каждого полученного GET-запроса (используется тестом на class_id=0,
    чтобы убедиться, что scaler_unit реально запрашивается)."""
    adapter = _ConnAdapter(conn)
    snrm_frame = HdlcFrame.decode(read_frame_from_transport(adapter))
    ua = HdlcFrame(destination=snrm_frame.source, source=snrm_frame.destination, control=CONTROL_UA)
    conn.sendall(ua.encode())

    aarq_frame = HdlcFrame.decode(read_frame_from_transport(adapter))
    parsed_aarq = dlms.parse_aarq(dlms.unwrap_llc(aarq_frame.information))
    accepted = parsed_aarq.password == PASSWORD
    aare = dlms.build_aare(accepted=accepted)
    aare_frame = HdlcFrame(
        destination=aarq_frame.source, source=aarq_frame.destination,
        control=control_information_frame(0, 1), information=dlms.wrap_llc_response(aare),
    )
    conn.sendall(aare_frame.encode())
    if not accepted:
        return

    send_seq, recv_seq = 1, 2
    served = 0
    while True:
        try:
            frame = HdlcFrame.decode(read_frame_from_transport(adapter))
        except (ConnectionError, OSError):
            return
        get_request = dlms.parse_get_request(dlms.unwrap_llc(frame.information))
        if attribute_ids_seen is not None:
            attribute_ids_seen.append(get_request.attribute_id)
        if get_request.attribute_id == dlms.REGISTER_SCALER_UNIT_ATTRIBUTE:
            payload = datatypes.encode_structure([datatypes.encode_integer(0), datatypes.encode_unsigned(0)])
            info = dlms.build_get_response_data(get_request.invoke_id, payload)
        else:
            value = obis_values.get(get_request.obis)
            info = (
                dlms.build_get_response_data(get_request.invoke_id, datatypes.encode_double_long_unsigned(value))
                if value is not None
                else dlms.build_get_response_error(get_request.invoke_id, _OBJECT_UNDEFINED)
            )
        response_frame = HdlcFrame(
            destination=frame.source, source=frame.destination,
            control=control_information_frame(send_seq, recv_seq), information=dlms.wrap_llc_response(info),
        )
        conn.sendall(response_frame.encode())
        send_seq += 1
        recv_seq += 1

        if get_request.attribute_id != dlms.REGISTER_SCALER_UNIT_ATTRIBUTE:
            served += 1
            if disconnect_after_registers is not None and served >= disconnect_after_registers:
                return


def _run_batch_server(
    *, obis_values: dict, disconnect_after_registers: int | None = None, attribute_ids_seen: list | None = None,
):
    """Запускает ``_serve_batch_session`` на localhost в фоновом потоке,
    возвращает ``(host, port, thread, server_sock)`` — вызывающий
    отвечает за ``server_sock.close()``/``thread.join()`` по завершении
    (см. использование ниже, тот же паттерн, что и у
    ``ThreadedEmulatorServer``, но без его one-register-per-session
    ограничения)."""
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    host, port = server_sock.getsockname()

    def _accept_and_serve():
        conn, _ = server_sock.accept()
        try:
            _serve_batch_session(
                conn, obis_values=obis_values, disconnect_after_registers=disconnect_after_registers,
                attribute_ids_seen=attribute_ids_seen,
            )
        finally:
            conn.close()

    thread = threading.Thread(target=_accept_and_serve, daemon=True)
    thread.start()
    return host, port, thread, server_sock


def _read_batch(host: str, port: int, *, password: bytes = PASSWORD, obis_specs, timeout_ms: int = 1000):
    config = TransportConfig(host=host, port=port, timeout_ms=timeout_ms, max_retries=1)
    with TcpTransport(config) as transport:
        hdlc_dlms.establish_link(transport, serial=SERIAL)
        return hdlc_dlms.read_registers_via_established_link(
            transport, serial=SERIAL, password=password, obis_specs=obis_specs
        )


def test_read_registers_happy_path_multiple_obis():
    obis_values = {dlms.parse_obis(OBIS): 1234567, dlms.parse_obis(OBIS_2): 2200000}
    host, port, thread, server_sock = _run_batch_server(obis_values=obis_values)
    try:
        outcomes = _read_batch(
            host, port, obis_specs=[(OBIS, dlms.REGISTER_CLASS_ID), (OBIS_2, dlms.REGISTER_CLASS_ID)]
        )
    finally:
        thread.join(timeout=3)
        server_sock.close()

    assert [o.obis for o in outcomes] == [OBIS, OBIS_2]
    assert all(o.ok for o in outcomes)
    assert outcomes[0].value == 1234567
    assert outcomes[1].value == 2200000


def test_read_registers_normalizes_class_id_zero_to_register():
    """2026-09-11, найдено на живом трафике (92% свежих показаний —
    null): DueJobOut/payload передаёт class_id=0 как "используй Register
    по умолчанию" (тот же смысл, что и в старом gRPC-пути,
    grpc_server.py: ``class_id=request.class_id or dlms.REGISTER_CLASS_ID``),
    но read_registers_via_established_link раньше использовал 0 как есть
    — 0 != dlms.REGISTER_CLASS_ID(3), поэтому scaler_unit НИ РАЗУ не
    запрашивался для обычных read_current job'ов (только они и приходят
    с class_id=0 по умолчанию) на событийном пути. Теперь 0 должен
    нормализоваться в REGISTER_CLASS_ID точно как на старом пути —
    scaler_unit обязан быть запрошен."""
    obis_values = {dlms.parse_obis(OBIS): 1234567}
    attribute_ids_seen: list = []
    host, port, thread, server_sock = _run_batch_server(
        obis_values=obis_values, attribute_ids_seen=attribute_ids_seen,
    )
    try:
        outcomes = _read_batch(host, port, obis_specs=[(OBIS, 0)])
    finally:
        thread.join(timeout=3)
        server_sock.close()

    assert outcomes[0].ok is True
    assert dlms.REGISTER_SCALER_UNIT_ATTRIBUTE in attribute_ids_seen


def test_read_registers_data_access_error_does_not_sink_rest_of_batch():
    """GatewayError (data-access-error — объект неизвестен "счётчику",
    не входит в obis_values) на ОДНОМ OBIS не должна прерывать батч —
    HDLC-нумерация кадров (N(S)/N(R)) должна остаться синхронной,
    следующий OBIS на той же ассоциации обязан прочитаться штатно."""
    unknown_obis = "1.1.99.99.0.ff"
    obis_values = {dlms.parse_obis(OBIS): 1234567}
    host, port, thread, server_sock = _run_batch_server(obis_values=obis_values)
    try:
        outcomes = _read_batch(
            host, port, obis_specs=[(unknown_obis, dlms.REGISTER_CLASS_ID), (OBIS, dlms.REGISTER_CLASS_ID)]
        )
    finally:
        thread.join(timeout=3)
        server_sock.close()

    assert outcomes[0].obis == unknown_obis
    assert outcomes[0].ok is False
    assert isinstance(outcomes[0].error, GatewayError)
    assert outcomes[1].obis == OBIS
    assert outcomes[1].ok is True
    assert outcomes[1].value == 1234567


def test_read_registers_wrong_password_raises_before_any_get():
    obis_values = {dlms.parse_obis(OBIS): 1234567, dlms.parse_obis(OBIS_2): 2200000}
    host, port, thread, server_sock = _run_batch_server(obis_values=obis_values)
    try:
        with pytest.raises(AuthFailedError):
            _read_batch(
                host, port, password=b"WRONGPASS",
                obis_specs=[(OBIS, dlms.REGISTER_CLASS_ID), (OBIS_2, dlms.REGISTER_CLASS_ID)],
            )
    finally:
        thread.join(timeout=3)
        server_sock.close()


def test_read_registers_connection_drop_mid_batch_returns_partial_results():
    """Обрыв соединения ПОСЛЕ первого успешно прочитанного регистра, но
    до второго — не должен терять уже собранный результат первого."""
    obis_values = {dlms.parse_obis(OBIS): 1234567, dlms.parse_obis(OBIS_2): 2200000}
    host, port, thread, server_sock = _run_batch_server(obis_values=obis_values, disconnect_after_registers=1)
    try:
        outcomes = _read_batch(
            host, port, obis_specs=[(OBIS, dlms.REGISTER_CLASS_ID), (OBIS_2, dlms.REGISTER_CLASS_ID)]
        )
    finally:
        thread.join(timeout=3)
        server_sock.close()

    assert len(outcomes) == 1
    assert outcomes[0].obis == OBIS
    assert outcomes[0].ok is True
    assert outcomes[0].value == 1234567


def test_aarq_is_resent_when_aare_delayed():
    """2026-09-10 (см. DECISIONS.md — ответ производителя на разбор
    AARE-тишины): AARE может не прийти на первый AARQ (помехи от
    heartbeat-кадров 3G/4G-модема); штатная рекомендация производителя —
    переотправить AARQ и снова ждать, а не просто ждать дольше на той же
    попытке. Сервер здесь намеренно молчит на первый AARQ и отвечает
    AARE только на второй (повторно присланный клиентом) — проверяем,
    что клиент это делает сам, без явного участия вызывающего кода."""
    obis_values = {dlms.parse_obis(OBIS): 1234567}
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    host, port = server_sock.getsockname()
    aarq_count = {"n": 0}

    def _serve():
        conn, _ = server_sock.accept()
        try:
            adapter = _ConnAdapter(conn)
            snrm_frame = HdlcFrame.decode(read_frame_from_transport(adapter))
            ua = HdlcFrame(destination=snrm_frame.source, source=snrm_frame.destination, control=CONTROL_UA)
            conn.sendall(ua.encode())

            # Первый AARQ — молча игнорируем (имитация помехи модема).
            HdlcFrame.decode(read_frame_from_transport(adapter))
            aarq_count["n"] += 1

            # Перед переотправкой клиент шлёт DISC (см. DECISIONS.md,
            # 2026-09-10) — считываем и отбрасываем, это не AARQ.
            disc_frame = HdlcFrame.decode(read_frame_from_transport(adapter))
            assert disc_frame.control == CONTROL_DISC

            # Второй AARQ — это и есть переотправка клиентом, отвечаем как обычно.
            aarq_frame = HdlcFrame.decode(read_frame_from_transport(adapter))
            aarq_count["n"] += 1
            parsed_aarq = dlms.parse_aarq(dlms.unwrap_llc(aarq_frame.information))
            aare = dlms.build_aare(accepted=parsed_aarq.password == PASSWORD)
            aare_frame = HdlcFrame(
                destination=aarq_frame.source, source=aarq_frame.destination,
                control=control_information_frame(0, 1), information=dlms.wrap_llc_response(aare),
            )
            conn.sendall(aare_frame.encode())

            frame = HdlcFrame.decode(read_frame_from_transport(adapter))
            get_request = dlms.parse_get_request(dlms.unwrap_llc(frame.information))
            payload = datatypes.encode_structure([datatypes.encode_integer(0), datatypes.encode_unsigned(0)])
            info = dlms.build_get_response_data(get_request.invoke_id, payload)
            response_frame = HdlcFrame(
                destination=frame.source, source=frame.destination,
                control=control_information_frame(1, 2), information=dlms.wrap_llc_response(info),
            )
            conn.sendall(response_frame.encode())

            frame = HdlcFrame.decode(read_frame_from_transport(adapter))
            get_request = dlms.parse_get_request(dlms.unwrap_llc(frame.information))
            value = obis_values[get_request.obis]
            info = dlms.build_get_response_data(get_request.invoke_id, datatypes.encode_double_long_unsigned(value))
            response_frame = HdlcFrame(
                destination=frame.source, source=frame.destination,
                control=control_information_frame(2, 3), information=dlms.wrap_llc_response(info),
            )
            conn.sendall(response_frame.encode())
        finally:
            conn.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        # Короткий таймаут на попытку — сервер сознательно молчит после
        # первого AARQ, клиент должен переотправить, а не просто зависнуть
        # до общего дедлайна.
        outcomes = _read_batch(
            host, port, obis_specs=[(OBIS, dlms.REGISTER_CLASS_ID)], timeout_ms=300,
        )
    finally:
        thread.join(timeout=3)
        server_sock.close()

    assert aarq_count["n"] == 2
    assert outcomes[0].ok is True
    assert outcomes[0].value == 1234567
