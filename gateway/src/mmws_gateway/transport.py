"""Транспортный уровень: TCP-соединение со счётчиком (ТЗ п. 4.3.1, Table 1).

Gateway всегда выступает инициатором TCP-соединения к IP-адресу и порту
счётчика. Таймаут и число повторов конфигурируемы, по умолчанию —
5000 мс и 3 попытки с возрастающей задержкой.
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass

from .errors import ConnectionLostError, GatewayError, MeterTimeoutError

logger = logging.getLogger("mmws_gateway.transport")

DEFAULT_TIMEOUT_MS = 5000
DEFAULT_RETRIES = 3
_INITIAL_RETRY_DELAY_S = 0.5


@dataclass
class TransportConfig:
    host: str
    port: int
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    max_retries: int = DEFAULT_RETRIES


class TcpTransport:
    """Обёртка над TCP-сокетом с явными таймаутами чтения/записи.

    Повтор попыток на уровне установления соединения выполняется здесь
    (``connect_with_retries``). Повтор целой операции запрос/ответ при
    ошибке контрольной суммы — задача вызывающего протокольного слоя
    (см. ``session.run_with_retries``), т.к. только он знает, какой
    запрос нужно переотправить.
    """

    def __init__(self, config: TransportConfig) -> None:
        self._config = config
        self._sock: socket.socket | None = None

    def connect_with_retries(self) -> None:
        delay = _INITIAL_RETRY_DELAY_S
        last_exc: Exception | None = None
        for attempt in range(1, self._config.max_retries + 1):
            try:
                self._connect_once()
                return
            except OSError as exc:
                last_exc = exc
                logger.warning(
                    "Попытка %s/%s подключения к %s:%s не удалась: %s",
                    attempt,
                    self._config.max_retries,
                    self._config.host,
                    self._config.port,
                    exc,
                )
                if attempt < self._config.max_retries:
                    time.sleep(delay)
                    delay *= 2
        raise MeterTimeoutError(
            f"Не удалось подключиться к {self._config.host}:{self._config.port} "
            f"после {self._config.max_retries} попыток: {last_exc}"
        )

    def _connect_once(self) -> None:
        timeout_s = self._config.timeout_ms / 1000
        sock = socket.create_connection(
            (self._config.host, self._config.port), timeout=timeout_s
        )
        sock.settimeout(timeout_s)
        self._sock = sock

    def send(self, data: bytes) -> None:
        assert self._sock is not None, "Транспорт не подключён"
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise ConnectionLostError(f"Обрыв при отправке данных: {exc}") from exc

    def recv_until(self, terminator: bytes, max_len: int = 8192) -> bytes:
        """Читает байты, пока не встретится ``terminator`` (включительно)."""
        assert self._sock is not None
        buf = bytearray()
        try:
            while terminator not in buf:
                chunk = self._sock.recv(1)
                if not chunk:
                    raise ConnectionLostError(
                        "Соединение закрыто счётчиком до получения полного ответа",
                        is_partial=len(buf) > 0,
                        raw_frame=bytes(buf),
                    )
                buf += chunk
                if len(buf) > max_len:
                    raise GatewayError(
                        "Превышен максимальный размер ответа счётчика",
                        raw_frame=bytes(buf),
                    )
        except socket.timeout as exc:
            raise MeterTimeoutError(
                "Таймаут ожидания ответа счётчика",
                is_partial=len(buf) > 0,
                raw_frame=bytes(buf),
            ) from exc
        return bytes(buf)

    def recv_exact(self, n: int) -> bytes:
        """Читает ровно ``n`` байт (используется при разборе HDLC-кадров)."""
        assert self._sock is not None
        buf = bytearray()
        try:
            while len(buf) < n:
                chunk = self._sock.recv(n - len(buf))
                if not chunk:
                    raise ConnectionLostError(
                        "Соединение закрыто счётчиком при частичном чтении кадра",
                        is_partial=len(buf) > 0,
                        raw_frame=bytes(buf),
                    )
                buf += chunk
        except socket.timeout as exc:
            raise MeterTimeoutError(
                "Таймаут при чтении кадра",
                is_partial=len(buf) > 0,
                raw_frame=bytes(buf),
            ) from exc
        return bytes(buf)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "TcpTransport":
        self.connect_with_retries()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
