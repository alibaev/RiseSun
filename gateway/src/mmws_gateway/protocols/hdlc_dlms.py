"""Протокольный профиль «HDLC + DLMS/COSEM» (ТЗ п. 4.3.2, третий профиль).

Последовательность одной операции чтения:
    1. SNRM -> UA (установление HDLC-соединения);
    2. AARQ -> AARE в I-кадрах (установление ассоциации DLMS, пароль
       низкого уровня безопасности — ТЗ п. 4.3.3);
    3. GET.request -> GET.response над COSEM Register (класс 3),
       атрибут 2 (value), адресуемым запрошенным OBIS-кодом.

Адресация и LLC-обёртка информационного поля проверены на реальном
оборудовании Risesun 2026-08-18 (см. DECISIONS.md) — см. docstring
``hdlc.py`` и ``dlms.py``.
"""

from __future__ import annotations

import logging

from dataclasses import dataclass
from datetime import timedelta
from typing import Iterator

from ..addressing import HDLC_DLMS, physical_address
from ..errors import ConnectionLostError, GatewayError, MeterTimeoutError
from ..transport import TcpTransport
from . import datatypes, dlms
from .datatypes import DlmsDataError
from .hdlc import (
    CONTROL_SNRM,
    CONTROL_UA,
    DEFAULT_CLIENT_ADDRESS,
    SNRM_PARAMETER_NEGOTIATION,
    HdlcFrame,
    control_information_frame,
    read_frame_from_transport,
    server_hdlc_address,
)

logger = logging.getLogger("mmws_gateway.hdlc_dlms")


def read_register(
    transport: TcpTransport,
    *,
    serial: str,
    password: bytes,
    obis: str,
    class_id: int = dlms.REGISTER_CLASS_ID,
) -> object:
    establish_link(transport, serial=serial)
    return read_register_via_established_link(
        transport, serial=serial, password=password, obis=obis, class_id=class_id
    )


def establish_link(transport: TcpTransport, *, serial: str) -> None:
    """Только шаг SNRM -> UA — используется отдельно фоновым
    "прогревом" call-home соединений (см. ``callhome.py``), которые
    держат HDLC-связь установленной заранее, до того как понадобится
    реальное чтение регистра."""
    server_addr = server_hdlc_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS
    _establish_link(transport, server_addr, client_addr)


def read_register_via_established_link(
    transport: TcpTransport,
    *,
    serial: str,
    password: bytes,
    obis: str,
    class_id: int = dlms.REGISTER_CLASS_ID,
    mechanism_id: int = 1,
) -> object:
    """AARQ/AARE + GET поверх УЖЕ установленной (SNRM/UA пройден) HDLC-связи.

    Для объектов класса Register (3) значение атрибута 2 — это СЫРОЕ
    целое число, применить масштаб (атрибут 3, scaler_unit) обязан
    именно Gateway (Backend не реализует протокольную логику,
    Promt_MMWS.md, раздел 3, принцип 1) — иначе, например, показание
    4507.70 отдаётся как 450770 (найденный баг, см. dlms.py). Если
    объект вообще не поддерживает scaler_unit (data-access-error на
    GET атрибута 3 — актуально для параметров класса Data, обычно
    читаемых через этот же путь с явно переданным class_id=1),
    возвращается сырое значение без изменений.

    Для отдельных регистров (см. ``dlms.VALUE_OBIS_OVERRIDES``) сам
    атрибут 2 (value) читается по ДРУГОМУ OBIS, чем переданный ``obis``
    — вендорская особенность Risesun DTZY217, подтверждённая реальным
    трафиком (см. dlms.py). scaler_unit при этом всегда запрашивается
    по исходному, переданному ``obis`` — именно там он подтверждённо
    доступен.

    Порядок GET-запросов: scaler_unit (атрибут 3) читается ПЕРВЫМ,
    value (атрибут 2) — ВТОРЫМ. Это не произвольный выбор: легитимный
    заводской клиент на реальном трафике Risesun (см. dlms.py, докстринг
    у ``REGISTER_SCALER_UNIT_ATTRIBUTE``) делает ровно так же —
    отдельный GET scaler_unit ПЕРЕД value. Прежняя реализация читала их
    в обратном порядке (value первым); показания на реальных счётчиках
    (2026-09-07, массовая активация 141 счётчика) стабильно приходили
    сырыми, без применения масштаба, при том что тот же механизм на
    эмуляторе (порядок запросов эмулятору безразличен) давал корректный
    результат — то есть на реальном железе именно ПОРЯДОК запросов
    оказывался значим, а не сама формула масштабирования."""
    server_addr = server_hdlc_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS

    aarq = dlms.build_aarq(password, mechanism_id=mechanism_id)
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=0, recv_seq=0,
        information=dlms.wrap_llc_command(aarq),
    )
    aare_frame = _recv_i_frame(transport)
    dlms.parse_aare(dlms.unwrap_llc(aare_frame.information))  # бросает AuthFailedError при отказе

    value, _next_send_seq, error = _read_one_register_via_established_link(
        transport, server_addr, client_addr, send_seq=1, obis=obis, class_id=class_id,
    )
    if error is not None:
        raise error
    return value


