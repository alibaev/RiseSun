"""Протокольный профиль «HDLC + DLMS/COSEM» (ТЗ п. 4.3.2, третий профиль).

Последовательность одной операции чтения:
    1. SNRM -> UA (установление HDLC-соединения);
    2. AARQ -> AARE в I-кадрах (установление ассоциации DLMS, пароль
       низкого уровня безопасности — ТЗ п. 4.3.3);
    3. GET.request -> GET.response над COSEM Register (класс 3),
       атрибут 2 (value), адресуемым запрошенным OBIS-кодом.

Адресация и LLC-обёртка информационного поля проверены на реальном
оборудовании Risesun 2026-08-18 (см. DECISIONS.md) — см. docstring
``hdlc.py`` и ``dlms.py``.
"""

from __future__ import annotations

import logging

from typing import Iterator

from ..addressing import HDLC_DLMS, physical_address
from ..errors import GatewayError
from ..transport import TcpTransport
from . import datatypes, dlms
from .datatypes import DlmsDataError
from .hdlc import (
    CONTROL_SNRM,
    CONTROL_UA,
    DEFAULT_CLIENT_ADDRESS,
    SNRM_PARAMETER_NEGOTIATION,
    HdlcFrame,
    control_information_frame,
    read_frame_from_transport,
    server_hdlc_address,
)

logger = logging.getLogger("mmws_gateway.hdlc_dlms")


def read_register(
    transport: TcpTransport,
    *,
    serial: str,
    password: bytes,
    obis: str,
    class_id: int = dlms.REGISTER_CLASS_ID,
) -> object:
    establish_link(transport, serial=serial)
    return read_register_via_established_link(
        transport, serial=serial, password=password, obis=obis, class_id=class_id
    )


def establish_link(transport: TcpTransport, *, serial: str) -> None:
    """Только шаг SNRM -> UA — используется отдельно фоновым
    "прогревом" call-home соединений (см. ``callhome.py``), которые
    держат HDLC-связь установленной заранее, до того как понадобится
    реальное чтение регистра."""
    server_addr = server_hdlc_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS
    _establish_link(transport, server_addr, client_addr)


def read_register_via_established_link(
    transport: TcpTransport,
    *,
    serial: str,
    password: bytes,
    obis: str,
    class_id: int = dlms.REGISTER_CLASS_ID,
) -> object:
    """AARQ/AARE + GET поверх УЖЕ установленной (SNRM/UA пройден) HDLC-связи."""
    server_addr = server_hdlc_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS

    aarq = dlms.build_aarq(password)
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=0, recv_seq=0,
        information=dlms.wrap_llc_command(aarq),
    )
    aare_frame = _recv_i_frame(transport)
    dlms.parse_aare(dlms.unwrap_llc(aare_frame.information))  # бросает AuthFailedError при отказе

    request = dlms.build_get_request(dlms.parse_obis(obis), class_id=class_id)
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=1, recv_seq=1,
        information=dlms.wrap_llc_command(request),
    )
    response_frame = _recv_i_frame(transport)
    return dlms.parse_get_response(dlms.unwrap_llc(response_frame.information))


def write_register(
    transport: TcpTransport,
    *,
    serial: str,
    password: bytes,
    obis: str,
    encoded_value: bytes,
    class_id: int = dlms.REGISTER_CLASS_ID,
) -> None:
    """Запись параметра (Этап 2, ТЗ п. 4.2.4) — SNRM/UA + AARQ/AARE + SET.
    ``encoded_value`` — уже закодированное Common-Data-Type значение (см.
    ``protocols.datatypes.encode_*``); ничего не возвращает, бросает
    GatewayError при отказе (в т.ч. AuthFailedError)."""
    establish_link(transport, serial=serial)
    write_register_via_established_link(
        transport, serial=serial, password=password, obis=obis,
        encoded_value=encoded_value, class_id=class_id,
    )


