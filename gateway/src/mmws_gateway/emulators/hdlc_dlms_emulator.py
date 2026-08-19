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
    load_profile_rows: list[list[bytes]] | None = None,
    load_profile_block_size: int = 40,
):
    """Возвращает обработчик TCP-подключения для ``ThreadedEmulatorServer``.

    ``data_values`` (Этап 2) — состояние class-1 (Data) объектов вроде
    «Current Time»/«Current Date» (словарь OBIS, лист RW_Tree_параметры):
    ключ — OBIS, значение — уже закодированные байты (тег+длина+
    содержимое). SET обновляет запись, GET читает — общий изменяемый
    словарь, переданный вызывающим тестом, чтобы проверить, что именно
    было записано.

    ``load_profile_obis``/``load_profile_rows`` (Этап 3) — эмуляция
    буфера профиля нагрузки: каждая строка ``load_profile_rows`` — уже
    закодированный список колонок (см. ``datatypes.encode_*``),
    оборачивается в structure и отдаётся ВСЕГДА через датаблоки (по
    ``load_profile_block_size`` байт на датаблок — специально маленький
    по умолчанию, чтобы гарантированно проверить склейку нескольких
    датаблоков в тестах), не через GET.response-Normal. Реальная
    фильтрация по диапазону дат не эмулируется — отдаются все строки
    целиком, диапазон в запросе не проверяется (эмулятор нужен для
    проверки МЕХАНИЗМА блочной передачи, не бизнес-логики счётчика)."""

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
    load_profile_rows: list[list[bytes]] | None = None,
    load_profile_block_size: int = 40,
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
    if (
        load_profile_obis is not None
        and tag == dlms.GET_REQUEST_TAG
        and has_access_selection
        and payload[5:11] == load_profile_obis
    ):
        _serve_load_profile(
            conn, req_frame, invoke_id=payload[2],
            rows=load_profile_rows or [], block_size=load_profile_block_size,
        )
        return

    if tag == dlms.SET_REQUEST_TAG:
        set_request = dlms.parse_set_request(payload)
        if data_values is not None:
            data_values[set_request.obis] = set_request.encoded_value
        info = dlms.build_set_response(set_request.invoke_id)
    else:
        get_request = dlms.parse_get_request(payload)
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


def _serve_load_profile(
    conn: socket.socket,
    req_frame: HdlcFrame,
    *,
    invoke_id: int,
    rows: list[list[bytes]],
    block_size: int,
) -> None:
    """Отдаёт настроенные строки профиля нагрузки ВСЕГДА через серию
    GET.response-with-datablock (даже если всё уместилось бы в одном
    PDU) — намеренно упрощённая, но специально проверяющая механизм
    склейки нескольких датаблоков и GET.request-Next в
    ``hdlc_dlms.read_load_profile``."""
    array_bytes = datatypes.encode_array(
        [datatypes.encode_structure(row) for row in rows]
    )

    send_seq = 1
    recv_seq = 2
    offset = 0
    block_number = 0
    while True:
        block_number += 1
        chunk = array_bytes[offset : offset + block_size]
        offset += len(chunk)
        last_block = offset >= len(array_bytes)

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
