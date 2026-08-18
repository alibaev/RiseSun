"""Программный эмулятор счётчика с протоколом IEC 62056-21 режим C.

Это ЭМУЛЯТОР для интеграционных тестов Gateway, а не замена реальным
полевым испытаниям (Promt_MMWS.md, раздел «Этап 0», п. 4).
"""

from __future__ import annotations

import socket
import time

from ..protocols.mode_c import ETX, STX, compute_bcc
from .common import ConnectionCounter, ErrorInjection, recv_exact, recv_until

_TIMEOUT_STALL_S = 0.3


def make_mode_c_handler(
    *,
    serial: str,
    readings: dict[str, str],
    error_injection: ErrorInjection,
    counter: ConnectionCounter,
):
    """Возвращает обработчик TCP-подключения для ``ThreadedEmulatorServer``."""

    def handler(conn: socket.socket) -> None:
        conn.settimeout(5)
        attempt = counter.next()

        if error_injection.force_timeout:
            # Соединение принято, но эмулятор намеренно молчит дольше,
            # чем клиентский таймаут — воспроизводит сценарий TIMEOUT.
            time.sleep(_TIMEOUT_STALL_S)
            return

        request = recv_until(conn, b"!\r\n")
        if not request.startswith(b"/?") or not request.endswith(b"!\r\n"):
            return

        ident = f"/RSN5{serial}\r\n".encode("ascii")
        conn.sendall(ident)

        ack = recv_exact(conn, 6)
        if not ack:
            return

        if error_injection.force_auth_fail:
            conn.sendall(b"\x15")  # NAK — счётчик отклоняет запрос
            return

        lines = "".join(f"{obis}({value})\r\n" for obis, value in readings.items())
        lines += "!\r\n"
        body = lines.encode("ascii") + bytes([ETX])

        if error_injection.force_partial_disconnect and attempt <= error_injection.fail_attempts:
            conn.sendall(bytes([STX]) + body[: len(body) // 2])
            return

        if error_injection.force_crc_error and attempt <= error_injection.fail_attempts:
            corrupted_bcc = (compute_bcc(body) ^ 0xFF) & 0xFF
            conn.sendall(bytes([STX]) + body + bytes([corrupted_bcc]))
            return

        bcc = compute_bcc(body)
        conn.sendall(bytes([STX]) + body + bytes([bcc]))

    return handler
