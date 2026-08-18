"""Программный эмулятор счётчика с протоколом IEC 62056-21 режим E.

Идентификационная преамбула — как в режиме C; после option select
message обмен продолжается по HDLC + DLMS/COSEM (переиспользует
``hdlc_dlms_emulator.serve_hdlc_dlms_session``).

Это ЭМУЛЯТОР для интеграционных тестов Gateway, а не замена реальным
полевым испытаниям (Promt_MMWS.md, раздел «Этап 0», п. 4).
"""

from __future__ import annotations

import socket
import time

from .common import ConnectionCounter, ErrorInjection, recv_exact, recv_until
from .hdlc_dlms_emulator import serve_hdlc_dlms_session

_TIMEOUT_STALL_S = 0.3


def make_mode_e_handler(
    *,
    serial: str,
    password: bytes,
    obis_values: dict[bytes, int],
    error_injection: ErrorInjection,
    counter: ConnectionCounter,
):
    """Возвращает обработчик TCP-подключения для ``ThreadedEmulatorServer``."""

    def handler(conn: socket.socket) -> None:
        conn.settimeout(5)
        attempt = counter.next()

        if error_injection.force_timeout:
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

        serve_hdlc_dlms_session(
            conn,
            password=password,
            obis_values=obis_values,
            error_injection=error_injection,
            attempt=attempt,
        )

    return handler
