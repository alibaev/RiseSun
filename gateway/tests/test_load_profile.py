"""Интеграционные тесты профиля нагрузки (Этап 3, ТЗ п.4.2.3) против
программного эмулятора — SNRM/UA + AARQ/AARE + GET с выборкой по датам,
блочная передача (GET.response-with-datablock + GET.request-Next) и
инкрементальная сборка строк в hdlc_dlms.read_load_profile.

DLMS-адрес профиля нагрузки — decimal-нотация 1-1:99.1.0.255 (IEC
62056-6-2, канал 1, class 7) — подтверждено экспортом реальной
объектной модели счётчика DTZY217 (сервисная программа завода,
2026-08-19, см. DECISIONS.md). В проекте OBIS-коды пишутся в
HEX-нотации (см. правило конвертации decimal->hex по полям,
DECISIONS.md, «Этап 2, итерация 3»), поэтому decimal C=99 -> "63",
F=255 -> "ff": итоговая строка "1.1.63.1.0.ff".

2026-09-12 (реальные байтовые трассы 4 успешных сеансов
IECMeterManage.exe, предоставленные пользователем, см. DECISIONS.md) —
буфер этой модели счётчика НЕ кодируется стандартным DLMS array-of-
structure: каждая строка несёт СОБСТВЕННУЮ метку времени (см.
``dlms.decode_load_profile_row`` — маркер ``A0 A0`` + BCD-поля). Более
ранняя находка ("буфер не включает Clock, метка времени вычисляется из
отдельного GET capture_period") была ошибочной — отдельного GET
capture_period больше нет."""

import socket

from datetime import datetime, timedelta

import pytest

from mmws_gateway.emulators.common import ConnectionCounter, ErrorInjection, ThreadedEmulatorServer
from mmws_gateway.emulators.hdlc_dlms_emulator import _read_frame, make_hdlc_dlms_handler
from mmws_gateway.protocols import datatypes, dlms, hdlc_dlms
from mmws_gateway.protocols.hdlc import CONTROL_UA, HdlcFrame, control_information_frame
from mmws_gateway.transport import TcpTransport, TransportConfig

SERIAL = "202006003607"
PASSWORD = b"12345678"
LOAD_PROFILE_OBIS = "1.1.63.1.0.ff"
LOAD_PROFILE_CLASS_ID = 7
FROM_DT = datetime(2026, 8, 1)
TO_DT = datetime(2026, 8, 19)


def _row(index: int, value: int) -> tuple[datetime, list[tuple[float, int, int]]]:
    """Строка с единственной колонкой (4 байта, 0 знаков после запятой) —
    метка времени встроена в саму строку (реальный формат, см. модуль)."""
    return FROM_DT + timedelta(minutes=index), [(float(value), 4, 0)]


def _start_server(
    rows: list[tuple[datetime, list[tuple[float, int, int]]]], *, block_size: int
) -> ThreadedEmulatorServer:
    counter = ConnectionCounter()
    handler = make_hdlc_dlms_handler(
        password=PASSWORD,
        obis_values={},
        error_injection=ErrorInjection(),
        counter=counter,
        load_profile_obis=dlms.parse_obis(LOAD_PROFILE_OBIS),
        load_profile_rows=rows,
        load_profile_block_size=block_size,
    )
    return ThreadedEmulatorServer(handler)


def _read_all(server: ThreadedEmulatorServer) -> list:
    config = TransportConfig(host=server.host, port=server.port, timeout_ms=1000, max_retries=1)
    with TcpTransport(config) as transport:
        rows = list(
            hdlc_dlms.read_load_profile(
                transport,
                serial=SERIAL,
                password=PASSWORD,
                obis=LOAD_PROFILE_OBIS,
                class_id=LOAD_PROFILE_CLASS_ID,
                from_dt=FROM_DT,
                to_dt=TO_DT,
            )
        )
    return rows


