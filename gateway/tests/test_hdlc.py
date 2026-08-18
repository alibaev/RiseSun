"""Тесты HDLC-кадрирования: CRC16/X.25, сборка/разбор кадра."""

import socket

import pytest

from mmws_gateway.errors import CrcError, GatewayError
from mmws_gateway.protocols.hdlc import (
    CONTROL_SNRM,
    DEFAULT_CLIENT_ADDRESS,
    FLAG,
    HdlcFrame,
    control_information_frame,
    crc16_x25,
    read_frame_from_transport,
    server_hdlc_address,
)
from mmws_gateway.protocols import dlms
from mmws_gateway.transport import TcpServerTransport


def test_crc16_x25_known_vector():
    # "123456789" — стандартный проверочный вектор для CRC-16/X-25 (ожидаемое 0x906E).
    assert crc16_x25(b"123456789") == 0x906E


def test_frame_round_trip():
    frame = HdlcFrame(destination=5, source=16, control=CONTROL_SNRM, information=b"\x60\x01\x02")
    encoded = frame.encode()
    assert encoded[0] == FLAG
    assert encoded[-1] == FLAG

    decoded = HdlcFrame.decode(encoded)
    assert decoded.destination == 5
    assert decoded.source == 16
    assert decoded.control == CONTROL_SNRM
    assert decoded.information == b"\x60\x01\x02"


def test_frame_round_trip_empty_information():
    frame = HdlcFrame(destination=1, source=16, control=0x73)
    decoded = HdlcFrame.decode(frame.encode())
    assert decoded.information == b""


def test_decode_rejects_missing_flags():
    with pytest.raises(GatewayError):
        HdlcFrame.decode(b"\x01\x02\x03")


def test_decode_detects_corrupted_fcs():
    frame = HdlcFrame(destination=5, source=16, control=CONTROL_SNRM, information=b"\x60\x01")
    encoded = bytearray(frame.encode())
    encoded[-2] ^= 0xFF  # портим младший байт FCS
    with pytest.raises(CrcError):
        HdlcFrame.decode(bytes(encoded))


def test_decode_detects_corrupted_hcs():
    frame = HdlcFrame(destination=5, source=16, control=CONTROL_SNRM, information=b"\x60\x01")
    encoded = bytearray(frame.encode())
    # HCS находится сразу после frame_format(2)+dest(1)+src(1)+control(1) = байты [5:7]
    encoded[5] ^= 0xFF
    with pytest.raises(CrcError):
        HdlcFrame.decode(bytes(encoded))


def test_server_hdlc_address_matches_real_capture():
    # Проверено на реальном счётчике Risesun 202001002352 (2026-08-18):
    # upper=1 (логическое устройство), lower=2352 (последние 5 цифр серийного) -> 18736.
    assert server_hdlc_address("02352") == (1 << 14) | 2352


def test_server_hdlc_address_supports_up_to_14_bit_physical_address():
    # Раньше адрес приводился по модулю 128 — теперь влезает до 14 бит (16383),
    # этого достаточно для обоих проверенных реальных счётчиков (2352, 4113).
    assert server_hdlc_address("16383") == (1 << 14) | 16383


def test_server_hdlc_address_rejects_5_digit_overflow():
    # Известное ограничение (см. DECISIONS.md, 2026-08-18): last-5-digits
    # серийного может доходить до 99999, что превышает 14-битный lower —
    # редкий случай, требующий отдельного решения при встрече на практике.
    with pytest.raises(ValueError):
        server_hdlc_address("99999")


def test_multi_byte_address_round_trip():
    # 18736 не влезает в 1 байт (127) и не влезает в 2 байта (16383) —
    # требует 4-байтной адресации, подтверждённой реальным трафиком.
    frame = HdlcFrame(destination=18736, source=48, control=CONTROL_SNRM)
    encoded = frame.encode()
    decoded = HdlcFrame.decode(encoded)
    assert decoded.destination == 18736
    assert decoded.source == 48


@pytest.mark.parametrize("bad_address", [-1, 2**28])
def test_hdlc_address_out_of_range_rejected(bad_address):
    with pytest.raises(ValueError):
        HdlcFrame(destination=bad_address, source=16, control=CONTROL_SNRM).encode()


def test_read_frame_handles_embedded_flag_byte_in_body():
    """Регрессия, найденная на живой проверке 2026-08-18 (счётчик
    999000111222): второй байт HCS у GET.request-кадра для этого адреса
    случайно совпадает с 0x7E (FLAG). Чтение кадра сканированием до
    первого 0x7E (recv_until) обрезало такой кадр раньше времени —
    read_frame_from_transport должен читать строго по длине из поля
    Frame Format, независимо от того, что встретится в теле."""
    request = dlms.build_get_request(dlms.parse_obis("1.1.1.8.0.ff"), invoke_id=1)
    obis_info = dlms.wrap_llc_command(request)
    frame = HdlcFrame(
        destination=server_hdlc_address("11222"),
        source=DEFAULT_CLIENT_ADDRESS,
        control=control_information_frame(1, 1),
        information=obis_info,
    )
    encoded = frame.encode()
    assert 0x7E in encoded[1:-1], "тестовый кейс должен провоцировать именно этот баг"

    server_sock, client_sock = socket.socketpair()
    try:
        server_sock.sendall(encoded)
        server_sock.close()
        transport = TcpServerTransport.from_accepted_socket(client_sock, peer_host="test", peer_port=0)
        raw = read_frame_from_transport(transport)
    finally:
        client_sock.close()

    assert raw == encoded
    decoded = HdlcFrame.decode(raw)
    assert decoded.destination == server_hdlc_address("11222")
    assert decoded.information == obis_info