def write_register_via_established_link(
    transport: TcpTransport,
    *,
    serial: str,
    password: bytes,
    obis: str,
    encoded_value: bytes,
    class_id: int = dlms.REGISTER_CLASS_ID,
) -> None:
    """AARQ/AARE + SET поверх УЖЕ установленной (SNRM/UA пройден) HDLC-связи."""
    server_addr = server_hdlc_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS

    aarq = dlms.build_aarq(password)
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=0, recv_seq=0,
        information=dlms.wrap_llc_command(aarq),
    )
    aare_frame = _recv_i_frame(transport)
    dlms.parse_aare(dlms.unwrap_llc(aare_frame.information))

    request = dlms.build_set_request(dlms.parse_obis(obis), encoded_value, class_id=class_id)
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=1, recv_seq=1,
        information=dlms.wrap_llc_command(request),
    )
    response_frame = _recv_i_frame(transport)
    dlms.parse_set_response(dlms.unwrap_llc(response_frame.information))


def execute_action(
    transport: TcpTransport,
    *,
    serial: str,
    password: bytes,
    obis: str,
    method_id: int,
    class_id: int,
) -> None:
    """Удалённое отключение/подключение счётчика (Этап 5, ТЗ п.4.2.10) —
    SNRM/UA + AARQ/AARE + ACTION. Ничего не возвращает, бросает
    GatewayError при отказе (в т.ч. AuthFailedError)."""
    establish_link(transport, serial=serial)
    execute_action_via_established_link(
        transport, serial=serial, password=password, obis=obis, method_id=method_id, class_id=class_id,
    )


def execute_action_via_established_link(
    transport: TcpTransport,
    *,
    serial: str,
    password: bytes,
    obis: str,
    method_id: int,
    class_id: int,
) -> None:
    """AARQ/AARE + ACTION поверх УЖЕ установленной (SNRM/UA пройден) HDLC-связи."""
    server_addr = server_hdlc_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS

    aarq = dlms.build_aarq(password)
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=0, recv_seq=0,
        information=dlms.wrap_llc_command(aarq),
    )
    aare_frame = _recv_i_frame(transport)
    dlms.parse_aare(dlms.unwrap_llc(aare_frame.information))

    request = dlms.build_action_request(dlms.parse_obis(obis), method_id, class_id=class_id)
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=1, recv_seq=1,
        information=dlms.wrap_llc_command(request),
    )
    response_frame = _recv_i_frame(transport)
    dlms.parse_action_response(dlms.unwrap_llc(response_frame.information))


def _establish_link(transport: TcpTransport, server_addr: int, client_addr: int) -> None:
    frame = HdlcFrame(
        destination=server_addr,
        source=client_addr,
        control=CONTROL_SNRM,
        information=SNRM_PARAMETER_NEGOTIATION,
    )
    transport.send(frame.encode())
    response = HdlcFrame.decode(read_frame_from_transport(transport))
    logger.info(
        "Ответ на SNRM: control=0x%02x src=%d dst=%d info=%s",
        response.control, response.source, response.destination, response.information.hex(),
    )
    if response.control != CONTROL_UA:
        raise GatewayError(
            "Счётчик не подтвердил установление HDLC-соединения (ожидался управляющий байт UA)"
        )


def _send_i_frame(
    transport: TcpTransport,
    server_addr: int,
    client_addr: int,
    *,
    send_seq: int,
    recv_seq: int,
    information: bytes,
) -> None:
    control = control_information_frame(send_seq, recv_seq)
    frame = HdlcFrame(
        destination=server_addr, source=client_addr, control=control, information=information
    )
    transport.send(frame.encode())


_MAX_SUPERVISORY_FRAMES_SKIPPED = 20


