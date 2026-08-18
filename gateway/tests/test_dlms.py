"""Тесты xDLMS APDU: AARQ/AARE, OBIS, GET.request/GET.response."""

import pytest

from mmws_gateway.errors import AuthFailedError, GatewayError
from mmws_gateway.protocols import datatypes, dlms


def test_parse_obis_hex_fields():
    assert dlms.parse_obis("1.1.1.8.0.ff") == bytes([0x01, 0x01, 0x01, 0x08, 0x00, 0xFF])


def test_parse_obis_rejects_wrong_arity():
    with pytest.raises(GatewayError):
        dlms.parse_obis("1.1.1.8.0")


def test_parse_obis_rejects_out_of_range():
    with pytest.raises(GatewayError):
        dlms.parse_obis("1.1.1.8.0.100")


def test_aarq_aare_round_trip_accepted():
    aarq = dlms.build_aarq(b"12345678")
    parsed = dlms.parse_aarq(aarq)
    assert parsed.password == b"12345678"

    aare = dlms.build_aare(accepted=True)
    assert dlms.parse_aare(aare) is True


def test_aare_rejected_raises_auth_failed():
    aare = dlms.build_aare(accepted=False)
    with pytest.raises(AuthFailedError):
        dlms.parse_aare(aare)


def test_get_request_response_round_trip():
    obis = dlms.parse_obis("1.1.1.8.0.ff")
    request = dlms.build_get_request(obis, invoke_id=7)
    parsed_request = dlms.parse_get_request(request)
    assert parsed_request.invoke_id == 7
    assert parsed_request.class_id == dlms.REGISTER_CLASS_ID
    assert parsed_request.obis == obis
    assert parsed_request.attribute_id == dlms.REGISTER_VALUE_ATTRIBUTE

    response = dlms.build_get_response_data(7, datatypes.encode_double_long_unsigned(123456))
    assert dlms.parse_get_response(response) == 123456


def test_get_response_error_raises():
    response = dlms.build_get_response_error(7, 9)
    with pytest.raises(GatewayError):
        dlms.parse_get_response(response)


def test_encode_oid_matches_manual_encoding():
    # {2 16 756 5 8 1 1}: первый байт 40*2+16=96=0x60; 756 -> 0x85 0x74 (base-128).
    assert dlms.encode_oid((2, 16, 756, 5, 8, 1, 1)) == bytes(
        [0x60, 0x85, 0x74, 0x05, 0x08, 0x01, 0x01]
    )
