"""Аутентификация и rate limiting для /api/v1/billing/* (ТЗ п.4.2.9,
API.docx раздел 3, 6) — отдельная выделенная техническая учётная запись,
НЕ связанная с JWT-сессиями пользователей веб-интерфейса (app/auth/deps.py).
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import verify_password
from ..db import get_db
from ..models import BillingApiKey, BillingApiKeyStatus
from ..services.billing_errors import BillingApiError

_RATE_WINDOW_S = 60.0
# Скользящее окно запросов в памяти процесса — сознательное упрощение
# (см. аналогичное решение в Этапе 4 для финализации запуска расписания):
# при нескольких экземплярах Backend (ТЗ п.4.6, горизонтальное
# масштабирование) лимит проверяется НЕЗАВИСИМО на каждом экземпляре, то
# есть фактический совокупный лимит может быть выше номинального в
# N раз (N — число экземпляров). Для PoC и ожидаемого числа биллинг-
# интеграций (как правило, одна) сочтено приемлемым; в проде — вынести
# в общее хранилище (Redis INCR + EXPIRE).
_REQUEST_LOG: dict[int, deque[float]] = {}


async def require_billing_api_key(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> BillingApiKey:
    if not authorization or not authorization.startswith("Bearer "):
        raise BillingApiError(401, "UNAUTHORIZED", "Отсутствует либо недействителен API-ключ")
    raw_key = authorization.removeprefix("Bearer ").strip()

    # Ключ хешируется (bcrypt, как и пароли пользователей) — не подлежит
    # поиску по индексу, поэтому перебираем активные ключи. Приемлемо при
    # ожидаемом малом их числе (выделенная техническая учётная запись на
    # интеграцию, не массовая аутентификация множества клиентов).
    result = await db.execute(select(BillingApiKey).where(BillingApiKey.status == BillingApiKeyStatus.ACTIVE))
    matched: BillingApiKey | None = None
    for key in result.scalars().all():
        if verify_password(raw_key, key.key_hash):
            matched = key
            break

    if matched is None:
        raise BillingApiError(401, "UNAUTHORIZED", "Отсутствует либо недействителен API-ключ")

    matched.last_used_at = datetime.now(timezone.utc)
    await db.commit()

    _check_rate_limit(matched)
    return matched


def _check_rate_limit(api_key: BillingApiKey) -> None:
    now = time.monotonic()
    window = _REQUEST_LOG.setdefault(api_key.id, deque())
    while window and now - window[0] > _RATE_WINDOW_S:
        window.popleft()
    if len(window) >= api_key.rate_limit_per_minute:
        retry_after = max(1, int(_RATE_WINDOW_S - (now - window[0])) + 1)
        raise BillingApiError(
            429, "RATE_LIMIT_EXCEEDED", "Превышена частота запросов", retry_after=retry_after
        )
    window.append(now)
