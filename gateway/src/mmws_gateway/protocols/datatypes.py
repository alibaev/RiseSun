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

TAG_NULL_DATA = 0x00  # без значения — сам по себе один байт
TAG_ARRAY = 0x01  # count (1 байт) + N вложенных значений
TAG_STRUCTURE = 0x02  # count (1 байт) + N вложенных значений
TAG_DOUBLE_LONG = 0x05  # int32, big-endian
TAG_DOUBLE_LONG_UNSIGNED = 0x06  # uint32, big-endian
TAG_OCTET_STRING = 0x09  # длина (1 байт) + сырые байты
TAG_VISIBLE_STRING = 0x0A  # длина (1 байт) + ASCII
TAG_INTEGER = 0x0F  # int8
TAG_LONG = 0x10  # int16, big-endian
TAG_UNSIGNED = 0x11  # uint8
TAG_LONG_UNSIGNED = 0x12  # uint16, big-endian
TAG_LONG64 = 0x14  # int64, big-endian
TAG_LONG64_UNSIGNED = 0x15  # uint64, big-endian
TAG_ENUM = 0x16  # uint8 (перечисление, напр. код единицы измерения)
TAG_DATE_TIME = 0x19  # 12 сырых байт (год/месяц/день/... — см. encode_cosem_date_time), БЕЗ байта длины

# array/structure(0x02)/long64/enum добавлены по итогам разбора реального
# трафика Risesun 2026-08-18 (структура scaler_unit, вендорское значение
# показания у одного из счётчиков в int64) — не входили в минимальный
# набор Этапа 0.

_TAG_NAMES = {
    TAG_ARRAY: "array",
    TAG_STRUCTURE: "structure",
    TAG_DOUBLE_LONG: "double-long",
    TAG_DOUBLE_LONG_UNSIGNED: "double-long-unsigned",
    TAG_OCTET_STRING: "octet-string",
    TAG_VISIBLE_STRING: "visible-string",
    TAG_INTEGER: "integer",
    TAG_LONG: "long",
    TAG_UNSIGNED: "unsigned",
    TAG_LONG_UNSIGNED: "long-unsigned",
    TAG_LONG64: "long64",
    TAG_LONG64_UNSIGNED: "long64-unsigned",
    TAG_ENUM: "enum",
    TAG_DATE_TIME: "date_time",
}


class DlmsDataError(ValueError):
    """Некорректные или неподдержанные данные DLMS Common-Data-Types."""


def tag_name(tag: int) -> str:
    return _TAG_NAMES.get(tag, f"0x{tag:02X}")


def encode_null() -> bytes:
    """NULL-DATA (тег 0x00, без значения) — 2026-09-10, см. DECISIONS.md:
    найдено побайтовым разбором декомпилированного ``ver2.zip``
    (``TpDLMS.cs::organizeFrame_GetLoadProfile``), что заводская
    сервисная программа отправляет ИМЕННО NULL, а не структуру
    Clock-объекта, в поле ``restricting_object`` access-parameters для
    range-descriptor при чтении профиля нагрузки — счётчик, у которого
    Clock не входит в захватываемые колонки буфера, отвергал нашу
    Clock-структуру кодом data-access-result=250."""
    return bytes([TAG_NULL_DATA])


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


def encode_structure(items: list[bytes]) -> bytes:
    """Этап 3 — нужно для access-selection (range-descriptor) при чтении
    профиля нагрузки. Как и у ``decode_value`` ниже, счётчик элементов —
    один байт (0-255), без полной BER-длины произвольного размера (тот
    же упрощённый принцип, что и у остальных Common-Data-Types здесь)."""
    if len(items) > 0xFF:
        raise DlmsDataError("Структуры свыше 255 элементов не поддержаны в Этапе 3")
    return bytes([TAG_STRUCTURE, len(items)]) + b"".join(items)


def encode_array(items: list[bytes]) -> bytes:
    if len(items) > 0xFF:
        raise DlmsDataError("Массивы свыше 255 элементов не поддержаны в Этапе 3")
    return bytes([TAG_ARRAY, len(items)]) + b"".join(items)