def test_single_datablock_round_trip():
    """Все строки умещаются в один датаблок (большой block_size) — всё
    равно проходят через код датаблочной ветки (эмулятор Этапа 3 не
    отдаёт GET.response-Normal для профиля нагрузки, см. docstring
    ``_serve_load_profile``)."""
    rows = [_row(h, 1000 + h) for h in range(3)]
    with _start_server(rows, block_size=4096) as server:
        decoded = _read_all(server)

    assert len(decoded) == 3
    for i, (timestamp, values) in enumerate(decoded):
        assert timestamp == FROM_DT + timedelta(minutes=i)
        assert values == [1000 + i]


def test_multi_datablock_round_trip_forces_block_transfer():
    """Маленький block_size гарантированно рвёт закодированные строки на
    несколько датаблоков (в т.ч. посреди одной строки — маркер ``A0 A0``
    может оказаться разрезан пополам между двумя датаблоками) —
    проверяет склейку через GET.request-Next и инкрементальное
    декодирование строк по мере накопления байт (см. hdlc_dlms.
    read_load_profile)."""
    rows = [_row(h, 2000 + h) for h in range(10)]
    encoded_len = sum(len(dlms.encode_load_profile_row(ts, fields)) for ts, fields in rows)
    block_size = 6  # заведомо меньше одной строки — несколько датаблоков на строку
    assert encoded_len > block_size * 3  # сверяем, что тест действительно форсирует блочную передачу

    with _start_server(rows, block_size=block_size) as server:
        decoded = _read_all(server)

    assert len(decoded) == 10
    for i, (timestamp, values) in enumerate(decoded):
        assert timestamp == FROM_DT + timedelta(minutes=i)
        assert values == [2000 + i]


def test_empty_load_profile_returns_no_rows():
    with _start_server([], block_size=4096) as server:
        decoded = _read_all(server)
    assert decoded == []


def test_read_profile_capture_objects_decodes_column_list():
    """2026-09-12 — диагностика причины data-access-error=250 на GET с
    диапазоном (см. DECISIONS.md, "покопай почему"): атрибут 3
    (capture_objects) — обычный GET без access-selection, отдельная
    ассоциация от чтения самого буфера. Проверяет только разбор ответа
    (декодирование ARRAY-of-STRUCTURE) — семантику самих полей (class_id/
    OBIS/attribute_index/data_index) подтвердит только живой счётчик."""
    capture_objects = datatypes.encode_array([
        datatypes.encode_structure([
            datatypes.encode_long_unsigned(8),  # class_id — Clock
            datatypes.encode_octet_string(bytes([0, 0, 1, 0, 0, 0xFF])),  # OBIS 0.0.1.0.0.255
            datatypes.encode_integer(2),  # attribute_index
            datatypes.encode_long_unsigned(0),  # data_index
        ]),
        datatypes.encode_structure([
            datatypes.encode_long_unsigned(3),  # class_id — Register
            datatypes.encode_octet_string(bytes([1, 1, 1, 8, 0, 0xFF])),  # OBIS 1.1.1.8.0.255
            datatypes.encode_integer(2),
            datatypes.encode_long_unsigned(0),
        ]),
    ])
    counter = ConnectionCounter()
    data_values = {dlms.parse_obis(LOAD_PROFILE_OBIS): capture_objects}
    handler = make_hdlc_dlms_handler(
        password=PASSWORD, obis_values={}, error_injection=ErrorInjection(),
        counter=counter, data_values=data_values,
    )
    with ThreadedEmulatorServer(handler) as server:
        config = TransportConfig(host=server.host, port=server.port, timeout_ms=1000, max_retries=1)
        with TcpTransport(config) as transport:
            hdlc_dlms.establish_link(transport, serial=SERIAL)
            decoded = hdlc_dlms.read_profile_capture_objects_via_established_link(
                transport, serial=SERIAL, password=PASSWORD,
                obis=LOAD_PROFILE_OBIS, class_id=LOAD_PROFILE_CLASS_ID,
            )

    assert decoded == [
        [8, bytes([0, 0, 1, 0, 0, 0xFF]), 2, 0],
        [3, bytes([1, 1, 1, 8, 0, 0xFF]), 2, 0],
    ]


