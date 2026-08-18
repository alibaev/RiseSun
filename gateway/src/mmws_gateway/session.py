"""Оркестрация повторов целой операции чтения (ТЗ Table 1 + Table 2).

Решение по этому модулю (не зафиксировано дословно в ТЗ, см. отчёт
Этапа 0): ТЗ описывает "до 3 попыток с возрастающей задержкой" на уровне
транспорта (Table 1) и отдельно требует повтора запроса при ошибке
контрольной суммы (Table 2). Для Этапа 0 обе политики сведены в один
повтор всей операции (подключение + идентификация + чтение) — этого
достаточно для PoC одного запроса; на последующих этапах, где сессия со
счётчиком переиспользуется для нескольких операций подряд, повтор,
вероятно, потребуется сделать более гранулярным.

Ошибка AUTH_FAILED никогда не повторяется (явное требование Table 2).
"""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

from .errors import AuthFailedError, ConnectionLostError, CrcError, GatewayError, MeterTimeoutError

logger = logging.getLogger("mmws_gateway.session")

_RETRYABLE = (MeterTimeoutError, CrcError, ConnectionLostError)

T = TypeVar("T")


def run_with_retries(operation: Callable[[], T], *, max_retries: int = 3) -> T:
    """Выполняет ``operation`` с повтором при ретраябельных ошибках связи."""
    delay = 0.5
    last_exc: GatewayError | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return operation()
        except AuthFailedError:
            raise
        except _RETRYABLE as exc:
            last_exc = exc
            if isinstance(exc, CrcError) and exc.raw_frame is not None:
                logger.warning(
                    "Ошибка контрольной суммы, сырой кадр: %s", exc.raw_frame.hex()
                )
            if attempt < max_retries:
                logger.warning(
                    "Попытка %s/%s операции завершилась ошибкой %s: %s",
                    attempt,
                    max_retries,
                    exc.code,
                    exc.message,
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise
    assert last_exc is not None
    raise last_exc