def _read_one_register_via_established_link(
    transport: TcpTransport,
    server_addr: int,
    client_addr: int,
    *,
    send_seq: int,
    obis: str,
    class_id: int,
) -> tuple[object | None, int, GatewayError | None]:
    """Тело GET одного регистра (scaler_unit + value, см. докстринг
    ``read_register_via_established_link`` про порядок и вендорскую
    особенность Risesun) — вынесено отдельно (2026-09-09, событийное
    чтение call-home, см. DECISIONS.md и план ticklish-popping-bear.md),
    чтобы читать НЕСКОЛЬКО регистров за одну ассоциацию (см.
    ``read_registers_via_established_link``), продолжая нумерацию
    HDLC-кадров через все них, а не начиная её заново на каждый GET.

    Возвращает ``(значение, следующий свободный send_seq, ошибка)`` —
    ошибка на GET самого значения (напр. data-access-error: счётчик
    ОТВЕТИЛ, просто отказом для этого атрибута) возвращается как
    значение кортежа, а НЕ бросается исключением: HDLC N(S)/N(R) при
    этом продвинулись штатно (ответ реально получен), вызывающему
    (``read_registers_via_established_link``) нужен корректный
    следующий ``send_seq`` ДАЖЕ при такой ошибке, чтобы не
    рассинхронизировать нумерацию кадров для следующего OBIS на той же
    ассоциации. Обрыв соединения (``ConnectionError``/``OSError``) НЕ
    перехватывается — это уже не восстановимо в рамках текущей
    ассоциации, распространяется вызывающему как есть."""
    parsed_obis = dlms.parse_obis(obis)
    value_obis = dlms.VALUE_OBIS_OVERRIDES.get(obis, obis)
    parsed_value_obis = dlms.parse_obis(value_obis) if value_obis != obis else parsed_obis

    scaler_unit: object = None
    if class_id == dlms.REGISTER_CLASS_ID:
        scaler_request = dlms.build_get_request(
            parsed_obis, class_id=class_id, attribute_id=dlms.REGISTER_SCALER_UNIT_ATTRIBUTE,
        )
        _send_i_frame(
            transport, server_addr, client_addr, send_seq=send_seq, recv_seq=send_seq,
            information=dlms.wrap_llc_command(scaler_request),
        )
        scaler_frame = _recv_i_frame(transport)
        try:
            scaler_unit = dlms.parse_get_response(dlms.unwrap_llc(scaler_frame.information))
        except GatewayError as exc:
            logger.warning(
                "Register %s: GET scaler_unit не удался (%s) — значение (OBIS %s) "
                "будет возвращено без применения масштаба",
                obis, exc.code, value_obis,
            )
            scaler_unit = None

    value_send_seq = send_seq + 1 if class_id == dlms.REGISTER_CLASS_ID else send_seq
    request = dlms.build_get_request(parsed_value_obis, class_id=class_id)
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=value_send_seq, recv_seq=value_send_seq,
        information=dlms.wrap_llc_command(request),
    )
    response_frame = _recv_i_frame(transport)
    next_send_seq = value_send_seq + 1
    try:
        raw_value = dlms.parse_get_response(dlms.unwrap_llc(response_frame.information))
    except GatewayError as exc:
        return None, next_send_seq, exc

    if class_id != dlms.REGISTER_CLASS_ID or not isinstance(raw_value, (int, float)):
        return raw_value, next_send_seq, None

    if not (isinstance(scaler_unit, list) and len(scaler_unit) == 2 and isinstance(scaler_unit[0], int)):
        if scaler_unit is not None:
            logger.warning(
                "Register %s (value read at %s): scaler_unit имеет неожиданный вид %r — "
                "возвращается сырое значение %r без применения масштаба",
                obis, value_obis, scaler_unit, raw_value,
            )
        return raw_value, next_send_seq, None
    scaler, unit = scaler_unit[0], scaler_unit[1]
    # unit=30 (Wh, Green Book) — единственная подтверждённая реальным
    # трафиком Risesun DTZY217 единица для этого регистра (см.
    # dlms.UNIT_WATT_HOUR). Везде в проекте энергия отображается в
    # кВт·ч (OBIS.xlsx), поэтому Вт·ч требует ДОПОЛНИТЕЛЬНОГО перевода
    # /1000 — эквивалентно вычитанию 3 из показателя степени scaler.
    # Найденный баг (2026-09-07, сообщение пользователя): без этого
    # перевода `raw_value * 10**scaler` давало число В 1000 РАЗ БОЛЬШЕ
    # правильного (напр. 132354 при scaler=1 превращалось в 1323540
    # вместо верных 1323.54) — то есть формула была неполной даже после
    # применения самого scaler, не только при его отсутствии/сбое.
    effective_scaler = scaler - 3 if unit == dlms.UNIT_WATT_HOUR else scaler
    if effective_scaler == 0:
        return raw_value, next_send_seq, None
    scaled = round(raw_value * (10**effective_scaler), max(0, -effective_scaler))
    logger.info(
        "Register %s (value read at %s): scaler=%d unit=%d (эффективный показатель степени=%d), %r -> %r",
        obis, value_obis, scaler, unit, effective_scaler, raw_value, scaled,
    )
    return scaled, next_send_seq, None