def test_read_profile_capture_objects_follows_datablock_transfer():
    """2026-09-12 — живая проверка на реальном счётчике показала, что
    ответ на GET атрибута 3 (capture_objects) приходит НЕ одним PDU
    (GET.response-normal), а через GET.response-with-datablock + GET.
    request-Next (регрессия, найденная сразу после первого деплоя этой
    функции — см. DECISIONS.md). Универсальный эмулятор (``data_values``)
    этого не форсирует (в отличие от ``_serve_load_profile`` для самого
    буфера), поэтому здесь — минимальный ручной сервер, режущий
    закодированный ответ на два датаблока (тот же приём, что и
    ``_run_fake_meter_load_profile`` в test_callhome.py)."""
    capture_objects = datatypes.encode_array([
        datatypes.encode_structure([
            datatypes.encode_long_unsigned(3),
            datatypes.encode_octet_string(bytes([1, 1, 1, 8, 0, 0xFF])),
            datatypes.encode_integer(2),
            datatypes.encode_long_unsigned(0),
        ]),
    ])
    mid = len(capture_objects) // 2
    captured: dict = {}

    def handler(conn: socket.socket) -> None:
        snrm_frame = HdlcFrame.decode(_read_frame(conn))
        ua = HdlcFrame(destination=snrm_frame.source, source=snrm_frame.destination, control=CONTROL_UA)
        conn.sendall(ua.encode())

        aarq_frame = HdlcFrame.decode(_read_frame(conn))
        aare = dlms.build_aare(accepted=True)
        aare_frame = HdlcFrame(
            destination=aarq_frame.source, source=aarq_frame.destination,
            control=control_information_frame(0, 1), information=dlms.wrap_llc_response(aare),
        )
        conn.sendall(aare_frame.encode())

        get_frame = HdlcFrame.decode(_read_frame(conn))
        get_payload = dlms.unwrap_llc(get_frame.information)
        invoke_id = get_payload[2]

        block1 = dlms.build_get_response_datablock(
            invoke_id, last_block=False, block_number=1, raw_data=capture_objects[:mid],
        )
        frame1 = HdlcFrame(
            destination=get_frame.source, source=get_frame.destination,
            control=control_information_frame(1, 2), information=dlms.wrap_llc_response(block1),
        )
        conn.sendall(frame1.encode())

        next_frame = HdlcFrame.decode(_read_frame(conn))
        next_payload = dlms.unwrap_llc(next_frame.information)
        captured["next_invoke_id"] = next_payload[2]
        captured["original_invoke_id"] = invoke_id

        block2 = dlms.build_get_response_datablock(
            invoke_id, last_block=True, block_number=2, raw_data=capture_objects[mid:],
        )
        frame2 = HdlcFrame(
            destination=next_frame.source, source=next_frame.destination,
            control=control_information_frame(2, 3), information=dlms.wrap_llc_response(block2),
        )
        conn.sendall(frame2.encode())

    with ThreadedEmulatorServer(handler) as server:
        config = TransportConfig(host=server.host, port=server.port, timeout_ms=1000, max_retries=1)
        with TcpTransport(config) as transport:
            hdlc_dlms.establish_link(transport, serial=SERIAL)
            decoded = hdlc_dlms.read_profile_capture_objects_via_established_link(
                transport, serial=SERIAL, password=PASSWORD,
                obis=LOAD_PROFILE_OBIS, class_id=LOAD_PROFILE_CLASS_ID,
            )

    assert decoded == [[3, bytes([1, 1, 1, 8, 0, 0xFF]), 2, 0]]
    # 2026-09-12 (реальные байтовые трассы 4 успешных сеансов
    # IECMeterManage.exe, предоставленные пользователем, см. DECISIONS.md)
    # ОПРОВЕРГЛИ более раннюю находку "invoke_id нарастает у Next" —
    # ~130 кадров Get-Request-Next во всех 4 трассах несут ТОТ ЖЕ
    # invoke_id, что и исходный GET.
    assert captured["next_invoke_id"] == captured["original_invoke_id"]