# DLMS cosem-date-time (12 сырых байт, Green Book): год(2, BE) + месяц +
# день + день_недели(1=Пн..7=Вс, 0xff=не задан) + час + минута + секунда
# + сотые_доли(0xff=не заданы) + отклонение_от_UTC(2, BE, минуты,
# 0x8000=не задано) + статус(0xff=не задан).
def encode_cosem_date_time(dt) -> bytes:
    dow = dt.isoweekday()
    return (
        dt.year.to_bytes(2, "big")
        + bytes([dt.month, dt.day, dow, dt.hour, dt.minute, dt.second, 0xFF])
        + (0x8000).to_bytes(2, "big")
        + bytes([0xFF])
    )


def encode_date_time(dt) -> bytes:
    """2026-09-12, по просьбе пользователя ("покопай, может тут сдвиг
    байта") — найдено в декомпилированном GXDLMSReader.cs
    (`RS_PostProcessingProfileGenericsDates`, см. DECISIONS.md):
    легаси-программа ХИРУРГИЧЕСКИ патчит исходящий запрос диапазона от
    стандартной библиотеки Gurux.DLMS, заменяя байты ``09 0C`` (тег
    ``octet-string`` + явная длина 12 — именно так ``build_get_request_
    range`` кодировал from/to ДО этого фикса) на ОДИН байт ``0x19`` —
    отдельный Common-Data-Type тег ``date_time`` (Green Book): 12 сырых
    байт БЕЗ отдельного байта длины (длина у него фиксированная и
    подразумевается самим тегом, в отличие от octet-string). Судя по
    этому патчу, прошивка счётчика (или её DLMS-стек) не разбирает дату
    в range-descriptor, завёрнутую в octet-string, и ждёт именно этот
    "родной" тег."""
    return bytes([TAG_DATE_TIME]) + encode_cosem_date_time(dt)


def decode_cosem_date_time(raw: bytes):
    """Обратное преобразование — используется при разборе строк буфера
    профиля нагрузки, где первой колонкой обычно идёт метка времени."""
    from datetime import datetime

    if len(raw) != 12:
        raise DlmsDataError(f"cosem-date-time должен быть ровно 12 байт, получено {len(raw)}")
    year = int.from_bytes(raw[0:2], "big")
    month, day = raw[2], raw[3]
    hour, minute, second = raw[5], raw[6], raw[7]
    return datetime(year, month, day, hour, minute, second)


def decode_value(data: bytes, offset: int = 0) -> tuple[object, int]:
    """Декодирует одно значение Common-Data-Type из ``data`` начиная с ``offset``.

    Возвращает кортеж (значение, число_считанных_байт).
    """
    if offset >= len(data):
        raise DlmsDataError("Пустые данные при разборе значения DLMS")
    tag = data[offset]
    pos = offset + 1

    if tag == TAG_NULL_DATA:
        # 2026-09-11 — найдено на новой партии счётчиков: значение может
        # легитимно прийти как null-data (нет данных прямо сейчас), а не
        # только использоваться нами как restricting-object в GET-запросе
        # диапазона (см. encode_null() выше). Раньше здесь не было ветки
        # разбора вовсе — падало "Неподдержанный тег типа данных DLMS: 0x00".
        return None, 1
    if tag == TAG_DOUBLE_LONG_UNSIGNED:
        _require(data, pos, 4)
        return int.from_bytes(data[pos : pos + 4], "big", signed=False), 5
    if tag == TAG_DOUBLE_LONG:
        _require(data, pos, 4)
        return int.from_bytes(data[pos : pos + 4], "big", signed=True), 5
    if tag == TAG_LONG64_UNSIGNED:
        _require(data, pos, 8)
        return int.from_bytes(data[pos : pos + 8], "big", signed=False), 9
    if tag == TAG_LONG64:
        _require(data, pos, 8)
        return int.from_bytes(data[pos : pos + 8], "big", signed=True), 9
    if tag == TAG_LONG_UNSIGNED:
        _require(data, pos, 2)
        return int.from_bytes(data[pos : pos + 2], "big", signed=False), 3
    if tag == TAG_LONG:
        _require(data, pos, 2)
        return int.from_bytes(data[pos : pos + 2], "big", signed=True), 3
    if tag in (TAG_UNSIGNED, TAG_ENUM):
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
    if tag in (TAG_ARRAY, TAG_STRUCTURE):
        _require(data, pos, 1)
        count = data[pos]
        pos += 1
        items = []
        for _ in range(count):
            item, consumed = decode_value(data, offset=pos)
            items.append(item)
            pos += consumed
        return items, pos - offset
    if tag == TAG_DATE_TIME:
        _require(data, pos, 12)
        return decode_cosem_date_time(data[pos : pos + 12]), 13

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