@dataclass
class RegisterReadOutcome:
    obis: str
    ok: bool
    value: object | None = None
    error: GatewayError | None = None


def read_registers_via_established_link(
    transport: TcpTransport,
    *,
    serial: str,
    password: bytes,
    obis_specs: list[tuple[str, int]],
) -> list[RegisterReadOutcome]:
    """AARQ/AARE ОДИН РАЗ, затем последовательно GET на каждый (obis,
    class_id) из ``obis_specs`` — событийное чтение call-home сразу при
    подключении (2026-09-09, см. DECISIONS.md и план
    ticklish-popping-bear.md): вместо отдельной ассоциации на каждый
    OBIS (как исторически делал ``read_via_call_home``, по одному
    held-соединению на попытку) читает ВСЕ due-OBIS счётчика за одну
    ассоциацию, аналог ``GXDLMSReader.ExecuteMeterTasks`` из
    легаси-референса (один ``InitializeConnection`` + цикл ``Read()``
    по объектам).

    ``GatewayError``, возвращённая через кортеж из
    ``_read_one_register_via_established_link`` (data-access-error и
    т.п. — счётчик ОТВЕТИЛ, просто отказом для этого атрибута), НЕ
    прерывает цикл — HDLC N(S)/N(R) остались синхронны, безопасно
    продолжать со следующим OBIS. А вот ``ConnectionLostError``/
    ``MeterTimeoutError`` (тоже подклассы ``GatewayError``, но
    БРОШЕННЫЕ, а не возвращённые — доходят из ``_recv_i_frame``/
    ``_send_i_frame`` транспортного уровня, реального ответа не было
    вовсе) вместе с обычными ``ConnectionError``/``OSError`` прерывают
    цикл — соединение более не пригодно для следующего GET. Уже
    собранные до этого момента результаты возвращаются вызывающему
    (частичный успех), а не теряются в брошенном исключении."""
    server_addr = server_hdlc_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS

    aarq = dlms.build_aarq(password)
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=0, recv_seq=0,
        information=dlms.wrap_llc_command(aarq),
    )
    aare_frame = _recv_i_frame(transport)
    dlms.parse_aare(dlms.unwrap_llc(aare_frame.information))  # AuthFailedError и т.п. — весь батч падает, это верно

    results: list[RegisterReadOutcome] = []
    send_seq = 1
    for obis, class_id in obis_specs:
        try:
            value, send_seq, error = _read_one_register_via_established_link(
                transport, server_addr, client_addr, send_seq=send_seq, obis=obis, class_id=class_id,
            )
        except (ConnectionLostError, MeterTimeoutError, ConnectionError, OSError):
            break
        if error is not None:
            results.append(RegisterReadOutcome(obis=obis, ok=False, error=error))
        else:
            results.append(RegisterReadOutcome(obis=obis, ok=True, value=value))
    return results


