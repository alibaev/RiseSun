"""Протокольный профиль IEC 62056-21, режим C (ТЗ п. 4.3.2, первый профиль).

Последовательность запроса/ответа (IEC 62056-21, режим C):
    1. Клиент отправляет запрос идентификации: ``/?<адрес>!<CR><LF>``.
    2. Счётчик отвечает сообщением идентификации:
       ``/<произв.><символ_скорости><идентификация><CR><LF>``.
    3. Клиент отправляет «option select message» (ACK) с указанием
       режима работы и подтверждённого символа скорости.
    4. В режиме C (в отличие от режима E) счётчик сразу отдаёт
       текстовый data readout: ``STX <строки_данных> ETX BCC``, где
       BCC — XOR всех байт между STX (не включая) и ETX (включая).

Формат ACK/option-select message (последовательность управляющих
символов P1/P2/P3 после ACK) — общее знание протокола IEC 62056-21,
не выгружено из ТЗ дословно; используется здесь в объёме, достаточном
для согласованной работы с собственным эмулятором (см. отчёт Этапа 0).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..errors import AuthFailedError, CrcError, GatewayError
from .datatypes import decode_bcd_ascii_number

ACK = 0x06
NAK = 0x15
STX = 0x02
ETX = 0x03
CRLF = b"\r\n"

_DATA_LINE_RE = re.compile(r"^([0-9.]+)\(([^*)]*)(?:\*([^)]*))?\)$")


def obis6_to_short_code(obis_6: str) -> str:
    """Приводит полный 6-байтный OBIS (``A.B.C.D.E.F``, hex-поля) к короткому
    коду data readout режима C (``C.D.E``, десятичное представление).

    Решение (см. отчёт Этапа 0): текстовый data readout режима C
    исторически использует укороченную нотацию из трёх полей в
    десятичном виде (например, «1.8.0»), тогда как словарь OBIS и
    профиль HDLC-DLMS оперируют полным 6-байтным шестнадцатеричным
    кодом. Однозначного правила пересчёта в ТЗ/словаре OBIS не
    зафиксировано — данное сопоставление (поля C.D.E, hex -> int)
    подобрано по образцу представительной выборки ТЗ Table 8 и
    подлежит проверке на реальном оборудовании.
    """
    parts = obis_6.split(".")
    if len(parts) != 6:
        raise GatewayError(
            f"OBIS-код должен состоять из 6 полей вида A.B.C.D.E.F, получено: {obis_6!r}"
        )
    c, d, e = (int(parts[i], 16) for i in (2, 3, 4))
    return f"{c}.{d}.{e}"


def build_identification_request(serial: str) -> bytes:
    return b"/?" + serial.encode("ascii") + b"!\r\n"


@dataclass
class IdentificationMessage:
    manufacturer: str
    baud_char: str
    identification: str


def parse_identification(raw: bytes) -> IdentificationMessage:
    text = raw.decode("ascii", errors="strict").strip("\r\n")
    if not text.startswith("/") or len(text) < 5:
        raise GatewayError(f"Некорректное сообщение идентификации счётчика: {raw!r}")
    manufacturer = text[1:4]
    baud_char = text[4]
    identification = text[5:]
    return IdentificationMessage(
        manufacturer=manufacturer, baud_char=baud_char, identification=identification
    )


def build_option_select(baud_char: str, *, mode: str = "0") -> bytes:
    """Строит option select message.

    ``mode``: "0" — продолжить обмен в режиме C (текстовый data readout,
    используется профилем mode_c); "2" — переход на кадрирование HDLC
    (используется профилем mode_e).
    """
    return bytes([ACK]) + mode.encode("ascii") + baud_char.encode("ascii") + b"0\r\n"


def compute_bcc(data: bytes) -> int:
    result = 0
    for byte in data:
        result ^= byte
    return result


@dataclass
class DataReadout:
    values: dict[str, tuple[str, str]]  # obis -> (raw_value, unit)


def parse_data_readout(stx_byte: bytes, body_with_etx: bytes, bcc_byte: bytes) -> DataReadout:
    if stx_byte != bytes([STX]):
        raise GatewayError(f"Ожидался STX в начале data readout, получено: {stx_byte!r}")
    if not body_with_etx or body_with_etx[-1] != ETX:
        raise GatewayError("Data readout не завершается байтом ETX")
    computed_bcc = compute_bcc(body_with_etx)
    if not bcc_byte or computed_bcc != bcc_byte[0]:
        raise CrcError(
            "Контрольная сумма (BCC) data readout не сошлась",
            raw_frame=stx_byte + body_with_etx + bcc_byte,
        )
    text = body_with_etx[:-1].decode("ascii", errors="strict")
    values: dict[str, tuple[str, str]] = {}
    for line in text.split("\r\n"):
        line = line.strip()
        if not line or line == "!":
            continue
        match = _DATA_LINE_RE.match(line)
        if not match:
            continue
        obis, value, unit = match.group(1), match.group(2), match.group(3) or ""
        values[obis] = (value, unit)
    return DataReadout(values=values)


def read_value(transport, *, serial: str, obis_5: str) -> float:
    """Полный цикл чтения одного значения в режиме C.

    ``obis_5`` — код в короткой нотации data readout (например, «1.8.0»,
    как он реально появляется в текстовых строках счётчика — без полей
    E/F шестибайтового DLMS-кода, используемых в профиле HDLC-DLMS).
    """
    transport.send(build_identification_request(serial))
    ident_raw = transport.recv_until(CRLF)
    ident = parse_identification(ident_raw)

    transport.send(build_option_select(ident.baud_char, mode="0"))

    stx_byte = transport.recv_exact(1)
    if stx_byte == bytes([NAK]):
        raise AuthFailedError(
            f"Счётчик {serial} отклонил запрос (NAK) — неверный адрес доступа"
        )
    body_with_etx = transport.recv_until(bytes([ETX]))
    bcc_byte = transport.recv_exact(1)
    readout = parse_data_readout(stx_byte, body_with_etx, bcc_byte)

    if obis_5 not in readout.values:
        raise GatewayError(
            f"OBIS-код {obis_5!r} отсутствует в data readout счётчика {serial}"
        )
    raw_value, _unit = readout.values[obis_5]
    return decode_bcd_ascii_number(raw_value)
