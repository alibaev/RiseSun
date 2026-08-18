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

from ..addressing import HDLC_DLMS, physical_address
from ..errors import GatewayError
from ..transport import TcpTransport
from . import dlms
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


def read_register(
    transport: TcpTransport, *, serial: str, password: bytes, obis: str
) -> object:
    server_addr = server_hdlc_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS

    _establish_link(transport, server_addr, client_addr)

    aarq = dlms.build_aarq(password)
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=0, recv_seq=0,
        information=dlms.wrap_llc_command(aarq),
    )
    aare_frame = _recv_i_frame(transport)
    dlms.parse_aare(dlms.unwrap_llc(aare_frame.information))  # бросает AuthFailedError при отказе

    request = dlms.build_get_request(dlms.parse_obis(obis))
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=1, recv_seq=1,
        information=dlms.wrap_llc_command(request),
    )
    response_frame = _recv_i_frame(transport)
    return dlms.parse_get_response(dlms.unwrap_llc(response_frame.information))


def _establish_link(transport: TcpTransport, server_addr: int, client_addr: int) -> None:
    frame = HdlcFrame(
        destination=server_addr,
        source=client_addr,
        control=CONTROL_SNRM,
        information=SNRM_PARAMETER_NEGOTIATION,
    )
    transport.send(frame.encode())
    response = HdlcFrame.decode(read_frame_from_transport(transport))
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


def _recv_i_frame(transport: TcpTransport) -> HdlcFrame:
    return HdlcFrame.decode(read_frame_from_transport(transport))