def test_read_load_profile_get_request_next_reuses_invoke_id():
    """2026-09-12 (реальные байтовые трассы 4 успешных сеансов
    IECMeterManage.exe, предоставленные пользователем, см. DECISIONS.md)
    — тот же фикс, что и для capture_objects, но для основного чтения
    буфера (``read_load_profile_via_established_link``). Ручной сервер
    (общий эмулятор ``_serve_load_profile`` не годится — он не проверяет
    и не отдаёт invoke_id из входящего Next), сценарий: GET-диапазон
    (invoke_id=1) -> датаблок 1 -> Next (ожидаем ТОТ ЖЕ invoke_id=1) ->
    датаблок 2. Отдельного GET capture_period больше нет (см. модуль)."""
    rows = [_row(0, 3000), _row(1, 3001)]
    all_bytes = b"".join(dlms.encode_load_profile_row(ts, fields) for ts, fields in rows)
    mid = len(all_bytes) // 2
    captured: dict = {}

    def handler(conn: socket.socket) -> None:
        snrm_frame = HdlcFrame.decode(_read_frame(conn))
        ua = HdlcFrame(destination=snrm_frame.source, source=snrm_frame.destination, control=CONTROL_UA)
        conn.sendall(ua.encode())

        aarq_frame = HdlcFrame.decode(_read_frame(conn))
        aare = dlms.build_aare(accepted=True)
        aare_frame = HdlcFrame(
            destination=aarq_frame.source, source=aarq_frame.destination,
            control=control_information_frame(0, 1), information=dlms.wrap_llc_response(aare),
        )
        conn.sendall(aare_frame.encode())

        range_frame = HdlcFrame.decode(_read_frame(conn))
        range_payload = dlms.unwrap_llc(range_frame.information)
        range_invoke_id = range_payload[2]
        captured["range_invoke_id"] = range_invoke_id

        block1 = dlms.build_get_response_datablock(
            range_invoke_id, last_block=False, block_number=1, raw_data=all_bytes[:mid],
        )
        frame1 = HdlcFrame(
            destination=range_frame.source, source=range_frame.destination,
            control=control_information_frame(1, 2), information=dlms.wrap_llc_response(block1),
        )
        conn.sendall(frame1.encode())

        next_frame = HdlcFrame.decode(_read_frame(conn))
        next_payload = dlms.unwrap_llc(next_frame.information)
        captured["next_invoke_id"] = next_payload[2]

        block2 = dlms.build_get_response_datablock(
            range_invoke_id, last_block=True, block_number=2, raw_data=all_bytes[mid:],
        )
        frame2 = HdlcFrame(
            destination=next_frame.source, source=next_frame.destination,
            control=control_information_frame(2, 3), information=dlms.wrap_llc_response(block2),
        )
        conn.sendall(frame2.encode())

    with ThreadedEmulatorServer(handler) as server:
        config = TransportConfig(host=server.host, port=server.port, timeout_ms=1000, max_retries=1)
        with TcpTransport(config) as transport:
            decoded = list(
                hdlc_dlms.read_load_profile(
                    transport, serial=SERIAL, password=PASSWORD,
                    obis=LOAD_PROFILE_OBIS, class_id=LOAD_PROFILE_CLASS_ID,
                    from_dt=FROM_DT, to_dt=TO_DT,
                )
            )

    assert len(decoded) == 2
    assert captured["next_invoke_id"] == captured["range_invoke_id"]


