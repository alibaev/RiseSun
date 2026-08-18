"""Протокольный профиль «HDLC + DLMS/COSEM» (ТЗ п. 4.3.2, третий профиль).

Последовательность одной операции чтения:
    1. SNRM -> UA (установление HDLC-соединения);
    2. AARQ -> AARE в I-кадрах (установление ассоциации DLMS, пароль
       низкого уровня безопасности — ТЗ п. 4.3.3);
    3. GET.request -> GET.response над COSEM Register (класс 3),
       атрибут 2 (value), адресуемым запрошенным OBIS-кодом.
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
    HdlcFrame,
    control_information_frame,
    encode_server_address,
    read_frame_from_transport,
)


def read_register(
    transport: TcpTransport, *, serial: str, password: bytes, obis: str
) -> object:
    server_addr = encode_server_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS

    _establish_link(transport, server_addr, client_addr)

    aarq = dlms.build_aarq(password)
    _send_i_frame(transport, server_addr, client_addr, send_seq=0, recv_seq=0, information=aarq)
    aare_frame = _recv_i_frame(transport)
    dlms.parse_aare(aare_frame.information)  # бросает AuthFailedError при отказе

    request = dlms.build_get_request(dlms.parse_obis(obis))
    _send_i_frame(transport, server_addr, client_addr, send_seq=1, recv_seq=1, information=request)
    response_frame = _recv_i_frame(transport)
    return dlms.parse_get_response(response_frame.information)


def _establish_link(transport: TcpTransport, server_addr: int, client_addr: int) -> None:
    frame = HdlcFrame(destination=server_addr, source=client_addr, control=CONTROL_SNRM)
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
