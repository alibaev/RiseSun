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
import time

from dataclasses import dataclass
from typing import Iterator

from ..addressing import HDLC_DLMS, physical_address
from ..errors import ConnectionLostError, GatewayError, MeterTimeoutError
from ..transport import TcpTransport
from . import datatypes, dlms
from .datatypes import DlmsDataError
from .hdlc import (
    CONTROL_DISC,
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
    aare_frame = _send_aarq_and_await_aare(
        transport, server_addr, client_addr, dlms.wrap_llc_command(aarq),
    )
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
    invoke_id: int = 1,
) -> tuple[object | None, int, GatewayError | None]:
    """Тело GET одного регистра (scaler_unit + value, см. докстринг
    ``read_register_via_established_link`` про порядок и вендорскую
    особенность Risesun) — вынесено отдельно (2026-09-09, событийное
    чтение call-home, см. DECISIONS.md и план ticklish-popping-bear.md),
    чтобы читать НЕСКОЛЬКО регистров за одну ассоциацию (см.
    ``read_registers_via_established_link``), продолжая нумерацию
    HDLC-кадров через все них, а не начиная её заново на каждый GET.

    ``invoke_id`` (2026-09-11, см. DECISIONS.md — "поймать байты одного
    отказа"): раньше ОБА GET внутри одного вызова (scaler_unit и value)
    жёстко использовали invoke_id=1 (значение по умолчанию
    ``dlms.build_get_request``), то есть КАЖДЫЙ GET за всю ассоциацию
    (включая все OBIS батча в ``read_registers_via_established_link``)
    отправлялся с одинаковым invoke_id. Найдено на живом трафике: второй
    (и далее) GET в ассоциации у части счётчиков (преимущественно новой
    партии) стабильно возвращает усечённый/невалидный ответ независимо
    от того, какой именно OBIS запрашивается (проверено на разных OBIS —
    показание энергии через VALUE_OBIS_OVERRIDES и токовый класс без
    него, оба ломаются одинаково) — рабочая гипотеза в том, что часть
    прошивок трактует повторный invoke_id как повтор УЖЕ обработанного
    запроса. Старый парк (381 исходный счётчик) читается нормально при
    том же поведении — значит, не все прошивки к этому чувствительны, но
    исправление безопасно для всех (invoke-id — advisory поле, ничего
    здесь не проверяет входящий invoke_id при разборе ответа).

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
            invoke_id=invoke_id,
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
        except DlmsDataError as exc:
            # см. комментарий у аналогичного except ниже (2026-09-10, DECISIONS.md)
            logger.warning(
                "Register %s: GET scaler_unit вернул некорректные данные (%s) — значение "
                "(OBIS %s) будет возвращено без применения масштаба",
                obis, exc, value_obis,
            )
            scaler_unit = None

    value_send_seq = send_seq + 1 if class_id == dlms.REGISTER_CLASS_ID else send_seq
    value_invoke_id = invoke_id + 1 if class_id == dlms.REGISTER_CLASS_ID else invoke_id
    request = dlms.build_get_request(parsed_value_obis, class_id=class_id, invoke_id=value_invoke_id)
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
    except DlmsDataError as exc:
        # 2026-09-10, см. DECISIONS.md: DlmsDataError — обычный ValueError,
        # НЕ подкласс GatewayError, поэтому раньше не ловился здесь и
        # улетал необработанным исключением из read_registers_via_
        # established_link — валил ВЕСЬ батч immediate-read (соседние уже
        # прочитанные OBIS этой же ассоциации терялись, job'ы не
        # репортились Backend'у и зависали до реанимации по таймауту)
        # вместо того, чтобы посчитаться отказом одного конкретно этого
        # OBIS, как и полагается data-access-error. Найдено на живом
        # трафике: счётчик ответил валидным GET.response, но с пустым
        # содержимым значения — редкий, но легитимный edge case.
        return None, next_send_seq, GatewayError(f"Некорректные данные в GET.response: {exc}")

    if class_id == dlms.REGISTER_CLASS_ID and raw_value is None:
        # 2026-09-11, найдено на новой партии счётчиков: GET.response-
        # Normal может прийти валидным (CRC/HCS сошлись), с выбором
        # "data" (не data-access-error), но со значением null-data
        # (0x00) — decode_value() теперь это разбирает как None вместо
        # падения "неподдержанный тег", но для показания энергии None
        # так же бессмысленен, как немасштабированное сырое число (см.
        # комментарий про scaler_unit чуть ниже) — тот же принцип "лучше
        # вообще без данных", отказ чтения вместо записи пустого
        # показания.
        return None, next_send_seq, GatewayError(
            f"Счётчик вернул null-data (0x00) вместо значения для {obis}"
        )

    if class_id != dlms.REGISTER_CLASS_ID or not isinstance(raw_value, (int, float)):
        return raw_value, next_send_seq, None

    if not (isinstance(scaler_unit, list) and len(scaler_unit) == 2 and isinstance(scaler_unit[0], int)):
        # 2026-09-11, см. DECISIONS.md: раньше в этом случае возвращалось
        # СЫРОЕ немасштабированное значение как будто оно валидное — и
        # оно попадало в MeterReading как обычное успешное показание.
        # Найдено на живых данных: 201901230052 получил "-1003" (без
        # дробной части — очевидный признак непроскейленного сырого
        # значения) при исправном предыдущем показании 3271.18 —
        # суммарная энергия не может уменьшаться, тем более уходить в
        # минус. Немасштабированное "сырое" число, тихо выданное за
        # валидное показание, вводит в заблуждение сильнее, чем честный
        # отказ чтения (который просто повторится на следующем цикле) —
        # поэтому теперь это GatewayError, а не успех.
        logger.warning(
            "Register %s (value read at %s): scaler_unit имеет неожиданный вид %r — "
            "отказ чтения вместо возврата непроскейленного сырого значения %r",
            obis, value_obis, scaler_unit, raw_value,
        )
        return None, next_send_seq, GatewayError(
            f"Не удалось прочитать масштаб (scaler_unit) для {obis} — значение {raw_value!r} "
            "не может быть достоверно интерпретировано"
        )
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
    aarq_user_information: bytes | None = None,
    aare_per_attempt_timeout_s: float | None = None,
    aare_max_attempts: int | None = None,
    send_disc_before_retry: bool = True,
    send_heartbeat_probe: bool = False,
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
    (частичный успех), а не теряются в брошенном исключении.

    ``aarq_user_information``/``aare_per_attempt_timeout_s``/
    ``aare_max_attempts``/``send_disc_before_retry`` — переопределения
    для эксперимента "эмуляция ver2.zip" (2026-09-10, см. DECISIONS.md
    и ``callhome.read_batch_via_fresh_connection_ver2_emulation``);
    дефолты воспроизводят обычное боевое поведение без изменений."""
    server_addr = server_hdlc_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS

    aarq = dlms.build_aarq(password, user_information=aarq_user_information)
    aare_frame = _send_aarq_and_await_aare(
        transport, server_addr, client_addr, dlms.wrap_llc_command(aarq),
        per_attempt_timeout_s=(
            aare_per_attempt_timeout_s
            if aare_per_attempt_timeout_s is not None
            else DEFAULT_AARQ_PER_ATTEMPT_TIMEOUT_S
        ),
        max_attempts=aare_max_attempts if aare_max_attempts is not None else DEFAULT_AARQ_MAX_ATTEMPTS,
        send_disc_before_retry=send_disc_before_retry,
        send_heartbeat_probe=send_heartbeat_probe,
    )
    dlms.parse_aare(dlms.unwrap_llc(aare_frame.information))  # AuthFailedError и т.п. — весь батч падает, это верно

    results: list[RegisterReadOutcome] = []
    send_seq = 1
    invoke_id = 1
    for obis, raw_class_id in obis_specs:
        # 2026-09-11, найдено на живом трафике (92% свежих показаний —
        # null): class_id=0 из payload/DueJobOut ("по умолчанию Register")
        # НИГДЕ не превращался в dlms.REGISTER_CLASS_ID=3 на этом,
        # событийном пути — в отличие от старого gRPC-пути
        # (grpc_server.py: ``class_id=request.class_id or dlms.
        # REGISTER_CLASS_ID``). В результате _read_one_register_via_
        # established_link все обычные read_current (класс не указан явно
        # -> 0) считал "не Register" — ни разу не читал scaler_unit и
        # возвращал СЫРОЕ значение как есть (а после сегодняшнего фикса
        # null-data — прямо None) как будто это валидный успех.
        class_id = raw_class_id or dlms.REGISTER_CLASS_ID
        try:
            value, send_seq, error = _read_one_register_via_established_link(
                transport, server_addr, client_addr, send_seq=send_seq, obis=obis, class_id=class_id,
                invoke_id=invoke_id,
            )
        except (ConnectionLostError, MeterTimeoutError, ConnectionError, OSError):
            break
        # Каждый GET получает свой invoke_id (см. докстринг
        # _read_one_register_via_established_link) — Register тратит 2
        # (scaler_unit + value), прочие классы — 1. Цикл 1..14, чтобы
        # invoke_id+1 внутри одного вызова не вышел за 4-битный диапазон
        # invoke-id (0-15).
        invoke_id += 2 if class_id == dlms.REGISTER_CLASS_ID else 1
        if invoke_id > 14:
            invoke_id = 1
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
    aare_frame = _send_aarq_and_await_aare(
        transport, server_addr, client_addr, dlms.wrap_llc_command(aarq),
    )
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
    aare_frame = _send_aarq_and_await_aare(
        transport, server_addr, client_addr, dlms.wrap_llc_command(aarq),
    )
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