def test_read_load_profile_retries_next_with_incremented_invoke_id_on_no_long_get():
    """2026-09-12 (см. DECISIONS.md, "может надо отправить хардбит?" —
    живая проверка сразу после этого эксперимента) — часть парка (как
    минимум партии 2018 и 2023 годов) отвечает честным отказом
    data-access-result=16 ("No Long Get Or Read In Progress") на
    константный invoke_id у Next, хотя партия 2020 года (см. тест выше)
    на тот же константный invoke_id отвечает штатно. Разные прошивки —
    разная конвенция. Сценарий: датаблок 1 -> Next(invoke_id=1) ->
    отказ 16 -> ОДИН повтор Next(invoke_id=2) -> датаблок 2 (успех),
    без потери уже собранных строк первого датаблока."""
    rows = [_row(0, 3000), _row(1, 3001)]
    all_bytes = b"".join(dlms.encode_load_profile_row(ts, fields) for ts, fields in rows)
    mid = len(all_bytes) // 2
    captured: dict = {}

    def handler(conn: socket.socket) -> None:
        snrm_frame = HdlcFrame.decode(_read_frame(conn))
        ua = HdlcFrame(destination=snrm_frame.source, source=snrm_frame.destination, control=CONTROL_UA)
        conn.sendall(ua.encode())

        aarq_frame = HdlcFrame.decode(_read_frame(conn))
        aare = dlms.build_aare(accepted=True)
        aare_frame = HdlcFrame(
            destination=aarq_frame.source, source=aarq_frame.destination,
            control=control_information_frame(0, 1), information=dlms.wrap_llc_response(aare),
        )
        conn.sendall(aare_frame.encode())

        range_frame = HdlcFrame.decode(_read_frame(conn))
        range_payload = dlms.unwrap_llc(range_frame.information)
        range_invoke_id = range_payload[2]

        block1 = dlms.build_get_response_datablock(
            range_invoke_id, last_block=False, block_number=1, raw_data=all_bytes[:mid],
        )
        frame1 = HdlcFrame(
            destination=range_frame.source, source=range_frame.destination,
            control=control_information_frame(1, 2), information=dlms.wrap_llc_response(block1),
        )
        conn.sendall(frame1.encode())

        next_frame = HdlcFrame.decode(_read_frame(conn))
        next_payload = dlms.unwrap_llc(next_frame.information)
        captured["first_next_invoke_id"] = next_payload[2]

        # Отказ "No Long Get Or Read In Progress" (code 16) на первый Next.
        error_info = bytes(
            [dlms.GET_RESPONSE_TAG, dlms.GET_RESPONSE_WITH_DATABLOCK, next_payload[2], 0]
        ) + (1).to_bytes(4, "big") + bytes([dlms.DATABLOCK_RESULT_DATA_ACCESS_ERROR, 16])
        error_frame = HdlcFrame(
            destination=next_frame.source, source=next_frame.destination,
            control=control_information_frame(2, 3), information=dlms.wrap_llc_response(error_info),
        )
        conn.sendall(error_frame.encode())

        retry_frame = HdlcFrame.decode(_read_frame(conn))
        retry_payload = dlms.unwrap_llc(retry_frame.information)
        captured["retry_next_invoke_id"] = retry_payload[2]

        block2 = dlms.build_get_response_datablock(
            retry_payload[2], last_block=True, block_number=2, raw_data=all_bytes[mid:],
        )
        frame2 = HdlcFrame(
            destination=retry_frame.source, source=retry_frame.destination,
            control=control_information_frame(3, 4), information=dlms.wrap_llc_response(block2),
        )
        conn.sendall(frame2.encode())

    with ThreadedEmulatorServer(handler) as server:
        config = TransportConfig(host=server.host, port=server.port, timeout_ms=1000, max_retries=1)
        with TcpTransport(config) as transport:
            decoded = list(
                hdlc_dlms.read_load_profile(
                    transport, serial=SERIAL, password=PASSWORD,
                    obis=LOAD_PROFILE_OBIS, class_id=LOAD_PROFILE_CLASS_ID,
                    from_dt=FROM_DT, to_dt=TO_DT,
                )
            )

    assert len(decoded) == 2
    assert decoded[0][1] == [3000]
    assert decoded[1][1] == [3001]
    assert captured["retry_next_invoke_id"] == captured["first_next_invoke_id"] + 1
