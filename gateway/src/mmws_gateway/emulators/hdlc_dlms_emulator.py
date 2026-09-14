"""Программный эмулятор счётчика с профилем HDLC + DLMS/COSEM.

Это ЭМУЛЯТОР для интеграционных тестов Gateway, а не замена реальным
полевым испытаниям (Promt_MMWS.md, раздел «Этап 0», п. 4).
"""

from __future__ import annotations

import socket
import time

from ..protocols import datatypes, dlms
from ..protocols.hdlc import (
    CONTROL_SNRM,
    CONTROL_UA,
    FLAG,
    HdlcFrame,
    control_information_frame,
)
from .common import ConnectionCounter, ErrorInjection, recv_exact, recv_until

_TIMEOUT_STALL_S = 0.3
OBJECT_UNDEFINED = 9  # data-access-result: object-undefined (Green Book)


def make_hdlc_dlms_handler(
    *,
    password: bytes,
    obis_values: dict[bytes, int],
    error_injection: ErrorInjection,
    counter: ConnectionCounter,
    data_values: dict[bytes, bytes] | None = None,
    load_profile_obis: bytes | None = None,
    load_profile_rows: list[tuple[object, list[tuple[float, int, int]]]] | None = None,
    load_profile_block_size: int = 40,
    register_scalers: dict[bytes, int] | None = None,
    register_units: dict[bytes, int] | None = None,
    action_state: dict[bytes, int] | None = None,
    action_parameters_state: dict[bytes, bytes | None] | None = None,
    action_force_result: int | None = None,
):
    """Возвращает обработчик TCP-подключения для ``ThreadedEmulatorServer``.

    ``data_values`` (Этап 2) — состояние class-1 (Data) объектов вроде
    «Current Time»/«Current Date» (словарь OBIS, лист RW_Tree_параметры):
    ключ — OBIS, значение — уже закодированные байты (тег+длина+
    содержимое). SET обновляет запись, GET читает — общий изменяемый
    словарь, переданный вызывающим тестом, чтобы проверить, что именно
    было записано.

    ``load_profile_obis``/``load_profile_rows`` (Этап 3, переработано
    2026-09-12 — см. DECISIONS.md, реальные байтовые трассы 4 успешных
    сеансов IECMeterManage.exe) — эмуляция буфера профиля нагрузки:
    каждый элемент ``load_profile_rows`` — ``(timestamp, fields)``,
    закодированные через ``dlms.encode_load_profile_row`` (собственный
    BCD-формат этой модели счётчика — метка времени встроена в каждую
    строку, отдельного GET атрибута 4/capture_period больше нет).
    Отдаётся ВСЕГДА через датаблоки (по ``load_profile_block_size`` байт
    на датаблок — специально маленький по умолчанию, чтобы гарантированно
    проверить склейку нескольких датаблоков в тестах), не через
    GET.response-Normal. Реальная фильтрация по диапазону дат не
    эмулируется — отдаются все строки целиком, диапазон в запросе не
    проверяется (эмулятор нужен для проверки МЕХАНИЗМА блочной
    передачи, не бизнес-логики счётчика).

    ``action_state`` (Этап 5) — если задан, каждый обработанный
    ACTION.request записывает в него ``{obis: method_id}``, чтобы тест
    мог проверить, что именно было вызвано (remote_disconnect/
    remote_reconnect). ``action_force_result`` — принудительный код
    Action-Result в ответе (для проверки обработки отказа).

    ``register_scalers``/``register_units`` (2026-08-19/2026-09-07, см.
    ``hdlc_dlms.read_register_via_established_link``) — эмулятор
    отвечает на GET атрибута 3 (scaler_unit, читается ПЕРЕД value)
    структурой `{scaler: register_scalers.get(obis, 0), unit:
    register_units.get(obis, 0)}` — по умолчанию scaler=0, unit=0 (не
    меняет поведение старых тестов, которые задают сырые значения без
    масштаба; unit=0 — не Wh, доп. перевод /1000 не применяется)."""

    def handler(conn: socket.socket) -> None:
        conn.settimeout(5)
        attempt = counter.next()

        if error_injection.force_timeout:
            time.sleep(_TIMEOUT_STALL_S)
            return

        serve_hdlc_dlms_session(
            conn,
            password=password,
            obis_values=obis_values,
            error_injection=error_injection,
            attempt=attempt,
            data_values=data_values,
            load_profile_obis=load_profile_obis,
            load_profile_rows=load_profile_rows,
            load_profile_block_size=load_profile_block_size,
            register_scalers=register_scalers,
            register_units=register_units,
            action_state=action_state,
            action_parameters_state=action_parameters_state,
            action_force_result=action_force_result,
        )

    return handler


