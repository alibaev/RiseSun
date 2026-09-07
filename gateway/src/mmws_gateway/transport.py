"""Транспортный уровень: TCP-соединение со счётчиком (ТЗ п. 4.3.1, Table 1).

ТЗ описывает Gateway как инициатора TCP-соединения к IP-адресу и порту
счётчика — так и работает ``TcpTransport`` ниже. Таймаут и число
повторов конфигурируемы, по умолчанию — 5000 мс и 3 попытки с
возрастающей задержкой.

Проверка на реальном оборудовании Risesun (2026-08-18, см.
DECISIONS.md) выявила расхождение с этой моделью: как минимум часть
счётчиков сама инициирует TCP-соединение к Gateway («звонок домой»,
типично для GPRS-счётчиков без статического IP/за NAT оператора).
``TcpServerTransport`` ниже — транспорт для этого сценария: не
подключается сам, а оборачивает уже принятое (``accept()``) соединение.
Полноценная архитектура серверного режима (постоянный листенер,
сопоставление входящего соединения с записью в реестре ``meters`` по
объявленному счётчиком адресу, keepalive) — вне рамок Этапа 0, это
строительный блок для будущей реализации.
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

    def reset_frame_seeking(self) -> None:
        """Хук, дающий обёрнутому сокету шанс сбросить фильтрацию
        "мусора" перед ожиданием НОВОГО HDLC-кадра (см.
        ``protocols.hdlc.read_frame_from_transport``, вызывается перед
        КАЖДЫМ кадром). Для обычного сокета (прямое TCP-подключение к
        счётчику, без call-home-фильтрации) — no-op; для
        ``callhome.DlT645FilteringSocket`` делегирует в его
        ``reset_seeking()`` (2026-09-07, найденный баг: раньше фильтрация
        включалась заново вручную только перед попытками SNRM, поэтому
        DL/T645-анонс счётчика, пришедший ПОСЛЕ установления HDLC-связи
        — например, в середине многокадрового чтения профиля нагрузки —
        не фильтровался и ломал разбор следующего кадра)."""
        reset = getattr(self._sock, "reset_seeking", None)
        if reset is not None:
            reset()

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


class TcpServerTransport(TcpTransport):
    """Транспорт поверх уже принятого (``accept()``) входящего соединения.

    Используется для счётчиков в режиме «звонок домой» (см. docstring
    модуля). В отличие от базового ``TcpTransport``, не подключается сам
    — ``connect_with_retries()`` для этого класса недопустим.
    """

    @classmethod
    def from_accepted_socket(
        cls,
        sock: socket.socket,
        *,
        peer_host: str,
        peer_port: int,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> "TcpServerTransport":
        transport = cls.__new__(cls)
        transport._config = TransportConfig(
            host=peer_host, port=peer_port, timeout_ms=timeout_ms, max_retries=1
        )
        sock.settimeout(timeout_ms / 1000)
        transport._sock = sock
        return transport

    def connect_with_retries(self) -> None:
        raise GatewayError(
            "TcpServerTransport оборачивает уже принятое соединение — "
            "connect_with_retries() здесь не применим"
        )

    def __enter__(self) -> "TcpServerTransport":
        return self