def write_register(
    transport: TcpTransport,
    *,
    serial: str,
    password: bytes,
    obis: str,
    encoded_value: bytes,
    class_id: int = dlms.REGISTER_CLASS_ID,
) -> None:
    """Запись параметра (Этап 2, ТЗ п. 4.2.4) — SNRM/UA + AARQ/AARE + SET.
    ``encoded_value`` — уже закодированное Common-Data-Type значение (см.
    ``protocols.datatypes.encode_*``); ничего не возвращает, бросает
    GatewayError при отказе (в т.ч. AuthFailedError)."""
    establish_link(transport, serial=serial)
    write_register_via_established_link(
        transport, serial=serial, password=password, obis=obis,
        encoded_value=encoded_value, class_id=class_id,
    )


def write_register_via_established_link(
    transport: TcpTransport,
    *,
    serial: str,
    password: bytes,
    obis: str,
    encoded_value: bytes,
    class_id: int = dlms.REGISTER_CLASS_ID,
) -> None:
    """AARQ/AARE + SET поверх УЖЕ установленной (SNRM/UA пройден) HDLC-связи."""
    server_addr = server_hdlc_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS

    aarq = dlms.build_aarq(password)
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=0, recv_seq=0,
        information=dlms.wrap_llc_command(aarq),
    )
    aare_frame = _recv_i_frame(transport)
    dlms.parse_aare(dlms.unwrap_llc(aare_frame.information))

    request = dlms.build_set_request(dlms.parse_obis(obis), encoded_value, class_id=class_id)
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=1, recv_seq=1,
        information=dlms.wrap_llc_command(request),
    )
    response_frame = _recv_i_frame(transport)
    dlms.parse_set_response(dlms.unwrap_llc(response_frame.information))


def execute_action(
    transport: TcpTransport,
    *,
    serial: str,
    password: bytes,
    obis: str,
    method_id: int,
    class_id: int,
) -> None:
    """Удалённое отключение/подключение счётчика (Этап 5, ТЗ п.4.2.10) —
    SNRM/UA + AARQ/AARE + ACTION. Ничего не возвращает, бросает
    GatewayError при отказе (в т.ч. AuthFailedError)."""
    establish_link(transport, serial=serial)
    execute_action_via_established_link(
        transport, serial=serial, password=password, obis=obis, method_id=method_id, class_id=class_id,
    )


def execute_action_via_established_link(
    transport: TcpTransport,
    *,
    serial: str,
    password: bytes,
    obis: str,
    method_id: int,
    class_id: int,
) -> None:
    """AARQ/AARE + ACTION поверх УЖЕ установленной (SNRM/UA пройден) HDLC-связи."""
    server_addr = server_hdlc_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS

    aarq = dlms.build_aarq(password)
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=0, recv_seq=0,
        information=dlms.wrap_llc_command(aarq),
    )
    aare_frame = _recv_i_frame(transport)
    dlms.parse_aare(dlms.unwrap_llc(aare_frame.information))

    request = dlms.build_action_request(dlms.parse_obis(obis), method_id, class_id=class_id)
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=1, recv_seq=1,
        information=dlms.wrap_llc_command(request),
    )
    response_frame = _recv_i_frame(transport)
    dlms.parse_action_response(dlms.unwrap_llc(response_frame.information))


