"""По просьбе пользователя (2026-09-08) — карта OBIS-кодов, только чтение."""

from __future__ import annotations

import pytest

from app.core.security import hash_password
from app.models import User, UserRole
from app.obis_catalog import OBIS_CATALOG


async def _seed_user(db, *, username: str, password: str, role: UserRole) -> User:
    user = User(username=username, password_hash=hash_password(password), role=role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client, username: str, password: str) -> str:
    resp = await client.post("/api/auth/login", data={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.mark.asyncio
async def test_list_obis_catalog_returns_all_entries_numbered_from_000(client, db_session):
    await _seed_user(db_session, username="obs", password="pass1234", role=UserRole.OBSERVER)
    token = await _login(client, "obs", "pass1234")

    resp = await client.get("/api/obis-catalog", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    entries = resp.json()
    assert len(entries) == len(OBIS_CATALOG)
    assert entries[0]["number"] == "000"
    assert all("obis" in e and "label" in e and "description" in e for e in entries)


@pytest.mark.asyncio
async def test_obis_catalog_requires_authentication(client):
    resp = await client.get("/api/obis-catalog")
    assert resp.status_code == 401
