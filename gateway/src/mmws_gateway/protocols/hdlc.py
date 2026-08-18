"""HDLC-кадрирование (ТЗ п. 3.2, 4.3.2 — «HDLC + DLMS/COSEM»).

Формат кадра (DLMS/COSEM поверх HDLC, IEC 62056-46 / ISO 13239,
общее знание протокола — см. оговорку в ``datatypes.py`` и отчёт
Этапа 0):

    0x7E | Frame Format (2 байта) | Dest addr | Src addr | Control (1 байт)
        | HCS (2 байта, CRC16/X.25 от заголовка) | Info (N байт)
        | FCS (2 байта, CRC16/X.25 от заголовка+Info) | 0x7E

Frame Format: старшие 4 бита — тип кадра (0xA для формата без
сегментации), младшие 11 бит — длина кадра без открывающего/закрывающего
флага (себя включает), 12-й бит (segmentation) в Этапе 0 всегда 0 —
сегментация длинных ответов не реализована (ограничение PoC, полная
докачка — Этап 3 согласно Promt_MMWS.md).

Адресация HDLC (проверено на реальном оборудовании Risesun, 2026-08-18):
адресное поле — 1, 2 или 4 байта. Значение разбивается на 7-битные
группы (big-endian); каждый байт кодируется как
``(группа << 1) | признак_последнего_байта_ПОЛЯ_ЦЕЛИКОМ`` — признак
установлен только у самого последнего байта всего адресного поля, а не
у каждой логической части. Двухкомпонентный (4-байтный) адрес счётчика
— верхний адрес (логическое устройство, на практике всегда 1) и нижний
адрес (физический адрес счётчика) — кодируется как единое 28-битное
значение ``(upper << 14) | lower`` этой же схемой, см.
``server_hdlc_address``. Более раннее предположение PoC (адрес всегда
влезает в 7 бит, приводится по модулю 128) опровергнуто реальными
данными и удалено.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import CrcError, GatewayError
from ..transport import TcpTransport

FLAG = 0x7E
FRAME_TYPE_NIBBLE = 0xA0  # тип кадра «формат 3», без сегментации

DEFAULT_CLIENT_ADDRESS = 0x30  # подтверждено реальным трафиком (Risesun, оба счётчика)

_ADDRESS_LENGTHS = (1, 2, 4)  # допустимые длины адресного поля HDLC (3 байта не используются)


def crc16_x25(data: bytes) -> int:
    """CRC16/X.25 — контрольная сумма HCS/FCS кадров HDLC.

    Полином 0x1021 (отражённый 0x8408), начальное значение 0xFFFF,
    финальный XOR 0xFFFF — стандартный алгоритм ISO/IEC 13239.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return crc ^ 0xFFFF


def _encode_hdlc_address(value: int) -> bytes:
    if value < 0:
        raise ValueError(f"Адрес {value} отрицательный — недопустимо")
    for num_bytes in _ADDRESS_LENGTHS:
        if value < (1 << (7 * num_bytes)):
            chunks = [(value >> (7 * (num_bytes - 1 - i))) & 0x7F for i in range(num_bytes)]
            return bytes(
                (chunk << 1) | (1 if i == num_bytes - 1 else 0) for i, chunk in enumerate(chunks)
            )
    raise ValueError(
        f"Адрес {value} превышает максимум 4-байтной адресации HDLC (2**28 - 1)"
    )


def server_hdlc_address(physical_address: str, *, logical_device: int = 1) -> int:
    """Строит адрес счётчика для 4-байтной двухкомпонентной адресации.

    ``upper`` (логическое устройство, на практике всегда 1) и ``lower``
    (физический адрес счётчика) кодируются как единое 28-битное число
    ``(upper << 14) | lower`` — см. docstring модуля. Подтверждено
    реальным трафиком Risesun (``upper=1`` в обоих проверенных сеансах).
    """
    lower = int(physical_address)
    if not 0 <= logical_device < (1 << 14) or not 0 <= lower < (1 << 14):
        raise ValueError(
            f"Компоненты адреса вне диапазона 14 бит: logical_device={logical_device}, "
            f"physical_address={lower}"
        )
    return (logical_device << 14) | lower


