"""xDLMS APDU: AARQ/AARE, сервис GET (ТЗ п. 4.3.2 — «HDLC + DLMS/COSEM»).

Кодировка идентификаторов приложения (application-context-name),
тегов ACSE (AARQ/AARE) и тегов xDLMS (GET.request/GET.response) —
общее знание стандарта DLMS/COSEM (IEC 62056-6-2, «Green Book»), не
выгружено из ТЗ или словаря OBIS напрямую. Реализация упрощена
относительно полного ACSE/BER (не кодируются необязательные поля
sender-acse-requirements, mechanism-name и др.) до минимума, достаточного
для установления ассоциации по паролю низкого уровня и чтения одного
атрибута. Подлежит сверке при работе с реальным оборудованием —
см. отчёт Этапа 0.

Формат OBIS-кода на входе: шесть точечно-разделённых полей, каждое —
однобайтное шестнадцатеричное значение (как в словаре OBIS, лист
RW_Tree_только_с_OBIS, например «1.1.1.8.0.ff»). Это соглашение
протокольного уровня; сокращённая десятичная нотация из API.docx
(«1.0.1.8.0», 5 полей, поле F=0xFF подразумевается) относится к
биллинговому REST API Backend'а и Gateway не касается.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import AuthFailedError, GatewayError
from . import datatypes

AARQ_TAG = 0x60
AARE_TAG = 0x61

# LLC-заголовок (Logical Link Control) поверх информационного поля
# HDLC-кадра, несущего DLMS/ACSE-данные (IEC 8802-2 LLC1, используется
# для COSEM-over-HDLC). Проверено на реальном трафике Risesun
# 2026-08-18: кадры от клиента к счётчику начинаются с ``E6 E6 00``,
# ответные — с ``E6 E7 00``. В ранней версии PoC не учитывалось.
LLC_COMMAND_HEADER = bytes([0xE6, 0xE6, 0x00])
LLC_RESPONSE_HEADER = bytes([0xE6, 0xE7, 0x00])


def wrap_llc_command(data: bytes) -> bytes:
    return LLC_COMMAND_HEADER + data


def wrap_llc_response(data: bytes) -> bytes:
    return LLC_RESPONSE_HEADER + data


def unwrap_llc(data: bytes) -> bytes:
    if len(data) < 3 or data[0] != 0xE6 or data[1] not in (0xE6, 0xE7) or data[2] != 0x00:
        raise GatewayError(f"Некорректный или отсутствующий LLC-заголовок: {data[:3].hex()}")
    return data[3:]

GET_REQUEST_TAG = 0xC0
GET_REQUEST_NORMAL = 0x01
GET_RESPONSE_TAG = 0xC4
GET_RESPONSE_NORMAL = 0x01

RESULT_DATA = 0x00
RESULT_DATA_ACCESS_ERROR = 0x01

AARE_RESULT_ACCEPTED = 0
AARE_RESULT_REJECTED_PERMANENT = 1
AARE_DIAGNOSTIC_AUTH_FAILURE = 13  # authenticationFailure (Green Book acse-service-user)

# application-context-name «LN referencing, без шифрования» — {2 16 756 5 8 1 1}
APPLICATION_CONTEXT_LN_NO_CIPHERING = (2, 16, 756, 5, 8, 1, 1)
# mechanism-name «Low Level Security» — {2 16 756 5 8 2 1}
MECHANISM_NAME_LLS = (2, 16, 756, 5, 8, 2, 1)

# sender-acse-requirements (тег 0x8A) и user-information/InitiateRequest
# (тег 0xBE) — фиксированные значения, не зависящие от пароля/счётчика:
# authentication-функция и стандартный набор proposed-conformance.
# Полный разбор/сборка InitiateRequest по BER — избыточно для Этапа 0,
# см. общую оговорку об упрощении ACSE в начале модуля.
#
# client-max-receive-pdu-size (последние 2 байта InitiateRequest) —
# ИЗМЕНЕНО 2026-09-10 (см. DECISIONS.md): было `0000` (0 байт),
# подтверждено побайтово по реальному сеансу Risesun 2026-08-18
# (test_real_capture_replay.py, CAPTURED_AARQ — та сессия тогда
# завершилась успешно и с этим значением). Но во время многодневного
# расследования тотальной AARE-тишины (2026-09-09/10) байтовый захват
# ПРЯМО СЕЙЧАС работающей референсной системы (легаси C#-клиент,
# 192.168.144.79, ~85% успеха) на нескольких разных счётчиках
# консистентно показал `0800` (2048 байт) — и это единственное
# отличие от нашего AARQ. Пересчитанный с новым значением CRC
# (`9C4D`) байт-в-байт совпал с реальным трафиком референсной системы
# — независимое подтверждение. Не 100%-но доказанная причина
# AARE-тишины (0 тоже когда-то работал), но безопасное изменение
# (2048 не хуже 0) и единственная проверяемая гипотеза, оставшаяся
# после того как fallback на несколько соединений, переотправка AARQ
# и фикс DPD на IPsec-туннеле не дали результата.
SENDER_ACSE_REQUIREMENTS = bytes.fromhex("8a020780")
USER_INFORMATION_INITIATE = bytes.fromhex("be10040e01000000065f1f040000081d0800")

# Байты AARQ декомпилированного `TpDLMS.cs::organizeFrame_AARQ` (ver2.zip,
# "заводская сервисная программа" — 2026-09-10, см. DECISIONS.md) —
# идентичны нашим ВО ВСЁМ, кроме client-max-receive-pdu-size: там `0000`
# (0), не `0800`. Скорее всего это и есть источник того самого
# исторического успешного сеанса 2026-08-18 с `0000` (см. комментарий
# выше) — вероятно, ОБЕ референс-программы реально работали с разными
# парками/сессиями. Используется только экспериментальным путём
# эмуляции ver2.zip (``callhome.py``), НЕ значением по умолчанию.
USER_INFORMATION_INITIATE_VER2_VARIANT = bytes.fromhex("be10040e01000000065f1f040000081d0000")

REGISTER_CLASS_ID = 3
REGISTER_VALUE_ATTRIBUTE = 2
# Атрибут 3 (scaler_unit) — structure {scaler: integer (десятичный
# показатель степени), unit: enum}. Найденный баг (2026-08-19,
# сообщение пользователя): показания выводились БЕЗ применения scaler
# (напр. 4507.70 отображалось как 450770) — атрибут 3 упоминался в
# докстрингах ещё с Этапа 0 как «легитимный клиент читает scaler_unit
# отдельным GET перед value» (подтверждено реальным трафиком Risesun),
# но фактически нигде не читался и не применялся. См.
# ``hdlc_dlms.read_register_via_established_link``.
REGISTER_SCALER_UNIT_ATTRIBUTE = 3

# Единица измерения из scaler_unit (Green Book, IEC 62056-62 Table 4).
# Единственная подтверждённая реальным трафиком Risesun DTZY217 (см.
# test_real_capture_replay.py, CAPTURED_GET_SCALER_ENERGY_RESP) —
# `unit=30` (Wh) для суммарной активной энергии. Тот же тест фиксирует
# ожидаемую формулу перевода в отображаемые (везде в проекте — kWh,
# см. OBIS.xlsx) единицы: `raw_value * 10**scaler / 1000`, а не просто
# `raw_value * 10**scaler` — доп. `/1000` переводит Вт·ч в кВт·ч и
# нужен ДАЖЕ при scaler=0 (найденный баг, 2026-09-07: показание
# счётчика 202302003956 передавалось как `6101020` вместо `6101.020` —
# именно случай scaler=0, unit=30, где старая формула ничего не меняла
# вообще). См. ``hdlc_dlms.read_register_via_established_link``.
UNIT_WATT_HOUR = 30

# Вендорская особенность Risesun DTZY217 (2026-08-20, найденный баг —
# показание счётчика 202306004113 по стандартному OBIS суммарной
# активной энергии `1.1.1.8.0.ff` отображалось как `450770` вместо
# `4507.70`, т.е. без применения scaler_unit). Реальным трафиком
# легитимного заводского приложения (2026-08-18, см. DECISIONS.md, п.6)
# подтверждена только ОДНА рабочая связка для этого регистра:
# scaler_unit (атрибут 3) читается по стандартному OBIS `1.8.0`, а
# ЗНАЧЕНИЕ (атрибут 2) — по отдельному вендорскому OBIS `96.80.0` (hex
# `1.1.60.50.0.ff`), тот же физический регистр. Читать value и
# scaler_unit ПО ОДНОМУ OBIS (как раньше) для этого регистра на
# реальном счётчике не работает (одометр отдаёт сырое value без ошибки,
# scaler_unit «пропадает» — вероятно, объект по стандартному OBIS не
# отдаёт корректный scaler_unit для value, точная причина не выяснена).
# Здесь регистрируется единственный подтверждённый случай: при чтении
# value для ключа слева Gateway обязан фактически запросить атрибут 2 у
# OBIS справа, оставив scaler_unit на исходном (запрошенном) OBIS — см.
# ``hdlc_dlms.read_register_via_established_link``.
VALUE_OBIS_OVERRIDES: dict[str, str] = {
    "1.1.1.8.0.ff": "1.1.60.50.0.ff",
}

PROFILE_GENERIC_CLASS_ID = 7  # буфер профиля нагрузки (Этап 3, ТЗ п.4.2.3)
# Атрибут 4 (capture_period, секунды) — период записи строк буфера.
# Реальный экспорт объектной модели DTZY217 (сервисная программа завода,
# 2026-08-19, см. DECISIONS.md) показал, что захватываемые колонки этого
# профиля НЕ включают объект Clock — буфер не несёт метку времени внутри
# строки, поэтому Gateway обязан прочитать capture_period ДО чтения
# буфера и вычислить метки сам (см. read_load_profile в hdlc_dlms.py).
PROFILE_GENERIC_CAPTURE_PERIOD_ATTRIBUTE = 4
# Атрибут 3 (capture_objects) — список фактически захватываемых колонок
# буфера (каждый элемент: class_id, logical_name, attribute_index,
# data_index). Диагностический GET без access-selection (как и
# capture_period) — реальные трассы 2026-09-12 показали, что рабочий
# референс читает этот атрибут отдельно для СПРАВКИ, но НЕ подставляет
# результат в selected_values GET-диапазона (см. DECISIONS.md и
# ``build_get_request_range``).
PROFILE_GENERIC_CAPTURE_OBJECTS_ATTRIBUTE = 3


def encode_oid(components: tuple[int, ...]) -> bytes:
    first = components[0] * 40 + components[1]
    out = bytearray([first])
    for component in components[2:]:
        out += _base128(component)
    return bytes(out)


def _base128(n: int) -> bytes:
    if n == 0:
        return bytes([0])
    chunks: list[int] = []
    while n:
        chunks.insert(0, n & 0x7F)
        n >>= 7
    for i in range(len(chunks) - 1):
        chunks[i] |= 0x80
    return bytes(chunks)


def parse_obis(text: str) -> bytes:
    """Разбирает «A.B.C.D.E.F» (шестнадцатеричные однобайтные поля) в 6 байт."""
    parts = text.split(".")
    if len(parts) != 6:
        raise GatewayError(
            f"OBIS-код должен состоять из 6 полей вида A.B.C.D.E.F, получено: {text!r}"
        )
    try:
        values = [int(p, 16) for p in parts]
    except ValueError as exc:
        raise GatewayError(f"Нечисловое (не hex) поле в OBIS-коде {text!r}") from exc
    if any(not 0 <= v <= 0xFF for v in values):
        raise GatewayError(f"Поле OBIS-кода вне диапазона 0..FF: {text!r}")
    return bytes(values)


def build_aarq(password: bytes, *, mechanism_id: int = 1, user_information: bytes | None = None) -> bytes:
    """Строит AARQ с calling-authentication-value = пароль низкого уровня.

    Состав полей (application-context, sender-acse-requirements,
    mechanism-name, calling-authentication-value, user-information)
    сверен побайтово с реальным трафиком Risesun — без
    sender-acse-requirements/mechanism-name/user-information реальный
    счётчик ассоциацию не примет (см. DECISIONS.md, 2026-08-18).

    ``mechanism_id`` — последний октет OID mechanism-name
    (``2.16.756.5.8.2.<mechanism_id>``, Green Book): 1=LLS (дефолт,
    подтверждён реальным трафиком), 2=HLS, 5=HLS-GMAC и т.д. Параметр
    исключительно для диагностики (2026-09-09) — гипотеза, что часть
    счётчиков молчит на AARQ из-за несовпадения уровня аутентификации
    (см. DECISIONS.md); calling-authentication-value при этом всё равно
    заполняется как для LLS (сырой пароль), что для настоящего HLS
    протокольно некорректно (там ожидается вызов-ответ, не пароль) —
    годится только чтобы проверить, реагирует ли счётчик на смену OID
    вообще, не для завершения полноценной HLS-ассоциации.

    ``user_information`` — переопределяет ``USER_INFORMATION_INITIATE``
    (дефолт — ``None``, используется модульная константа). Добавлено
    2026-09-10 для эксперимента "эмуляция ver2.zip": декомпилированный
    ``TpDLMS.cs::organizeFrame_AARQ`` шлёт те же байты, что и наша
    константа, ЗА ИСКЛЮЧЕНИЕМ последних двух (client-max-receive-pdu-size
    = ``0000``, а не ``0800``) — см. DECISIONS.md."""
    context_oid = encode_oid(APPLICATION_CONTEXT_LN_NO_CIPHERING)
    application_context = bytes([0xA1, len(context_oid) + 2, 0x06, len(context_oid)]) + context_oid
    mechanism_oid = encode_oid((2, 16, 756, 5, 8, 2, mechanism_id)) if mechanism_id != 1 else encode_oid(MECHANISM_NAME_LLS)
    mechanism_name = bytes([0x8B, len(mechanism_oid)]) + mechanism_oid
    auth_value = bytes([0x80, len(password)]) + password
    calling_auth = bytes([0xAC, len(auth_value)]) + auth_value
    body = (
        application_context
        + SENDER_ACSE_REQUIREMENTS
        + mechanism_name
        + calling_auth
        + (user_information if user_information is not None else USER_INFORMATION_INITIATE)
    )
    if len(body) > 0x7F:
        raise GatewayError(
            "AARQ длиннее 127 байт — короткая форма длины BER не подходит (не ожидается "
            "при пароле LLS фиксированной длины 8 символов, ТЗ п. 4.3.3)"
        )
    return bytes([AARQ_TAG, len(body)]) + body


@dataclass
class ParsedAarq:
    password: bytes


def parse_aarq(data: bytes) -> ParsedAarq:
    if not data or data[0] != AARQ_TAG:
        raise GatewayError("Ожидался AARQ (тег 0x60)")
    body = data[2 : 2 + data[1]]
    pos = 0
    password = b""
    while pos < len(body):
        tag = body[pos]
        length = body[pos + 1]
        content = body[pos + 2 : pos + 2 + length]
        if tag == 0xAC:
            # calling-authentication-value -> [0x80 len password]
            if content and content[0] == 0x80:
                password = content[2 : 2 + content[1]]
        pos += 2 + length
    return ParsedAarq(password=password)


def build_aare(*, accepted: bool) -> bytes:
    result = AARE_RESULT_ACCEPTED if accepted else AARE_RESULT_REJECTED_PERMANENT
    result_field = bytes([0xA2, 0x03, 0x02, 0x01, result])
    if accepted:
        body = result_field
    else:
        diagnostic = bytes(
            [0xA3, 0x05, 0xA1, 0x03, 0x02, 0x01, AARE_DIAGNOSTIC_AUTH_FAILURE]
        )
        body = result_field + diagnostic
    return bytes([AARE_TAG, len(body)]) + body


_AARE_RESULT_TAG = 0xA2


def parse_aare(data: bytes) -> bool:
    """Возвращает True, если ассоциация принята; иначе бросает AuthFailedError.

    Поле ``result`` (тег 0xA2) ищется сканированием TLV, а не по
    фиксированной позиции — реальный AARE (в отличие от упрощённого
    ``build_aare`` ниже) содержит перед ним application-context (тег
    0xA1), см. DECISIONS.md, 2026-08-18.
    """
    if not data or data[0] != AARE_TAG:
        raise GatewayError("Ожидался AARE (тег 0x61)")
    body = data[2 : 2 + data[1]]
    pos = 0
    result: int | None = None
    while pos + 1 < len(body):
        tag = body[pos]
        length = body[pos + 1]
        content = body[pos + 2 : pos + 2 + length]
        if tag == _AARE_RESULT_TAG and len(content) >= 3:
            result = content[2]
        pos += 2 + length
    if result is None:
        raise GatewayError("Некорректное поле result в AARE")
    if result == AARE_RESULT_ACCEPTED:
        return True
    raise AuthFailedError("Счётчик отклонил ассоциацию (неверный пароль доступа)")


def build_get_request(
    obis: bytes,
    *,
    invoke_id: int = 1,
    attribute_id: int = REGISTER_VALUE_ATTRIBUTE,
    class_id: int = REGISTER_CLASS_ID,
) -> bytes:
    """``attribute_id`` по умолчанию — 2 (value). Атрибут 3 (scaler_unit)
    нужен для чтения масштаба/единицы измерения перед интерпретацией
    значения (подтверждено реальным трафиком Risesun — легитимный
    клиент читает scaler_unit отдельным GET перед value, см.
    DECISIONS.md, 2026-08-18). ``class_id`` по умолчанию — 3 (Register);
    параметры записи (Этап 2, например «Current Time»/«Current Date» —
    словарь OBIS, лист RW_Tree_параметры) — класс 1 (Data), передаётся
    явно вызывающим кодом."""
    if len(obis) != 6:
        raise GatewayError("OBIS для GET.request должен быть ровно 6 байт")
    descriptor = (
        class_id.to_bytes(2, "big")
        + obis
        + bytes([attribute_id])
    )
    return (
        bytes([GET_REQUEST_TAG, GET_REQUEST_NORMAL, invoke_id])
        + descriptor
        + bytes([0x00])  # access-selection отсутствует
    )


@dataclass
class ParsedGetRequest:
    invoke_id: int
    class_id: int
    obis: bytes
    attribute_id: int


def parse_get_request(data: bytes) -> ParsedGetRequest:
    if len(data) < 3 or data[0] != GET_REQUEST_TAG or data[1] != GET_REQUEST_NORMAL:
        raise GatewayError("Ожидался GET.request-normal (тег 0xC0 0x01)")
    invoke_id = data[2]
    class_id = int.from_bytes(data[3:5], "big")
    obis = data[5:11]
    attribute_id = data[11]
    return ParsedGetRequest(
        invoke_id=invoke_id, class_id=class_id, obis=obis, attribute_id=attribute_id
    )


def build_get_response_data(invoke_id: int, encoded_value: bytes) -> bytes:
    return (
        bytes([GET_RESPONSE_TAG, GET_RESPONSE_NORMAL, invoke_id, RESULT_DATA])
        + encoded_value
    )


def build_get_response_error(invoke_id: int, data_access_result: int) -> bytes:
    return bytes(
        [
            GET_RESPONSE_TAG,
            GET_RESPONSE_NORMAL,
            invoke_id,
            RESULT_DATA_ACCESS_ERROR,
            data_access_result,
        ]
    )


def parse_get_response(data: bytes) -> object:
    """Возвращает декодированное значение атрибута либо бросает GatewayError."""
    if len(data) < 4 or data[0] != GET_RESPONSE_TAG or data[1] != GET_RESPONSE_NORMAL:
        raise GatewayError("Ожидался GET.response-normal (тег 0xC4 0x01)")
    result = data[3]
    if result == RESULT_DATA_ACCESS_ERROR:
        code = data[4] if len(data) > 4 else -1
        raise GatewayError(f"Счётчик вернул data-access-result={code} на GET-запрос")
    if result != RESULT_DATA:
        # 2026-09-11, найдено на живом трафике (см. DECISIONS.md — "поймать
        # байты одного отказа"): choice-байт Get-Data-Result по стандарту
        # может быть только 0 (data) или 1 (data-access-result) — но
        # реально приходит, напр., 0x17, за которым СРАЗУ конец кадра
        # (только 4 байта во всём GET.response-Normal, CRC/HCS кадра при
        # этом честно сходятся — это не повреждение при приёме, счётчик
        # прислал именно это). Раньше такой байт молча трактовался как
        # "дальше идёт значение" и decode_value() падал с непонятным
        # "Пустые данные"/"неподдержанный тег" — теперь отдельная, ясная
        # ошибка на самом источнике проблемы, ближе к правде о том, что
        # произошло.
        raise GatewayError(
            f"Неожиданный choice-байт Get-Data-Result: 0x{result:02X} (ожидался 0x00 data "
            f"или 0x01 data-access-result) — вероятно, счётчик прислал укороченный ответ"
        )
    value, _consumed = datatypes.decode_value(data, offset=4)
    return value


# --- SET-request/response (Этап 2 — запись параметров, ТЗ п. 4.2.4) ---
#
# Тот же принцип APDU, что и у GET выше (IEC 62056-6-2): SET-request-
# Normal переносит cosem-attribute-descriptor (class-id, OBIS,
# attribute-id) + access-selection + новое значение атрибута;
# SET-response-Normal возвращает единственный байт data-access-result
# (0 = success, прочие значения — конкретный код отказа записи, тот же
# перечень, что и у GET, Green Book).

SET_REQUEST_TAG = 0xC1
SET_REQUEST_NORMAL = 0x01
SET_RESPONSE_TAG = 0xC5
SET_RESPONSE_NORMAL = 0x01

SET_RESULT_SUCCESS = 0
_SET_RESULT_NAMES = {
    0: "success",
    1: "hardware-fault",
    2: "temporary-failure",
    3: "read-write-denied",
    4: "object-undefined",
    9: "object-class-inconsistent",
    11: "object-unavailable",
    12: "type-unmatched",
    13: "scope-of-access-violated",
    14: "data-block-unavailable",
    250: "other-reason",
}


def build_set_request(
    obis: bytes,
    encoded_value: bytes,
    *,
    invoke_id: int = 1,
    attribute_id: int = REGISTER_VALUE_ATTRIBUTE,
    class_id: int = REGISTER_CLASS_ID,
) -> bytes:
    """``encoded_value`` — уже закодированное Common-Data-Type значение
    (тег + длина + содержимое, см. ``protocols.datatypes.encode_*``)."""
    if len(obis) != 6:
        raise GatewayError("OBIS для SET.request должен быть ровно 6 байт")
    descriptor = class_id.to_bytes(2, "big") + obis + bytes([attribute_id])
    return (
        bytes([SET_REQUEST_TAG, SET_REQUEST_NORMAL, invoke_id])
        + descriptor
        + bytes([0x00])  # access-selection отсутствует
        + encoded_value
    )


@dataclass
class ParsedSetRequest:
    invoke_id: int
    class_id: int
    obis: bytes
    attribute_id: int
    value: object
    encoded_value: bytes  # сырые байты значения (тег+длина+содержимое) — удобно эмулятору для эхо в GET


def parse_set_request(data: bytes) -> ParsedSetRequest:
    if len(data) < 13 or data[0] != SET_REQUEST_TAG or data[1] != SET_REQUEST_NORMAL:
        raise GatewayError("Ожидался SET.request-normal (тег 0xC1 0x01)")
    invoke_id = data[2]
    class_id = int.from_bytes(data[3:5], "big")
    obis = data[5:11]
    attribute_id = data[11]
    # data[12] — access-selection, всегда 0x00 (отсутствует) в текущей реализации
    value, consumed = datatypes.decode_value(data, offset=13)
    encoded_value = data[13 : 13 + consumed]
    return ParsedSetRequest(
        invoke_id=invoke_id, class_id=class_id, obis=obis, attribute_id=attribute_id,
        value=value, encoded_value=encoded_value,
    )


def build_set_response(invoke_id: int, *, result: int = SET_RESULT_SUCCESS) -> bytes:
    return bytes([SET_RESPONSE_TAG, SET_RESPONSE_NORMAL, invoke_id, result])


def parse_set_response(data: bytes) -> None:
    """Не возвращает значения — бросает GatewayError, если запись отклонена."""
    if len(data) < 4 or data[0] != SET_RESPONSE_TAG or data[1] != SET_RESPONSE_NORMAL:
        raise GatewayError("Ожидался SET.response-normal (тег 0xC5 0x01)")
    result = data[3]
    if result != SET_RESULT_SUCCESS:
        name = _SET_RESULT_NAMES.get(result, f"0x{result:02X}")
        raise GatewayError(f"Счётчик отклонил запись параметра: data-access-result={result} ({name})")


# --- Профиль нагрузки (Этап 3, ТЗ п.4.2.3): GET с access-selection
# (выборка по диапазону дат) + блочная передача больших ответов ---
#
# Объект профиля нагрузки (COSEM Profile Generic, класс 7) в словаре
# OBIS.xlsx не задокументирован (проверено все 7 листов — есть только
# настройки интервала записи, не адрес самого буфера). По решению
# пользователя 2026-08-19 используется стандартный DLMS-адрес Load
# Profile 1 (`1.0.99.1.0.255`, IEC 62056-6-2) как рабочая гипотеза,
# подлежащая проверке на реальном оборудовании — см. DECISIONS.md.
#
# access-selection в GET.request-Normal — байт "есть/нет" (0x00 —
# отсутствует, уже используется в build_get_request через явный [0x00]
# в конце), затем при наличии: [0x01, access-selector, access-parameters].
# Для профиля нагрузки — access-selector=1 (range-descriptor),
# access-parameters — структура из 4 полей: restricting_object,
# from_value/to_value (диапазон, см. ``datatypes.encode_date_time`` —
# тег ``date_time`` 0x19, 2026-09-12), selected_values (пустой массив =
# вернуть все захватываемые колонки).
#
# restricting_object — история метаний (см. DECISIONS.md за подробным
# разбором каждого шага):
# 1. Изначально — структура-ссылка на объект Clock (класс 8, OBIS
#    0.0.1.0.0.255, атрибут 2), по "стандартному" описанию range-
#    descriptor в Green Book.
# 2. 2026-09-10 — заменено на NULL-DATA: экспорт объектной модели
#    (2026-08-19) показал, что захватываемые колонки буфера НЕ включают
#    Clock, а разбор декомпилированного `ver2.zip` (`TpDLMS.cs::
#    organizeFrame_GetLoadProfile`) показал, что реально работающая
#    программа отправляет там NULL. НО тот эксперимент с Clock
#    тестировался СО СТАРОЙ (октет-строка) кодировкой дат.
# 3. 2026-09-12 — снова Clock-структура, вместе с НОВОЙ кодировкой дат
#    (date_time, п.1 выше): другой декомпилированный референс
#    (`GXDLMSReader.cs::RS_PostProcessingProfileGenericsDates`) патчит
#    ТОЛЬКО кодировку дат в исходящем запросе стандартной Gurux.DLMS —
#    restricting_object он не трогает вовсе, значит у него остаётся
#    Gurux'овское значение по умолчанию (Clock-ссылка). "NULL +
#    date_time" на живой проверке дал ТОТ ЖЕ отказ (data-access-
#    result=250), что и раньше — комбинация "Clock + date_time" ещё не
#    была опробована, хотя именно она ближе всего к тому, что реально
#    отправляет второй референс.

RANGE_DESCRIPTOR_SELECTOR = 1
ENTRY_DESCRIPTOR_SELECTOR = 2
CLOCK_CLASS_ID = 8
CLOCK_OBIS = bytes([0, 0, 1, 0, 0, 0xFF])  # стандартный OBIS объекта Clock

GET_REQUEST_NEXT = 0x02
GET_RESPONSE_WITH_DATABLOCK = 0x02
DATABLOCK_RESULT_RAW_DATA = 0x00
DATABLOCK_RESULT_DATA_ACCESS_ERROR = 0x01


# 2026-09-12 (по просьбе пользователя — SSH-доступ к рабочему
# 2026-09-12 — раньше здесь был захардкожен список capture_objects,
# найденный через SSH в DataReadScheme.ini референсной системы, и
# подставлялся в ``selected_values`` GET-диапазона. Реальные байтовые
# трассы 4 успешных сеансов IECMeterManage.exe (предоставлены
# пользователем, см. DECISIONS.md) показали: рабочий референс шлёт
# ``selected_values`` ПУСТЫМ всегда — список удалён, использовать его
# для этой цели было ошибкой (см. ``build_get_request_range`` и
# ``hdlc_dlms.read_load_profile_via_established_link``).


def build_get_request_range(
    obis: bytes,
    *,
    class_id: int,
    from_dt,
    to_dt,
    invoke_id: int = 1,
    attribute_id: int = REGISTER_VALUE_ATTRIBUTE,
    selected_values: list[tuple[int, bytes, int, int]] | None = None,
) -> bytes:
    """GET.request-Normal с access-selection=range-descriptor — просит
    у счётчика только записи буфера профиля нагрузки за ``[from_dt,
    to_dt]`` вместо выгрузки всего буфера целиком.

    2026-09-12 (по просьбе пользователя — "покопай, может тут сдвиг
    байта") — from/to кодируются тегом ``date_time`` (0x19, см.
    ``datatypes.encode_date_time``), НЕ ``octet-string`` (0x09) с
    сырыми 12 байтами внутри, как было раньше. Найдено в
    декомпилированном `GXDLMSReader.cs::RS_PostProcessingProfileGenericsDates`
    (см. DECISIONS.md): легаси-программа хирургически патчит исходящий
    запрос от стандартной Gurux.DLMS, заменяя байты ``09 0C`` (ровно то,
    что кодировал старый вариант этой функции) на один байт ``0x19`` —
    прямое свидетельство, что прошивка счётчика не разбирает дату,
    завёрнутую в octet-string.

    ``restricting_object`` (2026-09-12, тот же повод) — структура-ссылка
    на объект Clock (класс 8, OBIS 0.0.1.0.0.255, атрибут 2, data_index
    0), а НЕ ``NULL-DATA`` (см. запись 2026-09-10 в DECISIONS.md — тогда
    её заменили на NULL, но тот эксперимент использовал СТАРУЮ кодировку
    дат через octet-string). 2026-09-12 (реальные байтовые трассы 4
    сеансов IECMeterManage.exe, предоставленные пользователем, см.
    DECISIONS.md) подтвердили ЧЕТЫРЁХпольную структуру (раньше кодировали
    только 3 поля, без ``data_index``) — ``[class_id, obis, attribute_id,
    data_index=0]``.

    ``selected_values`` (2026-09-12) — список ``(class_id, obis,
    attribute_index, data_index)``, каждый кодируется как структура из 4
    элементов; ``None``/пустой список (умолчание) — "верни все колонки".
    Те же реальные трассы 2026-09-12 показали: рабочий референс ВСЕГДА
    посылает здесь пустой массив, даже когда он же отдельным GET атрибута
    3 (capture_objects) уже знает список колонок — параметр оставлен для
    общности API, но использовать его для профиля нагрузки DTZY217 не
    нужно (см. отмену в ``hdlc_dlms.read_load_profile_via_established_
    link`` и DECISIONS.md)."""
    if len(obis) != 6:
        raise GatewayError("OBIS для GET.request должен быть ровно 6 байт")
    descriptor = class_id.to_bytes(2, "big") + obis + bytes([attribute_id])

    restricting_object = datatypes.encode_structure([
        datatypes.encode_long_unsigned(CLOCK_CLASS_ID),
        datatypes.encode_octet_string(CLOCK_OBIS),
        datatypes.encode_integer(2),
        datatypes.encode_long_unsigned(0),
    ])
    from_value = datatypes.encode_date_time(from_dt)
    to_value = datatypes.encode_date_time(to_dt)
    selected_values_encoded = datatypes.encode_array([
        datatypes.encode_structure([
            datatypes.encode_long_unsigned(sel_class_id),
            datatypes.encode_octet_string(sel_obis),
            datatypes.encode_integer(sel_attribute_index),
            datatypes.encode_long_unsigned(sel_data_index),
        ])
        for sel_class_id, sel_obis, sel_attribute_index, sel_data_index in (selected_values or [])
    ])
    access_parameters = datatypes.encode_structure(
        [restricting_object, from_value, to_value, selected_values_encoded]
    )
    access_selection = bytes([0x01, RANGE_DESCRIPTOR_SELECTOR]) + access_parameters

    return (
        bytes([GET_REQUEST_TAG, GET_REQUEST_NORMAL, invoke_id])
        + descriptor
        + access_selection
    )


def build_get_request_next(block_number: int, *, invoke_id: int = 1) -> bytes:
    """GET.request-Next — запрашивает следующий датаблок ответа, который
    не поместился целиком в предыдущий (см. parse_get_response_datablock)."""
    return bytes([GET_REQUEST_TAG, GET_REQUEST_NEXT, invoke_id]) + block_number.to_bytes(4, "big")


def _encode_ber_length(length: int) -> bytes:
    """Длина в стандартной BER-кодировке DLMS: один байт (0..127) для
    коротких значений, иначе байт ``0x80 | N`` (N — число следующих
    байт длины) и сама длина, big-endian, в N байтах.

    2026-09-12 (реальные байтовые трассы 4 успешных сеансов
    IECMeterManage.exe, предоставленные пользователем, см. DECISIONS.md)
    — раньше ``raw-data`` датаблока кодировалась/разбиралась ФИКСИРОВАННЫМИ
    2 байтами длины всегда, что для датаблоков короче 128 байт (в т.ч.
    для 1-го датаблока capture_objects, 110 байт) съедало на 1 байт
    больше, чем счётчик реально передавал, — вся ``raw_data`` этого
    датаблока смещалась на 1 байт и переставала разбираться. Датаблоки
    ≥128 байт (128..255) случайно совпадали с этим допущением (BER для
    них — ровно 2 байта: ``0x81`` + 1 байт длины), поэтому баг был
    незаметен на типичных «полных» датаблоках."""
    if length < 0x80:
        return bytes([length])
    if length <= 0xFF:
        return bytes([0x81, length])
    if length <= 0xFFFF:
        return bytes([0x82]) + length.to_bytes(2, "big")
    raise GatewayError("Датаблок длиннее 65535 байт не поддержан в Этапе 3")


def _decode_ber_length(data: bytes, pos: int) -> tuple[int, int]:
    """Обратная операция к ``_encode_ber_length`` — возвращает (длина,
    позиция_после_поля_длины)."""
    first = data[pos]
    if first & 0x80 == 0:
        return first, pos + 1
    n = first & 0x7F
    if n == 0 or pos + 1 + n > len(data):
        raise GatewayError("Некорректное BER-поле длины датаблока")
    length = int.from_bytes(data[pos + 1 : pos + 1 + n], "big")
    return length, pos + 1 + n


def build_get_response_datablock(
    invoke_id: int, *, last_block: bool, block_number: int, raw_data: bytes
) -> bytes:
    """Ответ-датаблок (используется эмулятором для проверки блочной
    передачи). Длина ``raw_data`` — стандартная BER-кодировка DLMS (см.
    ``_encode_ber_length``), как у реальных счётчиков, а не фиксированные
    2 байта, как было раньше."""
    return (
        bytes([GET_RESPONSE_TAG, GET_RESPONSE_WITH_DATABLOCK, invoke_id])
        + bytes([1 if last_block else 0])
        + block_number.to_bytes(4, "big")
        + bytes([DATABLOCK_RESULT_RAW_DATA])
        + _encode_ber_length(len(raw_data))
        + raw_data
    )


@dataclass
class DatablockResult:
    last_block: bool
    block_number: int
    raw_data: bytes


def parse_get_response_datablock(data: bytes) -> DatablockResult:
    if len(data) < 9 or data[0] != GET_RESPONSE_TAG or data[1] != GET_RESPONSE_WITH_DATABLOCK:
        raise GatewayError("Ожидался GET.response-with-datablock (тег 0xC4 0x02)")
    last_block = data[3] != 0
    block_number = int.from_bytes(data[4:8], "big")
    result_choice = data[8]
    if result_choice == DATABLOCK_RESULT_DATA_ACCESS_ERROR:
        code = data[9] if len(data) > 9 else -1
        raise GatewayError(f"Счётчик вернул data-access-result={code} на датаблоке #{block_number}")
    length, data_start = _decode_ber_length(data, 9)
    raw_data = data[data_start : data_start + length]
    return DatablockResult(last_block=last_block, block_number=block_number, raw_data=raw_data)


# --- Буфер профиля нагрузки DTZY217 — собственный (не стандартный DLMS
# common-data-type) построчный BCD-формат ---
#
# 2026-09-12 (реальные байтовые трассы 4 успешных сеансов
# IECMeterManage.exe, предоставленные пользователем — счётчики
# 202001002481/202004002987/202004003016/202004003111, см. DECISIONS.md)
# — GET.response на атрибут 2 (buffer) объекта Profile Generic для этой
# модели счётчика возвращает НЕ array-of-structure по стандартной
# DLMS-кодировке common-data-types (``datatypes.decode_value``), а
# собственный построчный формат, подтверждённый разбором декомпилированного
# `TpDLMS.cs::organizeFrame_GetLoadProfile`/`parseSingleValue` и
# `ReadLPDataForm_DLMS.cs::dateAnalysis`/`dataAnalysis` (`ver2.zip`) —
# каждая строка:
#   - маркер 0xA0 0xA0 (2 байта, разделитель строк — байты, которые не
#     могут встретиться внутри BCD-данных, см. ниже);
#   - метка времени: 6 байт, порядок байт ОБРАТНЫЙ (последний байт —
#     первый), каждый байт — 2 BCD-цифры; с префиксом века "20" разбирается
#     как yyyyMMddHHmmss;
#   - далее поля захватываемых колонок (порядок — как в capture_objects,
#     атрибут 3) до конца строки: один байт (старший ниббл — длина поля N
#     в байтах, младший ниббл — количество знаков после запятой S), затем
#     N байт значения в ОБРАТНОМ порядке байт, каждый байт — 2 BCD-цифры
#     (ведущий ниббл "F" после разворота — отрицательное значение, а не
#     цифра); значение = прочитанное как ДЕСЯТИЧНОЕ число / 10^S.
# Проверено на реальных данных: значения выходят физически осмысленными
# (напряжения ~255В, токи, cosφ 0.9-1.0, энергия — нарастающим итогом).
LOAD_PROFILE_ROW_MARKER = b"\xa0\xa0"


def split_load_profile_rows(buf: bytes) -> tuple[list[bytes], bytes]:
    """Разбивает накопленный буфер профиля нагрузки на ЗАВЕРШЁННЫЕ строки
    (по маркеру ``LOAD_PROFILE_ROW_MARKER``) и остаток, который ещё не
    сложился в полную строку (нужно ждать следующий датаблок — либо, если
    это последний датаблок, вызывающий код передаёт остаток сюда же ещё
    раз с ``final=True`` по смыслу вызова — см. ``read_load_profile_via_
    established_link``). Возвращает ``(строки, остаток)``; строка = все
    байты МЕЖДУ соседними вхождениями маркера, включая начальный маркер."""
    positions = []
    start = 0
    while True:
        pos = buf.find(LOAD_PROFILE_ROW_MARKER, start)
        if pos == -1:
            break
        positions.append(pos)
        start = pos + 1
    if len(positions) < 2:
        return [], buf
    rows = [buf[positions[i] : positions[i + 1]] for i in range(len(positions) - 1)]
    return rows, buf[positions[-1] :]


def _decode_bcd_reversed(raw: bytes) -> tuple[str, bool]:
    digits = raw[::-1].hex()
    negative = digits[:1].lower() == "f"
    if negative:
        digits = "0" + digits[1:]
    return digits, negative


def decode_load_profile_row(row: bytes) -> tuple[object, list[float]]:
    """Разбирает ОДНУ строку буфера профиля нагрузки (с ведущим маркером
    ``LOAD_PROFILE_ROW_MARKER``) в ``(datetime, [значения колонок])`` —
    см. описание формата выше."""
    from datetime import datetime

    if len(row) < 8 or row[:2] != LOAD_PROFILE_ROW_MARKER:
        raise GatewayError("Некорректная строка буфера профиля нагрузки (нет маркера A0 A0)")
    digits, _ = _decode_bcd_reversed(row[2:8])
    try:
        timestamp = datetime.strptime("20" + digits, "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise GatewayError(f"Некорректная метка времени строки буфера: {digits!r}") from exc
    pos = 8
    values: list[float] = []
    while pos < len(row):
        length_nibble = row[pos] >> 4
        scale_nibble = row[pos] & 0x0F
        pos += 1
        if length_nibble == 0 or pos + length_nibble > len(row):
            raise GatewayError("Строка буфера профиля нагрузки оборвана посреди поля значения")
        digits, negative = _decode_bcd_reversed(row[pos : pos + length_nibble])
        pos += length_nibble
        value = int(digits) / (10**scale_nibble)
        values.append(-value if negative else value)
    return timestamp, values


def encode_load_profile_row(timestamp, fields: list[tuple[float, int, int]]) -> bytes:
    """Обратная операция к ``decode_load_profile_row`` — используется
    тестовым эмулятором (реальный счётчик, естественно, ничего не
    кодирует этой функцией). ``fields`` — список ``(значение, число_байт_
    поля, число_знаков_после_запятой)`` в порядке колонок capture_objects."""

    def _bcd_reversed(digit_str: str) -> bytes:
        return bytes.fromhex(digit_str)[::-1]

    date_digits = timestamp.strftime("%Y%m%d%H%M%S")
    if date_digits[:2] != "20":
        raise GatewayError("encode_load_profile_row поддерживает только XXI век")
    out = bytearray(LOAD_PROFILE_ROW_MARKER)
    out += _bcd_reversed(date_digits[2:])
    for value, length, scale in fields:
        negative = value < 0
        scaled = round(abs(value) * (10**scale))
        digit_str = str(scaled)
        if len(digit_str) > length * 2:
            raise GatewayError(f"Значение {value} не помещается в поле длиной {length} байт")
        digit_str = digit_str.rjust(length * 2, "0")
        if negative:
            digit_str = "F" + digit_str[1:]
        out.append((length << 4) | scale)
        out += _bcd_reversed(digit_str)
    return bytes(out)


# --- ACTION-сервис (Этап 5, ТЗ п.4.2.10) — удалённое отключение/
# подключение счётчика через объект Disconnect Control (класс DLMS 70) ---
#
# Адрес объекта нигде не задокументирован в словаре OBIS.xlsx (проверено
# все 7 листов по ключевым словам disconnect/relay/отключ/реле) — та же
# ситуация, что была с профилем нагрузки в Этапе 3. По решению
# пользователя 2026-08-19 используется стандартный DLMS-адрес
# (decimal `0-0:96.3.10.255`, IEC 62056-6-2) как рабочая гипотеза,
# подлежащая проверке на реальном оборудовании — см. DECISIONS.md.
# В hex-нотации проекта (то же правило decimal->hex по полям, что и в
# Этапе 2/3: C=96->"60", D=3->"3", E=10->"a", F=255->"ff") — "0.0.60.3.a.ff".
#
# ACTION-request/response-normal (Green Book) — тот же принцип упрощённого
# APDU, что и у GET/SET выше: cosem-method-descriptor как фиксированные
# позиции (class-id, OBIS, method-id) вместо полного BER, единственный
# байт-результат в ответе (Action-Result, та же нумерация, что и
# Data-Access-Result у SET — оба перечня ENUMERATED из Green Book с
# идентичными кодами 0-14 для стандартных причин отказа).

ACTION_REQUEST_TAG = 0xC3
ACTION_REQUEST_NORMAL = 0x01
ACTION_RESPONSE_TAG = 0xC7
ACTION_RESPONSE_NORMAL = 0x01

ACTION_RESULT_SUCCESS = 0

DISCONNECT_CONTROL_CLASS_ID = 70
DISCONNECT_CONTROL_OBIS = "0.0.60.3.a.ff"
METHOD_REMOTE_DISCONNECT = 1
METHOD_REMOTE_RECONNECT = 2


def build_action_request(
    obis: bytes,
    method_id: int,
    *,
    class_id: int,
    invoke_id: int = 1,
    parameters: bytes | None = None,
) -> bytes:
    if len(obis) != 6:
        raise GatewayError("OBIS для ACTION.request должен быть ровно 6 байт")
    descriptor = class_id.to_bytes(2, "big") + obis + bytes([method_id])
    params_field = bytes([0x00]) if parameters is None else bytes([0x01]) + parameters
    return bytes([ACTION_REQUEST_TAG, ACTION_REQUEST_NORMAL, invoke_id]) + descriptor + params_field


@dataclass
class ParsedActionRequest:
    invoke_id: int
    class_id: int
    obis: bytes
    method_id: int
    parameters: bytes | None


def parse_action_request(data: bytes) -> ParsedActionRequest:
    if len(data) < 13 or data[0] != ACTION_REQUEST_TAG or data[1] != ACTION_REQUEST_NORMAL:
        raise GatewayError("Ожидался ACTION.request-normal (тег 0xC3 0x01)")
    invoke_id = data[2]
    class_id = int.from_bytes(data[3:5], "big")
    obis = data[5:11]
    method_id = data[11]
    has_parameters = data[12] == 0x01
    parameters = data[13:] if has_parameters and len(data) > 13 else None
    return ParsedActionRequest(
        invoke_id=invoke_id, class_id=class_id, obis=obis, method_id=method_id, parameters=parameters
    )


def build_action_response(invoke_id: int, *, result: int = ACTION_RESULT_SUCCESS) -> bytes:
    # Последний байт — presence-flag return-parameters, всегда 0x00
    # (отсутствуют): remote_disconnect/remote_reconnect ничего не
    # возвращают, кроме кода результата.
    return bytes([ACTION_RESPONSE_TAG, ACTION_RESPONSE_NORMAL, invoke_id, result, 0x00])


def parse_action_response(data: bytes) -> None:
    """Не возвращает значения — бросает GatewayError, если ACTION отклонён."""
    if len(data) < 4 or data[0] != ACTION_RESPONSE_TAG or data[1] != ACTION_RESPONSE_NORMAL:
        raise GatewayError("Ожидался ACTION.response-normal (тег 0xC7 0x01)")
    result = data[3]
    if result != ACTION_RESULT_SUCCESS:
        name = _SET_RESULT_NAMES.get(result, f"0x{result:02X}")
        raise GatewayError(f"Счётчик отклонил ACTION: result={result} ({name})")