# Ответ производителя (Eric, 2026-09-10, см. DECISIONS.md) на разбор
# AARE-тишины: "00 00 00" после AARQ — это heartbeat 3G/4G-модема,
# случайно совпавший по времени, штатная процедура на этот случай —
# "Resend the AARQ and wait for the meter's AARE response frame". До
# этого AARQ отправлялся РОВНО ОДИН раз, дальше — пассивное ожидание
# на весь бюджет (45-150с); теперь ждём короткими окнами и повторяем
# отправку AARQ между ними, вместо одной длинной пассивной паузы.
#
# Значение интервала (2026-09-10) — не подобрано наугад, а измерено на
# ЖИВОМ трафике работающей референсной системы (192.168.144.79,
# trace.txt): 88 из 95 измеренных интервалов между переотправками AARQ
# — РОВНО 5 секунд (было 10 — первоначальная догадка). См. DECISIONS.md.
DEFAULT_AARQ_PER_ATTEMPT_TIMEOUT_S = 5.0
DEFAULT_AARQ_MAX_ATTEMPTS = 20


def _send_aarq_and_await_aare(
    transport: TcpTransport,
    server_addr: int,
    client_addr: int,
    aarq_information: bytes,
    *,
    per_attempt_timeout_s: float = DEFAULT_AARQ_PER_ATTEMPT_TIMEOUT_S,
    max_attempts: int = DEFAULT_AARQ_MAX_ATTEMPTS,
    send_disc_before_retry: bool = True,
    send_heartbeat_probe: bool = False,
) -> HdlcFrame:
    """Отправляет AARQ (I(0,0)) и ждёт AARE, повторно отправляя AARQ
    короткими окнами вместо одного долгого пассивного ожидания (см.
    комментарий выше). Для call-home-транспорта (``DlT645FilteringSocket``,
    единый абсолютный дедлайн на всю операцию, включая последующие GET)
    временно сужает дедлайн под каждую попытку и восстанавливает исходный
    перед возвратом — иначе GET после AARE получили бы урезанный бюджет.
    Для обычного ``TcpTransport`` (прямое IP-подключение) — сокет и так
    использует фиксированный таймаут на каждый ``recv()``, поэтому
    ``get_deadline``/``set_deadline`` там просто отсутствуют (duck typing),
    и повтор AARQ происходит на этом же, уже существующем таймауте.

    ``send_heartbeat_probe`` (2026-09-12, точечный эксперимент, см.
    DECISIONS.md и ``callhome.HEARTBEAT_PROBE_TEST_SERIALS``) — по
    просьбе пользователя: если за весь бюджет ожидания AARE не приходит
    ВООБЩЕ НИ БАЙТА (даже документированного 2026-09-10 heartbeat-шума
    GPRS-модема "00 00 00", на который мы отвечаем эхом, когда его
    шлёт СЧЁТЧИК) — рабочая гипотеза: может быть, модем ждёт активности
    ОТ НАС, чтобы посчитать канал живым. Перед каждой повторной отправкой
    AARQ дополнительно шлём те же 3 нулевых байта (тот же паттерн, что
    counter/модем сам использует как keepalive) — дёшево и безопасно по
    построению (см. обоснование echo-фикса выше): если гипотеза неверна,
    это просто три лишних байта."""
    sock = getattr(transport, "_sock", None)
    get_deadline = getattr(sock, "get_deadline", None)
    set_deadline = getattr(sock, "set_deadline", None)
    original_deadline = get_deadline() if get_deadline is not None else None

    last_error: MeterTimeoutError | None = None
    try:
        for attempt in range(1, max_attempts + 1):
            if attempt > 1 and send_disc_before_retry:
                # Рекомендация производителя (2026-09-10, см. DECISIONS.md):
                # "Сначала отправьте кадр разрыва соединения, затем
                # отправьте AARQ" — перед ПОВТОРНОЙ отправкой AARQ (не
                # перед первой — на ней ещё нет "застрявшего" состояния,
                # которое нужно было бы сбрасывать) шлём DISC
                # (control=0x53). Ответ не ждём и не разбираем — если
                # придёт, это S/U-кадр, безопасно проглотится как
                # супервизорный в _recv_i_frame ниже.
                #
                # ``send_disc_before_retry=False`` — эмуляция ver2.zip
                # (см. DECISIONS.md 2026-09-10): декомпилированный
                # ``MeterDLMS.cs::Handclasp`` шлёт DISC перед повтором
                # AARQ НЕ безусловно, а только в конкретных случаях
                # рассинхронизации кадра — по умолчанию просто повторяет
                # AARQ без разрыва.
                disc_frame = HdlcFrame(
                    destination=server_addr, source=client_addr, control=CONTROL_DISC,
                )
                transport.send(disc_frame.encode())
            if send_heartbeat_probe:
                transport.send(b"\x00\x00\x00")
            _send_i_frame(
                transport, server_addr, client_addr, send_seq=0, recv_seq=0,
                information=aarq_information,
            )
            if set_deadline is not None:
                narrowed = time.time() + per_attempt_timeout_s
                if original_deadline is not None:
                    narrowed = min(narrowed, original_deadline)
                set_deadline(narrowed)
            try:
                return _recv_i_frame(transport)
            except MeterTimeoutError as exc:
                last_error = exc
                logger.info(
                    "AARE не пришло за %.0fс после AARQ (попытка %d/%d) — повторно "
                    "отправляем AARQ (рекомендация производителя, см. DECISIONS.md 2026-09-10)",
                    per_attempt_timeout_s, attempt, max_attempts,
                )
                if original_deadline is not None and time.time() >= original_deadline:
                    break
        raise last_error or MeterTimeoutError(
            f"AARE не пришло после {max_attempts} попыток AARQ"
        )
    finally:
        if set_deadline is not None:
            set_deadline(original_deadline)


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
    send_heartbeat_probe: bool = False,
) -> Iterator[tuple[object, object]]:
    """AARQ/AARE + GET профиля нагрузки поверх УЖЕ установленной (SNRM/UA
    пройден) HDLC-связи — используется как обычным ``read_load_profile``,
    так и call-home транспортом (``callhome.read_load_profile_via_call_home``),
    у которого SNRM/UA выполняется отдельно с повторами (счётчик не
    всегда отвечает на первый SNRM, см. callhome.py).

    2026-09-12 (реальные байтовые трассы 4 успешных сеансов
    IECMeterManage.exe, предоставленные пользователем — см. DECISIONS.md)
    ОПРОВЕРГЛИ более раннюю находку "буфер не включает Clock, метка времени
    вычисляется как from_dt + номер_строки*period" (сервисная программа
    завода, 2026-08-19): на самом деле КАЖДАЯ строка буфера несёт
    СОБСТВЕННУЮ метку времени в первых 6 байтах (см. ``dlms.decode_load_
    profile_row``) — отдельный GET атрибута 4 (capture_period) и вычисление
    меток по номеру строки больше не нужны и удалены (рабочий референс
    тоже не читает capture_period при чтении профиля — все 4 трассы это
    подтвердили).

    Генератор: отдаёт КАЖДУЮ строку буфера сразу, как только она
    полностью собрана из накопленных байт (не дожидаясь всего ответа
    целиком) — обрыв соединения посреди передачи не теряет уже
    отданные вызывающему коду строки (ТЗ п.4.2.3 — докачка при обрыве,
    is_partial). Каждый элемент генератора — пара ``(timestamp, values)``,
    где ``values`` — список значений колонок одной строки буфера (см.
    ``dlms.decode_load_profile_row`` — собственный BCD-формат этой модели
    счётчика, не стандартный DLMS common-data-type array-of-structure)."""
    server_addr = server_hdlc_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS

    aarq = dlms.build_aarq(password)
    aare_frame = _send_aarq_and_await_aare(
        transport, server_addr, client_addr, dlms.wrap_llc_command(aarq),
        send_heartbeat_probe=send_heartbeat_probe,
    )
    dlms.parse_aare(dlms.unwrap_llc(aare_frame.information))

    # 2026-09-11 — тот же баг, что был найден и исправлен для регистров
    # (см. read_registers_via_established_link и DECISIONS.md,
    # "class_id=0 никогда не нормализовался на событийном пути"): job'ы
    # read_load_profile, создаваемые через API/планировщик, обычно не
    # указывают class_id в payload вовсе (см. api/meters.py) — Backend
    # тогда отдаёт 0 по умолчанию (gateway_internal.py:
    # ``job.payload.get("class_id", 0)``). Старый gRPC-путь нормализовал
    # 0 в PROFILE_GENERIC_CLASS_ID снаружи, в grpc_server.py, ДО вызова
    # этой функции — новый (событийный) путь вызывает её напрямую и того
    # шага не делал. Нормализация здесь, внутри established_link-функции
    # (а не у каждого вызывающего кода по отдельности), защищает сразу
    # все три точки входа (call-home, call-home batch, обычный TCP).
    class_id = class_id or dlms.PROFILE_GENERIC_CLASS_ID

    parsed_obis = dlms.parse_obis(obis)

    # selected_values — 2026-09-12 реальные байтовые трассы 4 успешных
    # сеансов IECMeterManage.exe (предоставлены пользователем, см.
    # DECISIONS.md) показали, что рабочий референс ВСЕГДА шлёт здесь
    # пустой массив ("верни все колонки") — попытка подставить сюда
    # реальный список capture_objects (найденный ранее через SSH) была
    # ошибкой, отменена.
    request = dlms.build_get_request_range(
        parsed_obis, class_id=class_id, from_dt=from_dt, to_dt=to_dt, invoke_id=1,
    )
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=1, recv_seq=1,
        information=dlms.wrap_llc_command(request),
    )

    send_seq = 2
    invoke_id = 1
    buf = bytearray()
    # 2026-09-12 (см. DECISIONS.md, "может надо отправить хардбит?" —
    # живая проверка на нескольких счётчиках сразу после этого
    # эксперимента) — на КОНСТАНТНЫЙ invoke_id (см. комментарий ниже,
    # подтверждённый реальными трассами 4 сеансов IECMeterManage.exe)
    # часть парка (как минимум партии 2018 и 2023 годов — 201811000033,
    # 201811000016, ранее 202308004356) отвечает честным отказом
    # ``data-access-result=16`` ("No Long Get Or Read In Progress") на
    # ВТОРОМ датаблоке, хотя партия 2020 года (логи 4 успешных сеансов)
    # на тот же константный invoke_id отвечает штатно.
    #
    # Подтверждено первоисточником (2026-09-12, официальный открытый
    # исходник Gurux.DLMS.Net, GXDLMS.cs::GetInvokeIDPriority/
    # ReceiverReady — см. DECISIONS.md): это не два случайных диалекта,
    # а ШТАТНЫЙ переключатель настоящей библиотеки, ``AutoIncreaseInvokeID``
    # — при включении инкремент происходит НА КАЖДОМ исходящем PDU без
    # исключения, включая КАЖДЫЙ Get-Request-Next всей передачи, а не
    # только как разовое восстановление после отказа. Поэтому: обнаружив
    # отказ 16 один раз, дальше УЖЕ не пробуем константный invoke_id
    # снова — переключаемся в "нарастающий" режим на весь остаток этой
    # передачи (иначе на КАЖДОМ следующем датаблоке тратили бы лишний
    # обмен на заведомо обречённую константную попытку).
    use_incrementing_invoke_id = False

    while True:
        response_frame = _recv_i_frame(transport)
        payload = dlms.unwrap_llc(response_frame.information)
        response_type = payload[1] if len(payload) > 1 else None

        if response_type == dlms.GET_RESPONSE_NORMAL:
            # Весь ответ уместился в одном PDU — блочная передача не
            # понадобилась (короткий диапазон дат). ``parse_get_response``
            # тут не годится — она декодирует значение как СТАНДАРТНЫЙ
            # DLMS common-data-type, а буфер профиля нагрузки этой модели
            # счётчика — собственный BCD-формат (см. dlms.decode_load_
            # profile_row), поэтому нужны СЫРЫЕ байты после result-choice.
            if len(payload) < 4 or payload[3] != dlms.RESULT_DATA:
                raise GatewayError(
                    f"Неожиданный GET.response-normal при чтении профиля нагрузки: {payload.hex()}"
                )
            buf.extend(payload[4:])
            rows, buf_tail = dlms.split_load_profile_rows(bytes(buf))
            for row in rows:
                yield dlms.decode_load_profile_row(row)
            if buf_tail:
                yield dlms.decode_load_profile_row(buf_tail)
            return

        if response_type != dlms.GET_RESPONSE_WITH_DATABLOCK:
            raise GatewayError(
                f"Неожиданный тип GET.response при чтении профиля нагрузки: {payload[:2].hex()}"
            )

        try:
            block = dlms.parse_get_response_datablock(payload)
        except GatewayError:
            is_no_long_get_in_progress = (
                len(payload) > 9
                and payload[8] == dlms.DATABLOCK_RESULT_DATA_ACCESS_ERROR
                and payload[9] == 16
            )
            if is_no_long_get_in_progress and not use_incrementing_invoke_id:
                use_incrementing_invoke_id = True
                invoke_id = (invoke_id + 1) & 0xF
                request_next = dlms.build_get_request_next(pending_block_number, invoke_id=invoke_id)
                _send_i_frame(
                    transport, server_addr, client_addr, send_seq=send_seq, recv_seq=send_seq,
                    information=dlms.wrap_llc_command(request_next),
                )
                send_seq += 1
                continue
            raise
        buf.extend(block.raw_data)

        rows, remainder = dlms.split_load_profile_rows(bytes(buf))
        for row in rows:
            yield dlms.decode_load_profile_row(row)
        buf = bytearray(remainder)

        if block.last_block:
            if buf:
                yield dlms.decode_load_profile_row(bytes(buf))
            return

        # GET.request-Next продолжает УЖЕ начатую блочную передачу ответа
        # на GET-диапазон — по умолчанию переиспользует invoke_id
        # ИСХОДНОГО GET-запроса для всей длинной операции (2026-09-12:
        # реальные байтовые трассы 4 успешных сеансов IECMeterManage.exe,
        # предоставленные пользователем, — ~130 кадров Get-Request-Next
        # во всех 4 сеансах без единого исключения несут тот же
        # invoke_id, что и исходный GET-Request-Normal, см. DECISIONS.md).
        # Но если счётчик хоть раз откажет с data-access-result=16 (см.
        # обработку выше) — режим переключается на нарастающий (аналог
        # Gurux.DLMS.Net AutoIncreaseInvokeID=true) на весь остаток этой
        # передачи, инкрементируя ЗАРАНЕЕ, а не только после очередного
        # отказа. Маска ``& 0xF`` — invoke-id-and-priority строго 4-битное
        # поле (см. DECISIONS.md, официальный исходник Gurux.DLMS.Net,
        # GXDLMSSettings.InvokeID: сеттер бросает исключение при значении
        # больше 0xF) — без переноса на длинных профилях (20-30+
        # датаблоков) значение вышло бы за спецификацию.
        if use_incrementing_invoke_id:
            invoke_id = (invoke_id + 1) & 0xF
        pending_block_number = block.block_number + 1
        request_next = dlms.build_get_request_next(pending_block_number, invoke_id=invoke_id)
        _send_i_frame(
            transport, server_addr, client_addr, send_seq=send_seq, recv_seq=send_seq,
            information=dlms.wrap_llc_command(request_next),
        )
        send_seq += 1