@dataclass
class HdlcFrame:
    destination: int
    source: int
    control: int
    information: bytes = b""

    def encode(self) -> bytes:
        addr_field = _encode_hdlc_address(self.destination) + _encode_hdlc_address(
            self.source
        )
        header_wo_length = addr_field + bytes([self.control])
        # Длина по полю Frame Format исключает оба флага (0x7E) —
        # frame_format(2) + адреса+control + HCS(2) + info + FCS(2).
        length = 2 + len(header_wo_length) + 2 + len(self.information) + 2
        frame_format = (FRAME_TYPE_NIBBLE << 8) | length
        hcs = crc16_x25(frame_format.to_bytes(2, "big") + header_wo_length)
        body = (
            frame_format.to_bytes(2, "big")
            + header_wo_length
            + hcs.to_bytes(2, "little")
            + self.information
        )
        fcs = crc16_x25(body)
        return bytes([FLAG]) + body + fcs.to_bytes(2, "little") + bytes([FLAG])

    @staticmethod
    def decode(raw: bytes) -> "HdlcFrame":
        if len(raw) < 9 or raw[0] != FLAG or raw[-1] != FLAG:
            raise GatewayError("Некорректная структура HDLC-кадра (нет открывающего/закрывающего флага)")
        body = raw[1:-1]
        frame_format = int.from_bytes(body[0:2], "big")
        declared_len = frame_format & 0x07FF
        if declared_len != len(raw) - 2:
            raise CrcError(
                "Длина HDLC-кадра не совпадает с полем Frame Format",
                raw_frame=raw,
            )
        dest, dest_len = _decode_hdlc_address(body, 2)
        src, src_len = _decode_hdlc_address(body, 2 + dest_len)
        header_end = 2 + dest_len + src_len
        control = body[header_end]
        hcs_pos = header_end + 1
        hcs_received = int.from_bytes(body[hcs_pos : hcs_pos + 2], "little")
        hcs_computed = crc16_x25(body[0:hcs_pos])
        if hcs_received != hcs_computed:
            raise CrcError("Контрольная сумма заголовка HDLC (HCS) не сошлась", raw_frame=raw)
        info_start = hcs_pos + 2
        info_end = len(body) - 2
        information = body[info_start:info_end]
        fcs_received = int.from_bytes(body[info_end:], "little")
        fcs_computed = crc16_x25(body[0:info_end])
        if fcs_received != fcs_computed:
            raise CrcError("Контрольная сумма кадра HDLC (FCS) не сошлась", raw_frame=raw)
        return HdlcFrame(destination=dest, source=src, control=control, information=information)


def _decode_hdlc_address(body: bytes, offset: int) -> tuple[int, int]:
    """Разбирает адресное поле HDLC (1, 2 или 4 байта — см. docstring модуля).

    Читает байты, пока не встретит байт с установленным младшим битом
    (признак последнего байта поля); из них восстанавливает исходное
    значение обратной сборкой 7-битных групп.
    """
    value = 0
    length = 0
    max_len = _ADDRESS_LENGTHS[-1]
    while True:
        if offset + length >= len(body) or length >= max_len:
            raise GatewayError(
                f"Адресное поле HDLC превышает {max_len} байт без признака конца"
            )
        byte = body[offset + length]
        value = (value << 7) | (byte >> 1)
        length += 1
        if byte & 1:
            break
    return value, length


# Управляющие байты для установления/разрыва логического соединения HDLC.
CONTROL_SNRM = 0x93  # Set Normal Response Mode
CONTROL_UA = 0x73  # Unnumbered Acknowledge
CONTROL_DISC = 0x53  # Disconnect

# Согласование HDLC-параметров (max info length tx/rx, window size tx/rx)
# в информационном поле SNRM — байты сверены с реальным трафиком Risesun
# (одинаковы в обоих проверенных сеансах, не зависят от счётчика/пароля).
# Без этого поля реакция реального оборудования не проверялась — решено
# отправлять точно то же, что и рабочее legacy-приложение, а не
# полагаться на умолчания.
SNRM_PARAMETER_NEGOTIATION = bytes.fromhex("8180120501ff0601ff070400000001080400000001")


def control_information_frame(send_seq: int, recv_seq: int) -> int:
    """Control-байт информационного (I-) кадра с номерами N(S)/N(R).

    Бит Poll/Final всегда установлен (единичный запрос/ответ, без
    скользящего окна — достаточно для одной операции чтения в Этапе 0).
    """
    return ((recv_seq & 0x7) << 5) | 0x10 | ((send_seq & 0x7) << 1)


def read_frame_from_transport(transport: TcpTransport) -> bytes:
    """Читает один HDLC-кадр целиком, опираясь на длину из поля Frame
    Format — НЕ поиском закрывающего флага сканированием байт.

    Баг, найденный на практике (2026-08-18, при живой проверке с
    серийным номером 999000111222): байт 0x7E может случайно встретиться
    внутри тела кадра — в HCS/FCS, которые представляют собой CRC и
    могут принять любое значение байта. При росте вариативности адреса
    (4-байтная адресация вместо 1-байтной) вероятность такого совпадения
    выросла и стала регулярно обрезать кадр раньше времени при разборе
    через recv_until(FLAG). Настоящий HDLC устраняет это байт-стаффингом
    на битовом уровне физического канала; при переносе протокола поверх
    TCP (уже надёжного байтового потока) корректно и достаточно просто
    довериться длине, объявленной в самом кадре, а не искать флаг.
    """
    first = transport.recv_exact(1)
    if first != bytes([FLAG]):
        raise GatewayError(
            f"Ожидался открывающий флаг HDLC 0x{FLAG:02X}, получено: {first.hex()}"
        )
    frame_format = transport.recv_exact(2)
    declared_len = int.from_bytes(frame_format, "big") & 0x07FF
    if declared_len < 2:
        raise CrcError(
            "Некорректная длина в поле Frame Format HDLC-кадра",
            raw_frame=first + frame_format,
        )
    # declared_len считает от frame_format (включительно) до FCS
    # (включительно); frame_format (2 байта) уже прочитан, поэтому
    # остаётся declared_len - 2 байт тела и 1 байт закрывающего флага.
    rest = transport.recv_exact(declared_len - 2 + 1)
    if rest[-1:] != bytes([FLAG]):
        raise CrcError(
            "Не найден закрывающий флаг HDLC на ожидаемой по длине позиции",
            raw_frame=first + frame_format + rest,
        )
    return first + frame_format + rest
