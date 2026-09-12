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
    # restricting_object — снова Clock-структура, теперь из 4 полей
    # (class_id, logical_name, attribute_index, data_index=0) — 2026-09-12,
    # реальные байтовые трассы 4 успешных сеансов IECMeterManage.exe
    # (предоставлены пользователем, см. DECISIONS.md) подтвердили
    # четырёхпольную структуру (раньше кодировали только 3 поля).
    assert request[descriptor_end + 4] == datatypes.TAG_STRUCTURE
    assert request[descriptor_end + 5] == 4  # class_id, logical_name, attribute_index, data_index
    assert request[descriptor_end + 6] == datatypes.TAG_LONG_UNSIGNED
    assert request[descriptor_end + 7 : descriptor_end + 9] == (8).to_bytes(2, "big")  # class Clock
    restricting_end = descriptor_end + 9
    assert request[restricting_end] == datatypes.TAG_OCTET_STRING
    assert request[restricting_end + 1] == 6
    assert request[restricting_end + 2 : restricting_end + 8] == bytes([0, 0, 1, 0, 0, 0xFF])
    assert request[restricting_end + 8] == datatypes.TAG_INTEGER
    assert request[restricting_end + 9] == 2  # attribute_index (value)
    assert request[restricting_end + 10] == datatypes.TAG_LONG_UNSIGNED
    assert request[restricting_end + 11 : restricting_end + 13] == (0).to_bytes(2, "big")  # data_index
    # from_value сразу следующим элементом — тег date_time (2026-09-12,
    # см. DECISIONS.md: было octet-string(cosem-date-time) — найдено
    # побайтовым разбором GXDLMSReader.cs::RS_PostProcessingProfileGenericsDates,
    # легаси-программа патчит именно такой исходящий запрос от Gurux.DLMS,
    # заменяя "09 0C" на один байт 0x19).
    from_pos = restricting_end + 13
    assert request[from_pos] == datatypes.TAG_DATE_TIME
    # date_time — БЕЗ отдельного байта длины (фиксированные 12 байт сразу).
    assert request[from_pos + 1 : from_pos + 3] == (2026).to_bytes(2, "big")


def test_get_request_range_encodes_selected_values_when_given():
    """2026-09-12 (SSH-доступ к 192.168.144.79, реальный список
    capture_objects из DataReadScheme.ini рабочего IECMeterManage.exe —
    см. DECISIONS.md и dlms.DTZY217_LOAD_PROFILE_CAPTURE_OBJECTS) —
    selected_values больше не всегда пуст: если передан список
    (class_id, obis, attribute_index, data_index), каждый элемент
    кодируется 4-польной структурой."""
    from datetime import datetime

    obis = dlms.parse_obis("1.0.63.1.0.ff")
    selected = [(3, bytes.fromhex("0101010800FF"), 2, 3)]
    request = dlms.build_get_request_range(
        obis, class_id=7, from_dt=datetime(2026, 8, 1), to_dt=datetime(2026, 8, 19),
        invoke_id=9, selected_values=selected,
    )
    decoded, _consumed = datatypes.decode_value(request, offset=3 + 2 + 6 + 1 + 2)
    # decoded — вся access-parameters структура из 4 элементов:
    # [restricting_object, from, to, selected_values]
    assert decoded[3] == [[3, bytes.fromhex("0101010800FF"), 2, 3]]


def test_get_request_range_selected_values_empty_by_default():
    from datetime import datetime

    obis = dlms.parse_obis("1.0.63.1.0.ff")
    request = dlms.build_get_request_range(
        obis, class_id=7, from_dt=datetime(2026, 8, 1), to_dt=datetime(2026, 8, 19), invoke_id=9,
    )
    decoded, _consumed = datatypes.decode_value(request, offset=3 + 2 + 6 + 1 + 2)
    assert decoded[3] == []


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


def test_datablock_response_length_ber_encoding_short_data():
    """2026-09-12 (реальные байтовые трассы, см. DECISIONS.md) — найденный
    баг: длина датаблока раньше кодировалась/разбиралась ФИКСИРОВАННЫМИ
    2 байтами всегда, что для датаблоков короче 128 байт съедало на 1
    байт больше настоящей длины (реальная BER-кодировка для длин <128 —
    ОДИН байт). Проверяем byte-for-byte."""
    response = dlms.build_get_response_datablock(
        0x10, last_block=False, block_number=1, raw_data=bytes(range(110))
    )
    # header: tag,choice,invoke,last,block_number(4),result_choice = 9 байт
    assert response[9] == 110  # длина — ОДИН байт (110 < 128), без 0x81-префикса
    assert response[10:110] == bytes(range(100))
    result = dlms.parse_get_response_datablock(response)
    assert result.raw_data == bytes(range(110))