def _establish_link(transport: TcpTransport, server_addr: int, client_addr: int) -> None:
    frame = HdlcFrame(
        destination=server_addr,
        source=client_addr,
        control=CONTROL_SNRM,
        information=SNRM_PARAMETER_NEGOTIATION,
    )
    transport.send(frame.encode())
    response = HdlcFrame.decode(read_frame_from_transport(transport))
    logger.info(
        "Ответ на SNRM: control=0x%02x src=%d dst=%d info=%s",
        response.control, response.source, response.destination, response.information.hex(),
    )
    if response.control != CONTROL_UA:
        raise GatewayError(
            "Счётчик не подтвердил установление HDLC-соединения (ожидался управляющий байт UA)"
        )


def _send_i_frame(
    transport: TcpTransport,
    server_addr: int,
    client_addr: int,
    *,
    send_seq: int,
    recv_seq: int,
    information: bytes,
) -> None:
    control = control_information_frame(send_seq, recv_seq)
    frame = HdlcFrame(
        destination=server_addr, source=client_addr, control=control, information=information
    )
    transport.send(frame.encode())


_MAX_SUPERVISORY_FRAMES_SKIPPED = 20


def _recv_i_frame(transport: TcpTransport) -> HdlcFrame:
    """Ждёт информационный (I-) кадр, пропуская супервизорные S-кадры
    (напр. RR — подтверждение приёма без данных). Подтверждено на
    реальном оборудовании 2026-08-18: счётчик сразу же отвечает RR-
    подтверждением на присланный AARQ (control такого кадра — нечётный,
    ``information`` пуст), а сам AARE в виде настоящего I-кадра приходит
    отдельным, следующим кадром — если считать первым же полученным
    кадром сразу ответ приложения, то это RR-подтверждение ошибочно
    принимается за AARE с пустыми (некорректными) LLC-данными."""
    for _ in range(_MAX_SUPERVISORY_FRAMES_SKIPPED):
        frame = HdlcFrame.decode(read_frame_from_transport(transport))
        logger.info(
            "Получен HDLC-кадр: control=0x%02x src=%d dst=%d info=%s",
            frame.control, frame.source, frame.destination, frame.information.hex(),
        )
        if frame.control & 0x01 == 0:  # I-кадр — control_information_frame() всегда даёт чётный control
            return frame
    raise GatewayError(
        "Счётчик прислал слишком много супервизорных кадров подряд без ответа приложения"
    )


def read_load_profile(
    transport: TcpTransport,
    *,
    serial: str,
    password: bytes,
    obis: str,
    class_id: int,
    from_dt,
    to_dt,
) -> Iterator[tuple[object, object]]:
    """Профиль нагрузки (Этап 3, ТЗ п.4.2.3) — SNRM/UA + AARQ/AARE + GET
    с выборкой по датам поверх ЕЩЁ НЕ установленного HDLC-соединения
    (обычный, не call-home, транспорт). См. docstring
    ``read_load_profile_via_established_link`` — вся протокольная логика
    там, здесь только установление связи перед ней (та же схема, что и
    у ``read_register``/``read_register_via_established_link``)."""
    server_addr = server_hdlc_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS

    _establish_link(transport, server_addr, client_addr)

    yield from read_load_profile_via_established_link(
        transport, serial=serial, password=password, obis=obis,
        class_id=class_id, from_dt=from_dt, to_dt=to_dt,
    )