def read_profile_capture_objects_via_established_link(
    transport: TcpTransport, *, serial: str, password: bytes, obis: str, class_id: int,
) -> object:
    """Диагностика (2026-09-12, по просьбе пользователя "покопай почему
    data-access-error 250") — читает атрибут 3 (capture_objects) объекта
    профиля нагрузки ОБЫЧНЫМ GET без access-selection (как и
    ``PROFILE_GENERIC_CAPTURE_PERIOD_ATTRIBUTE`` в ``read_load_profile_
    via_established_link``), а не GET-с-диапазоном. Совсем отдельная
    ассоциация/функция, не переиспользует и не трогает основной путь
    чтения буфера — цель узнать РЕАЛЬНЫЕ захватываемые колонки этого
    конкретного счётчика (каждый элемент ответа — структура class_id/
    logical_name/attribute_index/data_index), а не гадать, что подставлять
    в ``selected_values`` GET-запроса с диапазоном (см. DECISIONS.md:
    побайтовый разбор декомпилированного TpDLMS.cs::
    organizeFrame_GetLoadProfile показал, что рабочий референс указывает
    там ОДИН конкретный объект, а не пустой список "все колонки", как
    сейчас у нас в build_get_request_range).

    2026-09-12 (живая проверка) — ответ на этот GET у реального счётчика
    НЕ уместился в один PDU (``GET.response-normal``) — пришёл как
    ``GET.response-with-datablock``, поэтому функция следует той же
    логике накопления датаблоков + ``GET.request-Next``, что и чтение
    самого буфера в ``read_load_profile_via_established_link`` (но
    декодирует результат целиком одним значением, а не построчным
    генератором — это разовая диагностика, не потоковое чтение).

    2026-09-11 (по просьбе пользователя — "покопай ver2.zip, он же
    работает") одно время считалось, что GET.request-Next должен нести
    нарастающий invoke_id — вывод из разбора декомпилированного
    `TpDLMS.cs`, после единичного отказа ``data-access-result=16`` ("No
    Long Get Or Read In Progress") на второй датаблок с повторённым
    invoke_id.

    2026-09-12 (реальные байтовые трассы 4 успешных сеансов
    IECMeterManage.exe, предоставленные пользователем, см. DECISIONS.md)
    ОПРОВЕРГЛИ этот вывод: ~130 кадров Get-Request-Next во всех 4 сеансах
    без единого исключения несут ТОТ ЖЕ invoke_id, что и исходный
    GET-Request-Normal. Тот единичный отказ был вызван чем-то другим (см.
    отдельно найденную и исправленную ошибку в ``selected_values`` GET-
    диапазона). Возвращено поведение "тот же invoke_id для всей
    операции"."""
    server_addr = server_hdlc_address(physical_address(serial, HDLC_DLMS))
    client_addr = DEFAULT_CLIENT_ADDRESS

    aarq = dlms.build_aarq(password)
    aare_frame = _send_aarq_and_await_aare(
        transport, server_addr, client_addr, dlms.wrap_llc_command(aarq),
    )
    dlms.parse_aare(dlms.unwrap_llc(aare_frame.information))

    parsed_obis = dlms.parse_obis(obis)
    invoke_id = 1
    request = dlms.build_get_request(
        parsed_obis, class_id=class_id,
        attribute_id=dlms.PROFILE_GENERIC_CAPTURE_OBJECTS_ATTRIBUTE,
        invoke_id=invoke_id,
    )
    _send_i_frame(
        transport, server_addr, client_addr, send_seq=1, recv_seq=1,
        information=dlms.wrap_llc_command(request),
    )

    send_seq = 2
    buf = bytearray()
    while True:
        response_frame = _recv_i_frame(transport)
        payload = dlms.unwrap_llc(response_frame.information)
        response_type = payload[1] if len(payload) > 1 else None

        if response_type == dlms.GET_RESPONSE_NORMAL:
            return dlms.parse_get_response(payload)

        if response_type != dlms.GET_RESPONSE_WITH_DATABLOCK:
            raise GatewayError(
                f"Неожиданный тип GET.response при чтении capture_objects: {payload[:2].hex()}"
            )

        block = dlms.parse_get_response_datablock(payload)
        buf.extend(block.raw_data)
        if block.last_block:
            value, _consumed = datatypes.decode_value(bytes(buf), offset=0)
            return value

        request_next = dlms.build_get_request_next(block.block_number + 1, invoke_id=invoke_id)
        _send_i_frame(
            transport, server_addr, client_addr, send_seq=send_seq, recv_seq=send_seq,
            information=dlms.wrap_llc_command(request_next),
        )
        send_seq += 1
