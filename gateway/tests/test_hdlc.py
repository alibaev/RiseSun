"""Тесты HDLC-кадрирования: CRC16/X.25, сборка/разбор кадра."""

import pytest

from mmws_gateway.errors import CrcError, GatewayError
from mmws_gateway.protocols.hdlc import (
    CONTROL_SNRM,
    FLAG,
    HdlcFrame,
    crc16_x25,
    encode_server_address,
)


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


def test_encode_server_address_wraps_into_7_bits():
    # Последние 5 цифр серийного номера могут превышать 127 — адрес приводится по модулю.
    assert 0 <= encode_server_address("99999") <= 0x7F


@pytest.mark.parametrize("bad_address", [-1, 128, 999])
def test_hdlc_address_out_of_range_rejected(bad_address):
    with pytest.raises(ValueError):
        HdlcFrame(destination=bad_address, source=16, control=CONTROL_SNRM).encode()
