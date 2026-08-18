"""Тестовая БД — отдельная база ``mmws_test`` (не dev-база), пересоздаётся
схемой на каждый тестовый прогон."""

from __future__ import annotations

import os

os.environ["MMWS_DATABASE_URL"] = "postgresql+asyncpg://mmws:mmws@localhost:5432/mmws_test"

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base, SessionLocal, engine
from app.main import app


@pytest_asyncio.fixture(autouse=True)
async def _reset_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