def _recv_i_frame(transport: TcpTransport) -> HdlcFrame:
    """Ждёт информационный (I-) кадр, пропуская супервизорные S-кадры
    (напр. RR — подтверждение приёма без данных). Подтверждено на
    реальном оборудовании 2026-08-18: счётчик сразу же отвечает RR-
    подтверждением на присланный AARQ (control такого кадра — нечётный,
    ``information`` пуст), а сам AARE в виде настоящего I-кадра приходит
    отдельным, следующим кадром — если считать первым же полученным
    кадром сразу ответ приложения, то это RR-подтверждение ошибочно
    принимается за AARE с пустыми (некорректными) LLC-данными."""
    for _ in range(_MAX_SUPERVISORY_FRAMES_SKIPPED):
        frame = HdlcFrame.decode(read_frame_from_transport(transport))
        logger.info(
            "Получен HDLC-кадр: control=0x%02x src=%d dst=%d info=%s",
            frame.control, frame.source, frame.destination, frame.information.hex(),
        )
        if frame.control & 0x01 == 0:  # I-кадр — control_information_frame() всегда даёт чётный control
            return frame
    raise GatewayError(
        "Счётчик прислал слишком много супервизорных кадров подряд без ответа приложения"
    )


def read_load_profile(
    transport: TcpTransport,
    *,
    serial: str,
    password: bytes,
    obis: str,
    class_id: int,
    from_dt,
    to_dt,
) -> Iterator[object]:
    """Профиль нагрузки (Этап 3, ТЗ п.4.2.3) — GET с выборкой по датам,
    при необходимости через несколько датаблоков (см. dlms.py).

    Генератор: отдаёт КАЖДУЮ строку буфера сразу, как только она
    полностью собрана из накопленных байт (не дожидаясь всего ответа
    целиком) — обрыв соединения посреди передачи не теряет уже
    отданные вызывающему коду строки (ТЗ п.4.2.3 — докачка при обрыве,
    is_partial). Каждая строка — то, что вернул ``datatypes.decode_value``
    для одного элемента массива (обычно список значений колонок,
    первая колонка по конвенции — метка времени)."""
    server_addr = server_hdlc_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS

    _establish_link(transport, server_addr, client_addr)

    aarq = dlms.build_aarq(password)
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=0, recv_seq=0,
        information=dlms.wrap_llc_command(aarq),
    )
    aare_frame = _recv_i_frame(transport)
    dlms.parse_aare(dlms.unwrap_llc(aare_frame.information))

    request = dlms.build_get_request_range(
        dlms.parse_obis(obis), class_id=class_id, from_dt=from_dt, to_dt=to_dt,
    )
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=1, recv_seq=1,
        information=dlms.wrap_llc_command(request),
    )

    send_seq = 2
    buf = bytearray()
    cursor = 0
    total_count: int | None = None
    decoded_count = 0

    while True:
        response_frame = _recv_i_frame(transport)
        payload = dlms.unwrap_llc(response_frame.information)
        response_type = payload[1] if len(payload) > 1 else None

        if response_type == dlms.GET_RESPONSE_NORMAL:
            # Весь ответ уместился в одном PDU — блочная передача не
            # понадобилась (короткий диапазон дат).
            value = dlms.parse_get_response(payload)
            for row in value:
                yield row
            return

        if response_type != dlms.GET_RESPONSE_WITH_DATABLOCK:
            raise GatewayError(
                f"Неожиданный тип GET.response при чтении профиля нагрузки: {payload[:2].hex()}"
            )

        block = dlms.parse_get_response_datablock(payload)
        buf.extend(block.raw_data)

        if total_count is None and len(buf) >= 2 and buf[0] == datatypes.TAG_ARRAY:
            total_count = buf[1]
            cursor = 2

        if total_count is not None:
            while decoded_count < total_count:
                try:
                    row, consumed = datatypes.decode_value(bytes(buf), offset=cursor)
                except DlmsDataError:
                    break  # строка ещё не собрана целиком — ждём следующий датаблок
                yield row
                cursor += consumed
                decoded_count += 1

        if block.last_block:
            if total_count is None or decoded_count < total_count:
                raise GatewayError(
                    "Буфер профиля нагрузки собран не полностью — данные оборвались "
                    "раньше заявленного количества строк"
                )
            return

        request_next = dlms.build_get_request_next(block.block_number + 1)
        _send_i_frame(
            transport, server_addr, client_addr, send_seq=send_seq, recv_seq=send_seq,
            information=dlms.wrap_llc_command(request_next),
        )
        send_seq += 1
