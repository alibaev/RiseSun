"""Async SQLAlchemy engine/сессии. Backend API — stateless (ТЗ п. 4.6),
все состояние живёт в PostgreSQL, что допускает горизонтальное
масштабирование Backend'а без sticky-сессий.
"""

from __future__ import annotations

from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

# pool_size — под settings.job_worker_concurrency параллельных
# воркеров (каждый держит сессию на время всей операции чтения, до
# ~150-220с на call-home) плюс запас на обычные HTTP-запросы API и
# фоновые циклы (heartbeat/scheduler/offline_detector/meter_discovery).
#
# 2026-09-11 — найдено на живом трафике при масштабировании парка
# 381 -> 1700+ счётчиков: старый расчёт (job_worker_concurrency + 10,
# т.е. 18+10=28) не учитывал СОВЕРШЕННО ОТДЕЛЬНЫЙ источник спроса на
# соединения — событийное чтение (immediate-read, 2026-09-09). Каждое
# опознанное call-home соединение на Gateway'е дёргает
# POST /claim-jobs, и КАЖДЫЙ такой вызов на Backend'е открывает свою
# короткую сессию — при всплеске дозвонов (после рестарта, при заметном
# росте парка) таких вызовов легко одновременно СОТНИ, независимо от
# job_worker_concurrency. Пул исчерпывался целиком
# (`sqlalchemy.exc.TimeoutError: QueuePool limit of size 18 overflow 10
# reached`), claim-jobs массово падал по таймауту на Gateway'е (3с,
# backend_client.DEFAULT_CLAIM_TIMEOUT_S) — immediate-read сдавался
# (fail-closed по дизайну) и такие счётчики проваливались в старый,
# гораздо менее надёжный FIFO-путь, отсюда и резкий рост отказов
# read_current, не связанный с конкретной партией счётчиков как таковой.
# Новые числа — с запасом под Postgres max_connections=100 (сейчас
# 100 - тут не трогаем, есть кому: проверено, ~34 соединения заняты при
# старом пуле 28, есть запас).
# max_overflow — временный запас сверху на пиковые всплески.
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.job_worker_concurrency + 40,
    max_overflow=30,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
