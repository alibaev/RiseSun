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


def test_structure_round_trip():
    encoded = datatypes.encode_structure(
        [datatypes.encode_double_long_unsigned(7), datatypes.encode_unsigned(1)]
    )
    decoded, consumed = datatypes.decode_value(encoded)
    assert decoded == [7, 1]
    assert consumed == len(encoded)


def test_array_round_trip():
    encoded = datatypes.encode_array([datatypes.encode_unsigned(1), datatypes.encode_unsigned(2)])
    decoded, consumed = datatypes.decode_value(encoded)
    assert decoded == [1, 2]
    assert consumed == len(encoded)


def test_array_of_structures_round_trip():
    row = datatypes.encode_structure(
        [datatypes.encode_octet_string(b"\x00" * 12), datatypes.encode_double_long_unsigned(1234)]
    )
    encoded = datatypes.encode_array([row, row])
    decoded, consumed = datatypes.decode_value(encoded)
    assert decoded == [[b"\x00" * 12, 1234], [b"\x00" * 12, 1234]]
    assert consumed == len(encoded)


def test_cosem_date_time_round_trip():
    from datetime import datetime

    dt = datetime(2026, 8, 19, 12, 30, 45)
    raw = datatypes.encode_cosem_date_time(dt)
    assert len(raw) == 12
    decoded = datatypes.decode_cosem_date_time(raw)
    assert decoded == dt