def test_datablock_response_length_ber_encoding_long_data():
    """Датаблоки 128..255 байт — BER-кодировка ДЛИНЫ ровно 2 байта
    (``0x81`` + сама длина), что случайно совпадало со старым (неверным)
    допущением "всегда 2 байта" — поэтому баг был незаметен на типичных
    «полных» датаблоках (см. test_datablock_response_length_ber_encoding_
    short_data)."""
    raw = bytes(range(178))
    response = dlms.build_get_response_datablock(
        0x11, last_block=False, block_number=1, raw_data=raw
    )
    assert response[9] == 0x81
    assert response[10] == 178
    result = dlms.parse_get_response_datablock(response)
    assert result.raw_data == raw


def test_datablock_response_matches_real_meter_capture():
    """Регрессионный тест на реальных байтах (не эмуляция) — первый
    датаблок ответа на GET атрибута 3 (capture_objects) счётчика
    202001002481, захваченный tcpdump-ом с работающего IECMeterManage.exe
    (файл logs.zip, предоставлен пользователем 2026-09-12, см.
    DECISIONS.md). Раньше (баг с фиксированной 2-байтной длиной)
    разбирался как 28161-байтный датаблок вместо настоящих 110 байт —
    ``raw_data`` терял свой первый байт и сдвигался."""
    apdu = bytes.fromhex(
        "c402100000000001006e010b020412000309060101200700ff0f0212000302"
        "0412000309060101340700ff0f02120003020412000309060101480700ff0f"
        "021200030204120003090601011f0700ff0f0212000302041200030906010"
        "1330700ff0f02120003020412000309060101470700ff0f02120003"
    )
    result = dlms.parse_get_response_datablock(apdu)
    assert result.last_block is False
    assert result.block_number == 1
    assert len(result.raw_data) == 110
    assert result.raw_data[0] == datatypes.TAG_ARRAY
    assert result.raw_data[1] == 11  # count=11 захватываемых колонок


def test_decode_load_profile_row_matches_real_meter_capture():
    """Регрессионный тест на РЕАЛЬНЫХ байтах — первая строка буфера
    профиля нагрузки счётчика 202001002481, захваченная с работающего
    IECMeterManage.exe (файл logs.zip, предоставлен пользователем
    2026-09-12, см. DECISIONS.md). Формат расшифрован побайтовым разбором
    декомпилированного ``ReadLPDataForm_DLMS.cs::dateAnalysis``/
    ``dataAnalysis`` (``ver2.zip``): маркер A0 A0, метка времени и поля
    значений — BCD-цифры в ОБРАТНОМ порядке байт."""
    row = bytes.fromhex(
        "a0a0000000110926426555020042695502004295540200430700000043000000"
        "004328000000343990003400000134999900812935020000000000811427020"
        "000000000"
    )
    timestamp, values = dlms.decode_load_profile_row(row)
    assert timestamp.isoformat() == "2026-09-11T00:00:00"
    assert values == pytest.approx(
        [255.65, 255.69, 254.95, 0.007, 0.0, 0.028, 0.9039, 1.0, 0.9999, 2352.9, 2271.4]
    )


def test_load_profile_row_encode_decode_round_trip():
    from datetime import datetime

    fields = [(255.65, 3, 2), (-12.3, 2, 1), (0.0, 1, 0)]
    row = dlms.encode_load_profile_row(datetime(2026, 9, 11, 0, 30, 0), fields)
    timestamp, values = dlms.decode_load_profile_row(row)
    assert timestamp == datetime(2026, 9, 11, 0, 30, 0)
    assert values == pytest.approx([255.65, -12.3, 0.0])


def test_split_load_profile_rows_handles_marker_split_across_chunks():
    """Маркер ``A0 A0`` может оказаться разрезан пополам между двумя
    датаблоками — split_load_profile_rows должна просто не находить
    вторую строку, пока оба байта маркера не окажутся в буфере (не
    ронять исключение, не терять данные)."""
    from datetime import datetime

    row1 = dlms.encode_load_profile_row(datetime(2026, 9, 11, 0, 0, 0), [(1.0, 1, 0)])
    row2 = dlms.encode_load_profile_row(datetime(2026, 9, 11, 0, 30, 0), [(2.0, 1, 0)])
    whole = row1 + row2
    split_point = len(row1) + 1  # разрезаем ПОСЛЕ первого байта маркера второй строки
    part1, part2 = whole[:split_point], whole[split_point:]

    rows, remainder = dlms.split_load_profile_rows(part1)
    assert rows == []
    assert remainder == part1  # вторая строка ещё не распознана — маркер не завершён

    rows, remainder = dlms.split_load_profile_rows(part1 + part2)
    assert len(rows) == 1
    assert remainder == row2
    assert dlms.decode_load_profile_row(rows[0]) == dlms.decode_load_profile_row(row1)
    assert dlms.decode_load_profile_row(remainder) == dlms.decode_load_profile_row(row2)


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