def read_load_profile_via_established_link(
    transport: TcpTransport,
    *,
    serial: str,
    password: bytes,
    obis: str,
    class_id: int,
    from_dt,
    to_dt,
) -> Iterator[tuple[object, object]]:
    """AARQ/AARE + GET профиля нагрузки поверх УЖЕ установленной (SNRM/UA
    пройден) HDLC-связи — используется как обычным ``read_load_profile``,
    так и call-home транспортом (``callhome.read_load_profile_via_call_home``),
    у которого SNRM/UA выполняется отдельно с повторами (счётчик не
    всегда отвечает на первый SNRM, см. callhome.py).

    Реальный экспорт объектной модели счётчика (сервисная программа
    завода, 2026-08-19, см. DECISIONS.md) показал, что захватываемые
    колонки буфера НЕ включают объект Clock — строка не несёт метку
    времени сама по себе. Поэтому перед чтением буфера отдельным
    GET читается атрибут 4 (capture_period, секунды), а метка времени
    каждой строки вычисляется как ``from_dt + номер_строки * period``.

    Генератор: отдаёт КАЖДУЮ строку буфера сразу, как только она
    полностью собрана из накопленных байт (не дожидаясь всего ответа
    целиком) — обрыв соединения посреди передачи не теряет уже
    отданные вызывающему коду строки (ТЗ п.4.2.3 — докачка при обрыве,
    is_partial). Каждый элемент генератора — пара ``(timestamp, values)``,
    где ``values`` — то, что вернул ``datatypes.decode_value`` для
    захватываемых колонок одной строки буфера (список значений)."""
    server_addr = server_hdlc_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS

    aarq = dlms.build_aarq(password)
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=0, recv_seq=0,
        information=dlms.wrap_llc_command(aarq),
    )
    aare_frame = _recv_i_frame(transport)
    dlms.parse_aare(dlms.unwrap_llc(aare_frame.information))

    parsed_obis = dlms.parse_obis(obis)

    period_request = dlms.build_get_request(
        parsed_obis, class_id=class_id,
        attribute_id=dlms.PROFILE_GENERIC_CAPTURE_PERIOD_ATTRIBUTE,
    )
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=1, recv_seq=1,
        information=dlms.wrap_llc_command(period_request),
    )
    period_frame = _recv_i_frame(transport)
    capture_period_seconds = dlms.parse_get_response(dlms.unwrap_llc(period_frame.information))
    if not isinstance(capture_period_seconds, int) or capture_period_seconds <= 0:
        raise GatewayError(
            f"Некорректный capture_period профиля нагрузки: {capture_period_seconds!r}"
        )

    request = dlms.build_get_request_range(
        parsed_obis, class_id=class_id, from_dt=from_dt, to_dt=to_dt,
    )
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=2, recv_seq=2,
        information=dlms.wrap_llc_command(request),
    )

    send_seq = 3
    buf = bytearray()
    cursor = 0
    total_count: int | None = None
    decoded_count = 0

    def _timestamp_for(index: int):
        return from_dt + timedelta(seconds=capture_period_seconds * index)

    while True:
        response_frame = _recv_i_frame(transport)
        payload = dlms.unwrap_llc(response_frame.information)
        response_type = payload[1] if len(payload) > 1 else None

        if response_type == dlms.GET_RESPONSE_NORMAL:
            # Весь ответ уместился в одном PDU — блочная передача не
            # понадобилась (короткий диапазон дат).
            value = dlms.parse_get_response(payload)
            for index, row in enumerate(value):
                yield _timestamp_for(index), row
            return

        if response_type != dlms.GET_RESPONSE_WITH_DATABLOCK:
            raise GatewayError(
                f"Неожиданный тип GET.response при чтении профиля нагрузки: {payload[:2].hex()}"
            )

        block = dlms.parse_get_response_datablock(payload)
        buf.extend(block.raw_data)

        if total_count is None and len(buf) >= 2 and buf[0] == datatypes.TAG_ARRAY:
            total_count = buf[1]
            cursor = 2

        if total_count is not None:
            while decoded_count < total_count:
                try:
                    row, consumed = datatypes.decode_value(bytes(buf), offset=cursor)
                except DlmsDataError:
                    break  # строка ещё не собрана целиком — ждём следующий датаблок
                yield _timestamp_for(decoded_count), row
                cursor += consumed
                decoded_count += 1

        if block.last_block:
            if total_count is None or decoded_count < total_count:
                raise GatewayError(
                    "Буфер профиля нагрузки собран не полностью — данные оборвались "
                    "раньше заявленного количества строк"
                )
            return

        request_next = dlms.build_get_request_next(block.block_number + 1)
        _send_i_frame(
            transport, server_addr, client_addr, send_seq=send_seq, recv_seq=send_seq,
            information=dlms.wrap_llc_command(request_next),
        )
        send_seq += 1
