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

Адреса HDLC кодируются однобайтно: (адрес << 1) | признак_последнего_байта.
В Этапе 0 поддержаны только физические адреса, умещающиеся в 7 бит
(0..127) — этого достаточно для последних 5 цифр серийного номера,
приведённых по модулю (см. ``encode_server_address``); при выходе за
диапазон потребуется двух- или трёхбайтная адресация DLMS (не
реализована в PoC).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import CrcError, GatewayError
from ..transport import TcpTransport

FLAG = 0x7E
FRAME_TYPE_NIBBLE = 0xA0  # тип кадра «формат 3», без сегментации

DEFAULT_CLIENT_ADDRESS = 0x10  # публичный клиент (общепринятое значение Green Book)


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
    if not 0 <= value <= 0x7F:
        raise ValueError(
            f"Адрес {value} вне диапазона однобайтной адресации HDLC (0..127), "
            "поддержанной в Этапе 0"
        )
    return bytes([(value << 1) | 1])


def encode_server_address(physical_address: str) -> int:
    """Приводит физический адрес счётчика (строка цифр) к 7-битному адресу HDLC."""
    return int(physical_address) % 0x80


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
    """Разбирает однобайтный адрес HDLC (бит расширения — младший бит)."""
    byte = body[offset]
    if not byte & 1:
        raise GatewayError(
            "Многобайтная адресация HDLC не поддержана в Этапе 0 (см. ограничение PoC)"
        )
    return byte >> 1, 1


# Управляющие байты для установления/разрыва логического соединения HDLC.
CONTROL_SNRM = 0x93  # Set Normal Response Mode
CONTROL_UA = 0x73  # Unnumbered Acknowledge
CONTROL_DISC = 0x53  # Disconnect


def control_information_frame(send_seq: int, recv_seq: int) -> int:
    """Control-байт информационного (I-) кадра с номерами N(S)/N(R).

    Бит Poll/Final всегда установлен (единичный запрос/ответ, без
    скользящего окна — достаточно для одной операции чтения в Этапе 0).
    """
    return ((recv_seq & 0x7) << 5) | 0x10 | ((send_seq & 0x7) << 1)


def read_frame_from_transport(transport: TcpTransport) -> bytes:
    """Читает один HDLC-кадр целиком (от открывающего до закрывающего флага)."""
    first = transport.recv_exact(1)
    if first != bytes([FLAG]):
        raise GatewayError(
            f"Ожидался открывающий флаг HDLC 0x{FLAG:02X}, получено: {first.hex()}"
        )
    rest = transport.recv_until(bytes([FLAG]))
    return first + rest
