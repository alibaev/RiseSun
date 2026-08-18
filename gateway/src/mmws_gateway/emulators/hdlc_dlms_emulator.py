"""Программный эмулятор счётчика с профилем HDLC + DLMS/COSEM.

Это ЭМУЛЯТОР для интеграционных тестов Gateway, а не замена реальным
полевым испытаниям (Promt_MMWS.md, раздел «Этап 0», п. 4).
"""

from __future__ import annotations

import socket
import time

from ..protocols import datatypes, dlms
from ..protocols.hdlc import (
    CONTROL_SNRM,
    CONTROL_UA,
    FLAG,
    HdlcFrame,
    control_information_frame,
)
from .common import ConnectionCounter, ErrorInjection, recv_exact, recv_until

_TIMEOUT_STALL_S = 0.3
OBJECT_UNDEFINED = 9  # data-access-result: object-undefined (Green Book)


def make_hdlc_dlms_handler(
    *,
    password: bytes,
    obis_values: dict[bytes, int],
    error_injection: ErrorInjection,
    counter: ConnectionCounter,
):
    """Возвращает обработчик TCP-подключения для ``ThreadedEmulatorServer``."""

    def handler(conn: socket.socket) -> None:
        conn.settimeout(5)
        attempt = counter.next()

        if error_injection.force_timeout:
            time.sleep(_TIMEOUT_STALL_S)
            return

        serve_hdlc_dlms_session(
            conn,
            password=password,
            obis_values=obis_values,
            error_injection=error_injection,
            attempt=attempt,
        )

    return handler


def serve_hdlc_dlms_session(
    conn: socket.socket,
    *,
    password: bytes,
    obis_values: dict[bytes, int],
    error_injection: ErrorInjection,
    attempt: int,
) -> None:
    """Обслуживает установление HDLC-соединения, AARQ/AARE и один GET.

    Вынесено отдельной функцией, чтобы её мог переиспользовать эмулятор
    режима E (``mode_e_emulator``) после собственной идентификационной
    преамбулы IEC 62056-21.
    """
    snrm = HdlcFrame.decode(_read_frame(conn))
    if snrm.control != CONTROL_SNRM:
        return
    ua = HdlcFrame(destination=snrm.source, source=snrm.destination, control=CONTROL_UA)
    conn.sendall(ua.encode())

    aarq_frame = HdlcFrame.decode(_read_frame(conn))
    parsed_aarq = dlms.parse_aarq(dlms.unwrap_llc(aarq_frame.information))
    accepted = (not error_injection.force_auth_fail) and parsed_aarq.password == password
    aare = dlms.build_aare(accepted=accepted)
    aare_frame = HdlcFrame(
        destination=aarq_frame.source,
        source=aarq_frame.destination,
        control=control_information_frame(0, 1),
        information=dlms.wrap_llc_response(aare),
    )
    conn.sendall(aare_frame.encode())
    if not accepted:
        return

    get_frame = HdlcFrame.decode(_read_frame(conn))
    get_request = dlms.parse_get_request(dlms.unwrap_llc(get_frame.information))
    value = obis_values.get(get_request.obis)
    if value is None:
        info = dlms.build_get_response_error(get_request.invoke_id, OBJECT_UNDEFINED)
    else:
        info = dlms.build_get_response_data(
            get_request.invoke_id, datatypes.encode_double_long_unsigned(value)
        )
    response_frame = HdlcFrame(
        destination=get_frame.source,
        source=get_frame.destination,
        control=control_information_frame(1, 2),
        information=dlms.wrap_llc_response(info),
    )
    encoded = response_frame.encode()

    if error_injection.force_crc_error and attempt <= error_injection.fail_attempts:
        corrupted = bytearray(encoded)
        corrupted[-3] ^= 0xFF  # портим младший байт FCS перед закрывающим флагом
        conn.sendall(bytes(corrupted))
        return

    if error_injection.force_partial_disconnect and attempt <= error_injection.fail_attempts:
        conn.sendall(encoded[: len(encoded) // 2])
        return

    conn.sendall(encoded)


def _read_frame(conn: socket.socket) -> bytes:
    """Читает кадр по длине из Frame Format, не сканированием на 0x7E —
    см. подробное обоснование в ``protocols.hdlc.read_frame_from_transport``
    (тот же баг воспроизводится и на серверной стороне эмулятора)."""
    first = recv_exact(conn, 1)
    if first != bytes([FLAG]):
        raise ConnectionError("Ожидался открывающий флаг HDLC")
    frame_format = recv_exact(conn, 2)
    declared_len = int.from_bytes(frame_format, "big") & 0x07FF
    rest = recv_exact(conn, declared_len - 2 + 1)
    return first + frame_format + rest
