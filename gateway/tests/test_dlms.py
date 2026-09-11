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


def test_get_response_invalid_choice_byte_raises_clear_error():
    """2026-09-11, найдено на живом трафике (см. DECISIONS.md — "поймать
    байты одного отказа"): choice-байт Get-Data-Result по стандарту может
    быть только 0x00 (data) или 0x01 (data-access-result) — реально
    приходил ответ ровно 4 байта (тег+тип+invoke_id+choice=0x17), CRC/HCS
    кадра при этом сходились (не повреждение при приёме). Раньше падало
    непонятным "Пустые данные при разборе значения DLMS"."""
    response = bytes([dlms.GET_RESPONSE_TAG, dlms.GET_RESPONSE_NORMAL, 1, 0x17])
    with pytest.raises(GatewayError, match="0x17"):
        dlms.parse_get_response(response)


def test_get_request_with_custom_class_id():
    # class 1 (Data) — параметры вроде «Current Time» (Этап 2, ТЗ п.4.2.4,
    # словарь OBIS RW_Tree_параметры), не Register (class 3).
    obis = dlms.parse_obis("1.0.0.9.1.ff")
    request = dlms.build_get_request(obis, class_id=1)
    parsed = dlms.parse_get_request(request)
    assert parsed.class_id == 1
    assert parsed.obis == obis


def test_set_request_response_round_trip():
    obis = dlms.parse_obis("1.0.0.9.1.ff")
    value = datatypes.encode_octet_string(bytes([12, 30, 0]))  # 12:30:00
    request = dlms.build_set_request(obis, value, invoke_id=3, class_id=1)
    parsed = dlms.parse_set_request(request)
    assert parsed.invoke_id == 3
    assert parsed.class_id == 1
    assert parsed.obis == obis
    assert parsed.value == bytes([12, 30, 0])
    assert parsed.encoded_value == value

    response = dlms.build_set_response(3)
    dlms.parse_set_response(response)  # не бросает — успех


def test_set_response_failure_raises():
    response = dlms.build_set_response(3, result=dlms.SET_RESULT_SUCCESS + 1)
    with pytest.raises(GatewayError):
        dlms.parse_set_response(response)


def test_encode_oid_matches_manual_encoding():
    # {2 16 756 5 8 1 1}: первый байт 40*2+16=96=0x60; 756 -> 0x85 0x74 (base-128).
    assert dlms.encode_oid((2, 16, 756, 5, 8, 1, 1)) == bytes(
        [0x60, 0x85, 0x74, 0x05, 0x08, 0x01, 0x01]
    )


def test_get_request_range_has_access_selection_present_flag():
    from datetime import datetime

    obis = dlms.parse_obis("1.0.63.1.0.ff")
    request = dlms.build_get_request_range(
        obis, class_id=7, from_dt=datetime(2026, 8, 1), to_dt=datetime(2026, 8, 19), invoke_id=9
    )
    assert request[0] == dlms.GET_REQUEST_TAG
    assert request[1] == dlms.GET_REQUEST_NORMAL
    assert request[2] == 9
    descriptor_end = 3 + 2 + 6 + 1
    assert request[descriptor_end] == 0x01  # access-selection присутствует
    assert request[descriptor_end + 1] == dlms.RANGE_DESCRIPTOR_SELECTOR
    # access-parameters — структура из 4 элементов
    assert request[descriptor_end + 2] == datatypes.TAG_STRUCTURE
    assert request[descriptor_end + 3] == 4
    # restricting_object — NULL-DATA (2026-09-10, см. DECISIONS.md: было
    # Clock-структурой, счётчик без Clock в захватываемых колонках
    # отвергал её data-access-result=250; сверено побайтово с реально
    # работающей заводской программой ver2.zip).
    assert request[descriptor_end + 4] == datatypes.TAG_NULL_DATA
    # from_value сразу следующим элементом — octet-string (cosem-date-time).
    assert request[descriptor_end + 5] == datatypes.TAG_OCTET_STRING


def test_get_request_next_round_trip_shape():
    request = dlms.build_get_request_next(3, invoke_id=5)
    assert request == bytes([dlms.GET_REQUEST_TAG, dlms.GET_REQUEST_NEXT, 5, 0, 0, 0, 3])


def test_datablock_response_round_trip():
    response = dlms.build_get_response_datablock(
        7, last_block=False, block_number=2, raw_data=b"\x01\x02\x03"
    )
    result = dlms.parse_get_response_datablock(response)
    assert result.last_block is False
    assert result.block_number == 2
    assert result.raw_data == b"\x01\x02\x03"


def test_datablock_response_last_block_true():
    response = dlms.build_get_response_datablock(
        7, last_block=True, block_number=3, raw_data=b""
    )
    result = dlms.parse_get_response_datablock(response)
    assert result.last_block is True
    assert result.raw_data == b""


def test_disconnect_control_obis_hex_matches_standard_decimal_address():
    # Стандартный decimal-адрес 0-0:96.3.10.255 -> hex "0.0.60.3.a.ff"
    # (то же правило перевода, что и в Этапах 2/3 — см. DECISIONS.md).
    assert dlms.parse_obis(dlms.DISCONNECT_CONTROL_OBIS) == bytes([0x00, 0x00, 0x60, 0x03, 0x0A, 0xFF])


def test_action_request_response_round_trip():
    obis = dlms.parse_obis(dlms.DISCONNECT_CONTROL_OBIS)
    request = dlms.build_action_request(
        obis, dlms.METHOD_REMOTE_DISCONNECT, class_id=dlms.DISCONNECT_CONTROL_CLASS_ID, invoke_id=5
    )
    parsed_request = dlms.parse_action_request(request)
    assert parsed_request.invoke_id == 5
    assert parsed_request.class_id == dlms.DISCONNECT_CONTROL_CLASS_ID
    assert parsed_request.obis == obis
    assert parsed_request.method_id == dlms.METHOD_REMOTE_DISCONNECT
    assert parsed_request.parameters is None

    response = dlms.build_action_response(5)
    dlms.parse_action_response(response)  # не бросает — успех


def test_action_response_failure_raises():
    response = dlms.build_action_response(5, result=dlms.ACTION_RESULT_SUCCESS + 1)
    with pytest.raises(GatewayError):
        dlms.parse_action_response(response)


def test_action_request_with_parameters_round_trip():
    obis = dlms.parse_obis(dlms.DISCONNECT_CONTROL_OBIS)
    request = dlms.build_action_request(
        obis, dlms.METHOD_REMOTE_RECONNECT, class_id=dlms.DISCONNECT_CONTROL_CLASS_ID,
        parameters=datatypes.encode_unsigned(1),
    )
    parsed = dlms.parse_action_request(request)
    assert parsed.method_id == dlms.METHOD_REMOTE_RECONNECT
    assert parsed.parameters == datatypes.encode_unsigned(1)