def serve_hdlc_dlms_session(
    conn: socket.socket,
    *,
    password: bytes,
    obis_values: dict[bytes, int],
    error_injection: ErrorInjection,
    attempt: int,
    data_values: dict[bytes, bytes] | None = None,
    load_profile_obis: bytes | None = None,
    load_profile_rows: list[tuple[object, list[tuple[float, int, int]]]] | None = None,
    load_profile_block_size: int = 40,
    register_scalers: dict[bytes, int] | None = None,
    register_units: dict[bytes, int] | None = None,
    action_state: dict[bytes, int] | None = None,
    action_parameters_state: dict[bytes, bytes | None] | None = None,
    action_force_result: int | None = None,
) -> None:
    """Обслуживает установление HDLC-соединения, AARQ/AARE и один GET либо SET
    (либо — если настроен ``load_profile_obis`` и запрос его затрагивает —
    серию GET.request-Next/датаблоков для профиля нагрузки).

    Вынесено отдельной функцией, чтобы её мог переиспользовать эмулятор
    режима E (``mode_e_emulator``) после собственной идентификационной
    преамбулы IEC 62056-21.
    """
    snrm = HdlcFrame.decode(_read_frame(conn))
    if snrm.control != CONTROL_SNRM:
        return
    ua = HdlcFrame(destination=snrm.source, source=snrm.destination, control=CONTROL_UA)
    conn.sendall(ua.encode())

    aarq_frame = HdlcFrame.decode(_read_frame(conn))
    parsed_aarq = dlms.parse_aarq(dlms.unwrap_llc(aarq_frame.information))
    accepted = (not error_injection.force_auth_fail) and parsed_aarq.password == password
    aare = dlms.build_aare(accepted=accepted)
    aare_frame = HdlcFrame(
        destination=aarq_frame.source,
        source=aarq_frame.destination,
        control=control_information_frame(0, 1),
        information=dlms.wrap_llc_response(aare),
    )
    conn.sendall(aare_frame.encode())
    if not accepted:
        return

    req_frame = HdlcFrame.decode(_read_frame(conn))
    payload = dlms.unwrap_llc(req_frame.information)
    tag = payload[0] if payload else None

    has_access_selection = len(payload) > 12 and payload[12] == 0x01
    is_load_profile_obis = (
        load_profile_obis is not None
        and tag == dlms.GET_REQUEST_TAG
        and payload[5:11] == load_profile_obis
    )
    if is_load_profile_obis and has_access_selection:
        _serve_load_profile(
            conn, req_frame, invoke_id=payload[2],
            rows=load_profile_rows or [], block_size=load_profile_block_size,
        )
        return

    if tag == dlms.ACTION_REQUEST_TAG:
        action_request = dlms.parse_action_request(payload)
        if action_state is not None:
            action_state[action_request.obis] = action_request.method_id
        if action_parameters_state is not None:
            action_parameters_state[action_request.obis] = action_request.parameters
        result = dlms.ACTION_RESULT_SUCCESS if action_force_result is None else action_force_result
        info = dlms.build_action_response(action_request.invoke_id, result=result)
    elif tag == dlms.SET_REQUEST_TAG:
        set_request = dlms.parse_set_request(payload)
        if data_values is not None:
            data_values[set_request.obis] = set_request.encoded_value
        info = dlms.build_set_response(set_request.invoke_id)
    else:
        get_request = dlms.parse_get_request(payload)
        if (
            get_request.class_id == dlms.REGISTER_CLASS_ID
            and get_request.attribute_id == dlms.REGISTER_SCALER_UNIT_ATTRIBUTE
        ):
            # scaler_unit (атрибут 3) запрашивается ПЕРВЫМ, ДО value —
            # см. ``hdlc_dlms.read_register_via_established_link``
            # (2026-09-07: порядок запросов приведён в соответствие с
            # подтверждённым реальным трафиком легитимного заводского
            # клиента, который тоже читает scaler_unit перед value).
            _serve_register_scaler_then_value(
                conn, req_frame, get_request,
                obis_values=obis_values, register_scalers=register_scalers or {},
                register_units=register_units or {},
                error_injection=error_injection, attempt=attempt,
            )
            return
        if get_request.class_id == dlms.REGISTER_CLASS_ID:
            value = obis_values.get(get_request.obis)
            info = (
                dlms.build_get_response_data(
                    get_request.invoke_id, datatypes.encode_double_long_unsigned(value)
                )
                if value is not None
                else dlms.build_get_response_error(get_request.invoke_id, OBJECT_UNDEFINED)
            )
        else:
            encoded = (data_values or {}).get(get_request.obis)
            info = (
                dlms.build_get_response_data(get_request.invoke_id, encoded)
                if encoded is not None
                else dlms.build_get_response_error(get_request.invoke_id, OBJECT_UNDEFINED)
            )

    response_frame = HdlcFrame(
        destination=req_frame.source,
        source=req_frame.destination,
        control=control_information_frame(1, 2),
        information=dlms.wrap_llc_response(info),
    )
    encoded = response_frame.encode()

    if error_injection.force_crc_error and attempt <= error_injection.fail_attempts:
        corrupted = bytearray(encoded)
        corrupted[-3] ^= 0xFF  # портим младший байт FCS перед закрывающим флагом
        conn.sendall(bytes(corrupted))
        return

    if error_injection.force_partial_disconnect and attempt <= error_injection.fail_attempts:
        conn.sendall(encoded[: len(encoded) // 2])
        return

    conn.sendall(encoded)


def _serve_register_scaler_then_value(
    conn: socket.socket,
    scaler_req_frame: HdlcFrame,
    scaler_request: object,
    *,
    obis_values: dict[bytes, int],
    register_scalers: dict[bytes, int],
    register_units: dict[bytes, int],
    error_injection: ErrorInjection,
    attempt: int,
) -> None:
    """Отвечает на GET атрибута 3 (scaler_unit) объекта класса Register,
    затем дожидается и обслуживает следующий GET — атрибута 2 (value),
    возможно по ДРУГОМУ OBIS (``dlms.VALUE_OBIS_OVERRIDES``, вендорская
    особенность Risesun DTZY217). Порядок «scaler_unit, затем value»
    соответствует ``hdlc_dlms.read_register_via_established_link``
    (2026-09-07). ``error_injection`` применяется к этому, ПЕРВОМУ
    отправляемому серверу кадру — как и раньше, когда первым (и тогда
    единственным) кадром был ответ на value."""
    scaler = register_scalers.get(scaler_request.obis, 0)
    unit = register_units.get(scaler_request.obis, 0)
    scaler_value = datatypes.encode_structure(
        [datatypes.encode_integer(scaler), datatypes.encode_unsigned(unit)]
    )
    info = dlms.build_get_response_data(scaler_request.invoke_id, scaler_value)
    response_frame = HdlcFrame(
        destination=scaler_req_frame.source,
        source=scaler_req_frame.destination,
        control=control_information_frame(1, 2),
        information=dlms.wrap_llc_response(info),
    )
    encoded = response_frame.encode()

    if error_injection.force_crc_error and attempt <= error_injection.fail_attempts:
        corrupted = bytearray(encoded)
        corrupted[-3] ^= 0xFF  # портим младший байт FCS перед закрывающим флагом
        conn.sendall(bytes(corrupted))
        return

    if error_injection.force_partial_disconnect and attempt <= error_injection.fail_attempts:
        conn.sendall(encoded[: len(encoded) // 2])
        return

    conn.sendall(encoded)

    value_frame = HdlcFrame.decode(_read_frame(conn))
    value_payload = dlms.unwrap_llc(value_frame.information)
    value_request = dlms.parse_get_request(value_payload)
    value = obis_values.get(value_request.obis)
    value_info = (
        dlms.build_get_response_data(
            value_request.invoke_id, datatypes.encode_double_long_unsigned(value)
        )
        if value is not None
        else dlms.build_get_response_error(value_request.invoke_id, OBJECT_UNDEFINED)
    )
    value_response_frame = HdlcFrame(
        destination=value_frame.source,
        source=value_frame.destination,
        control=control_information_frame(2, 3),
        information=dlms.wrap_llc_response(value_info),
    )
    conn.sendall(value_response_frame.encode())


def _serve_load_profile(
    conn: socket.socket,
    req_frame: HdlcFrame,
    *,
    invoke_id: int,
    rows: list[tuple[object, list[tuple[float, int, int]]]],
    block_size: int,
    send_seq: int = 1,
    recv_seq: int = 2,
) -> None:
    """Отдаёт настроенные строки профиля нагрузки ВСЕГДА через серию
    GET.response-with-datablock (даже если всё уместилось бы в одном
    PDU) — намеренно упрощённая, но специально проверяющая механизм
    склейки нескольких датаблоков и GET.request-Next в
    ``hdlc_dlms.read_load_profile``. ``send_seq``/``recv_seq`` — с какого
    номера кадра начинать (по умолчанию сразу после AARE — 1,2).

    2026-09-12 — строки кодируются собственным BCD-форматом этой модели
    счётчика (``dlms.encode_load_profile_row``, маркер ``A0 A0`` +
    встроенная метка времени), не стандартным DLMS array-of-structure
    (см. DECISIONS.md)."""
    all_bytes = b"".join(dlms.encode_load_profile_row(ts, fields) for ts, fields in rows)

    offset = 0
    block_number = 0
    while True:
        block_number += 1
        chunk = all_bytes[offset : offset + block_size]
        offset += len(chunk)
        last_block = offset >= len(all_bytes)

        info = dlms.build_get_response_datablock(
            invoke_id, last_block=last_block, block_number=block_number, raw_data=chunk
        )
        response_frame = HdlcFrame(
            destination=req_frame.source,
            source=req_frame.destination,
            control=control_information_frame(send_seq, recv_seq),
            information=dlms.wrap_llc_response(info),
        )
        conn.sendall(response_frame.encode())
        send_seq += 1

        if last_block:
            return

        next_frame = HdlcFrame.decode(_read_frame(conn))
        req_frame = next_frame
        recv_seq += 1


def _read_frame(conn: socket.socket) -> bytes:
    """Читает кадр по длине из Frame Format, не сканированием на 0x7E —
    см. подробное обоснование в ``protocols.hdlc.read_frame_from_transport``
    (тот же баг воспроизводится и на серверной стороне эмулятора)."""
    first = recv_exact(conn, 1)
    if first != bytes([FLAG]):
        raise ConnectionError("Ожидался открывающий флаг HDLC")
    frame_format = recv_exact(conn, 2)
    declared_len = int.from_bytes(frame_format, "big") & 0x07FF
    rest = recv_exact(conn, declared_len - 2 + 1)
    return first + frame_format + rest
