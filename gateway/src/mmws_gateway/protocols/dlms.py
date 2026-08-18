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

REGISTER_CLASS_ID = 3
REGISTER_VALUE_ATTRIBUTE = 2


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


def build_aarq(password: bytes) -> bytes:
    """Строит AARQ с calling-authentication-value = пароль низкого уровня."""
    oid = encode_oid(APPLICATION_CONTEXT_LN_NO_CIPHERING)
    application_context = bytes([0xA1, len(oid) + 2, 0x06, len(oid)]) + oid
    auth_value = bytes([0x80, len(password)]) + password
    calling_auth = bytes([0xAC, len(auth_value)]) + auth_value
    body = application_context + calling_auth
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


def parse_aare(data: bytes) -> bool:
    """Возвращает True, если ассоциация принята; иначе бросает AuthFailedError."""
    if not data or data[0] != AARE_TAG:
        raise GatewayError("Ожидался AARE (тег 0x61)")
    body = data[2 : 2 + data[1]]
    if len(body) < 5 or body[0] != 0xA2:
        raise GatewayError("Некорректное поле result в AARE")
    result = body[4]
    if result == AARE_RESULT_ACCEPTED:
        return True
    raise AuthFailedError("Счётчик отклонил ассоциацию (неверный пароль доступа)")


def build_get_request(obis: bytes, *, invoke_id: int = 1) -> bytes:
    if len(obis) != 6:
        raise GatewayError("OBIS для GET.request должен быть ровно 6 байт")
    descriptor = (
        REGISTER_CLASS_ID.to_bytes(2, "big")
        + obis
        + bytes([REGISTER_VALUE_ATTRIBUTE])
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
    value, _consumed = datatypes.decode_value(data, offset=4)
    return value
