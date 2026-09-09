"""Аутентификация внутреннего HTTP-канала Gateway -> Backend
(событийное чтение call-home счётчиков сразу при подключении, см.
DECISIONS.md и /root/.claude/plans/ticklish-popping-bear.md) — НЕ
связана с JWT-сессиями пользователей веб-интерфейса (app/auth/deps.py)
и НЕ связана с биллинговыми API-ключами (app/auth/billing_deps.py).
Общий статичный секрет вместо БД-хранимого ключа — этот канал
исключительно container-to-container внутри docker-сети, не внешняя
интеграция, ротация/отзыв конкретного клиента не нужны.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from ..config import settings


async def require_gateway_internal_secret(
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> None:
    # secrets.compare_digest — постоянное время сравнения (защита от
    # timing-атак по подбору секрета), в отличие от обычного ==.
    if not x_internal_secret or not secrets.compare_digest(x_internal_secret, settings.gateway_internal_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Недействительный внутренний секрет")
