"""Общая инфраструктура программных эмуляторов счётчика.

ВАЖНО (Promt_MMWS.md, раздел «Этап 0», п. 4): эмуляторы в этом пакете —
программная имитация счётчика для интеграционных тестов Gateway. Они НЕ
заменяют проверку на реальном оборудовании, обязательную перед сдачей
Этапа 0.
"""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass
from typing import Callable


@dataclass
class ErrorInjection:
    """Управляет тем, какой сценарий ошибки связи (ТЗ Table 2) воспроизводит эмулятор.

    ``fail_attempts`` — число первых TCP-подключений подряд, на которых
    эмулятор ведёт себя ошибочно (для force_partial_disconnect/force_crc_error),
    прежде чем начать отвечать штатно; используется тестами восстановления
    после повтора (retry).
    """

    force_timeout: bool = False
    force_auth_fail: bool = False
    force_partial_disconnect: bool = False
    force_crc_error: bool = False
    fail_attempts: int = 1


class ConnectionCounter:
    """Потокобезопасный счётчик TCP-подключений к эмулятору (номер попытки клиента)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def next(self) -> int:
        with self._lock:
            self._count += 1
            return self._count


class ThreadedEmulatorServer:
    """Минимальный TCP-сервер на localhost, обслуживающий подключения в потоке.

    Каждое TCP-подключение обрабатывается последовательно (без пула
    потоков) — этого достаточно для интеграционных тестов, где Gateway
    открывает не более одного соединения к счётчику единовременно.
    """

    def __init__(
        self, handler: Callable[[socket.socket], None], host: str = "127.0.0.1"
    ) -> None:
        self._handler = handler
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((host, 0))
        self._server_socket.listen(5)
        self.host = host
        self.port = self._server_socket.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        self._server_socket.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handler(conn)
            except Exception:
                # Ошибка эмулятора не должна ронять поток сервера —
                # тест увидит проблему через поведение клиента (Gateway).
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def __enter__(self) -> "ThreadedEmulatorServer":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop = True
        self._thread.join(timeout=3)
        self._server_socket.close()


def recv_until(conn: socket.socket, terminator: bytes, max_len: int = 8192) -> bytes:
    buf = bytearray()
    while terminator not in buf:
        chunk = conn.recv(1)
        if not chunk:
            break
        buf += chunk
        if len(buf) > max_len:
            break
    return bytes(buf)


def recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)
