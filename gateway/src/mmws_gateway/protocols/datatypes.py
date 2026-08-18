"""Кодирование/декодирование типов данных DLMS Common-Data-Types.

Реализован минимально достаточный для Этапа 0 подмножество тегов —
те, что нужны для чтения скалярных значений регистров (класс DLMS 3,
атрибут 2) числовых OBIS-параметров из ТЗ Приложение Г (Table 7/8 и
лист «Типы_данных_кодирование» словаря OBIS): целочисленные форматы и
строки. Полный перечень типов Common-Data-Types (даты, битовые строки,
массивы структур и т. д.) выходит за рамки PoC и будет расширяться по
мере необходимости на следующих этапах.

Значения тегов соответствуют стандартной кодировке DLMS/COSEM
(IEC 62056-6-2, т.н. «Blue/Green Book») — это общее знание протокола,
не выгружено из ТЗ/словаря OBIS напрямую, и должно быть сверено при
тестировании на реальном оборудовании (см. отчёт Этапа 0).
"""

from __future__ import annotations

TAG_DOUBLE_LONG = 0x05  # int32, big-endian
TAG_DOUBLE_LONG_UNSIGNED = 0x06  # uint32, big-endian
TAG_OCTET_STRING = 0x09  # длина (1 байт) + сырые байты
TAG_VISIBLE_STRING = 0x0A  # длина (1 байт) + ASCII
TAG_INTEGER = 0x0F  # int8
TAG_LONG = 0x10  # int16, big-endian
TAG_UNSIGNED = 0x11  # uint8
TAG_LONG_UNSIGNED = 0x12  # uint16, big-endian

_TAG_NAMES = {
    TAG_DOUBLE_LONG: "double-long",
    TAG_DOUBLE_LONG_UNSIGNED: "double-long-unsigned",
    TAG_OCTET_STRING: "octet-string",
    TAG_VISIBLE_STRING: "visible-string",
    TAG_INTEGER: "integer",
    TAG_LONG: "long",
    TAG_UNSIGNED: "unsigned",
    TAG_LONG_UNSIGNED: "long-unsigned",
}


class DlmsDataError(ValueError):
    """Некорректные или неподдержанные данные DLMS Common-Data-Types."""


def tag_name(tag: int) -> str:
    return _TAG_NAMES.get(tag, f"0x{tag:02X}")


def encode_double_long_unsigned(value: int) -> bytes:
    return bytes([TAG_DOUBLE_LONG_UNSIGNED]) + value.to_bytes(4, "big", signed=False)


def encode_double_long(value: int) -> bytes:
    return bytes([TAG_DOUBLE_LONG]) + value.to_bytes(4, "big", signed=True)


def encode_long_unsigned(value: int) -> bytes:
    return bytes([TAG_LONG_UNSIGNED]) + value.to_bytes(2, "big", signed=False)


def encode_long(value: int) -> bytes:
    return bytes([TAG_LONG]) + value.to_bytes(2, "big", signed=True)


def encode_unsigned(value: int) -> bytes:
    return bytes([TAG_UNSIGNED]) + value.to_bytes(1, "big", signed=False)


def encode_integer(value: int) -> bytes:
    return bytes([TAG_INTEGER]) + value.to_bytes(1, "big", signed=True)


def encode_octet_string(value: bytes) -> bytes:
    if len(value) > 0x7F:
        raise DlmsDataError("Длины octet-string свыше 127 байт не поддержаны в Этапе 0")
    return bytes([TAG_OCTET_STRING, len(value)]) + value


def encode_visible_string(value: str) -> bytes:
    raw = value.encode("ascii")
    if len(raw) > 0x7F:
        raise DlmsDataError("Длины visible-string свыше 127 байт не поддержаны в Этапе 0")
    return bytes([TAG_VISIBLE_STRING, len(raw)]) + raw


def decode_value(data: bytes, offset: int = 0) -> tuple[object, int]:
    """Декодирует одно значение Common-Data-Type из ``data`` начиная с ``offset``.

    Возвращает кортеж (значение, число_считанных_байт).
    """
    if offset >= len(data):
        raise DlmsDataError("Пустые данные при разборе значения DLMS")
    tag = data[offset]
    pos = offset + 1

    if tag == TAG_DOUBLE_LONG_UNSIGNED:
        _require(data, pos, 4)
        return int.from_bytes(data[pos : pos + 4], "big", signed=False), 5
    if tag == TAG_DOUBLE_LONG:
        _require(data, pos, 4)
        return int.from_bytes(data[pos : pos + 4], "big", signed=True), 5
    if tag == TAG_LONG_UNSIGNED:
        _require(data, pos, 2)
        return int.from_bytes(data[pos : pos + 2], "big", signed=False), 3
    if tag == TAG_LONG:
        _require(data, pos, 2)
        return int.from_bytes(data[pos : pos + 2], "big", signed=True), 3
    if tag == TAG_UNSIGNED:
        _require(data, pos, 1)
        return data[pos], 2
    if tag == TAG_INTEGER:
        _require(data, pos, 1)
        return int.from_bytes(data[pos : pos + 1], "big", signed=True), 2
    if tag in (TAG_OCTET_STRING, TAG_VISIBLE_STRING):
        _require(data, pos, 1)
        length = data[pos]
        pos += 1
        _require(data, pos, length)
        raw = data[pos : pos + length]
        value = raw.decode("ascii") if tag == TAG_VISIBLE_STRING else raw
        return value, (pos + length) - offset

    raise DlmsDataError(f"Неподдержанный тег типа данных DLMS: 0x{tag:02X}")


def _require(data: bytes, pos: int, length: int) -> None:
    if pos + length > len(data):
        raise DlmsDataError("Данные DLMS обрываются раньше заявленной длины значения")


def decode_bcd_ascii_number(text: str) -> float:
    """Разбирает десятичное число из текстового readout mode C/E (ТЗ Table 7).

    Значения readout (например, «001234.567») передаются как ASCII-текст,
    а не как двоичный BCD, поэтому разбор — простое приведение типа;
    имя функции отражает происхождение формата (колонка «Кодирование»
    словаря OBIS указывает BCD для соответствующего двоичного представления
    внутри счётчика, на текстовом канале mode C оно уже разэкранировано).
    """
    return float(text)
