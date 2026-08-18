"""Протокольный профиль IEC 62056-21, режим E (ТЗ п. 4.3.2, второй профиль).

Идентификация выполняется так же, как в режиме C (см. ``mode_c``), но
option select message переключает счётчик на кадрирование HDLC
(``mode="2"``), после чего обмен продолжается по HDLC + DLMS/COSEM —
тем же кодом, что и профиль ``hdlc_dlms`` (правило адресации у mode_e и
hdlc_dlms одинаковое — последние 5 цифр серийного номера, ТЗ п. 4.3.2,
и уже учтено внутри ``hdlc_dlms.read_register``).
"""

from __future__ import annotations

from ..transport import TcpTransport
from . import hdlc_dlms
from .mode_c import build_identification_request, build_option_select, parse_identification

_HDLC_HANDOFF_MODE = "2"


def read_register(
    transport: TcpTransport, *, serial: str, password: bytes, obis: str
) -> object:
    transport.send(build_identification_request(serial))
    ident_raw = transport.recv_until(b"\r\n")
    ident = parse_identification(ident_raw)

    transport.send(build_option_select(ident.baud_char, mode=_HDLC_HANDOFF_MODE))

    return hdlc_dlms.read_register(transport, serial=serial, password=password, obis=obis)
