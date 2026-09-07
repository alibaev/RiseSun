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
# фоновые циклы (heartbeat/scheduler/offline_detector/meter_discovery);
# max_overflow — временный запас сверху на пиковые всплески. Без этого
# рост job_worker_concurrency упёрся бы в дефолтный пул SQLAlchemy
# (5+10) раньше, чем в реальный параллелизм (2026-09-07).
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.job_worker_concurrency + 10,
    max_overflow=10,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
