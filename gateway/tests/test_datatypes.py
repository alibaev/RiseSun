"""Тесты кодирования/декодирования типов данных DLMS Common-Data-Types."""

import pytest

from mmws_gateway.protocols import datatypes


@pytest.mark.parametrize(
    "encode_fn,value",
    [
        (datatypes.encode_double_long_unsigned, 1234567),
        (datatypes.encode_double_long, -1234567),
        (datatypes.encode_long_unsigned, 4321),
        (datatypes.encode_long, -4321),
        (datatypes.encode_unsigned, 200),
        (datatypes.encode_integer, -100),
    ],
)
def test_numeric_round_trip(encode_fn, value):
    encoded = encode_fn(value)
    decoded, consumed = datatypes.decode_value(encoded)
    assert decoded == value
    assert consumed == len(encoded)


def test_octet_string_round_trip():
    encoded = datatypes.encode_octet_string(b"\x01\x02\x03")
    decoded, consumed = datatypes.decode_value(encoded)
    assert decoded == b"\x01\x02\x03"
    assert consumed == len(encoded)


def test_visible_string_round_trip():
    encoded = datatypes.encode_visible_string("RSN123")
    decoded, consumed = datatypes.decode_value(encoded)
    assert decoded == "RSN123"
    assert consumed == len(encoded)


def test_decode_unsupported_tag_raises():
    with pytest.raises(datatypes.DlmsDataError):
        datatypes.decode_value(bytes([0xEE, 0x00]))


def test_decode_truncated_data_raises():
    encoded = datatypes.encode_double_long_unsigned(1000)
    with pytest.raises(datatypes.DlmsDataError):
        datatypes.decode_value(encoded[:-1])


def test_decode_bcd_ascii_number():
    assert datatypes.decode_bcd_ascii_number("001234.567") == pytest.approx(1234.567)
