"""Ошибки протокольного уровня Gateway.

Каждый сценарий ошибки связи со счётчиком (ТЗ, раздел 4.3.4, Table 2)
представлен отдельным классом с машиночитаемым атрибутом ``code``, чтобы
вызывающий код (CLI на Этапе 0, оркестратор задач на последующих этапах)
мог сопоставить ошибку со статусом задачи без разбора текста сообщения.
"""

from __future__ import annotations


class GatewayError(Exception):
    """Базовая ошибка протокольного слоя."""

    code = "GATEWAY_ERROR"

    def __init__(
        self,
        message: str,
        *,
        is_partial: bool = False,
        raw_frame: bytes | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        # Признак того, что часть данных уже была получена до обрыва
        # (ТЗ Table 2, «Обрыв соединения при чтении профиля нагрузки»).
        self.is_partial = is_partial
        # Сырой кадр в бинарном виде — для протоколирования в hex
        # (ТЗ Table 2, «Ошибка контрольной суммы / некорректный кадр»).
        self.raw_frame = raw_frame

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class MeterTimeoutError(GatewayError):
    """Счётчик не отвечает в течение сконфигурированного таймаута."""

    code = "TIMEOUT"


class AuthFailedError(GatewayError):
    """Неверный пароль доступа либо неверный физический/логический адрес.

    Автоматический повтор для этого сценария не выполняется (ТЗ Table 2).
    """

    code = "AUTH_FAILED"


class ConnectionLostError(GatewayError):
    """TCP-соединение оборвалось до получения полного ответа."""

    code = "CONNECTION_LOST"


class CrcError(GatewayError):
    """Контрольная сумма кадра (BCC для mode C, HCS/FCS для HDLC) не сошлась."""

    code = "CRC_ERROR"
