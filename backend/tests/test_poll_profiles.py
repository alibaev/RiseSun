"""По просьбе пользователя (2026-09-08) — профили опроса: CRUD набора
OBIS-кодов с пояснением и признаком включён/выключен."""

from __future__ import annotations

import pytest

from app.core.security import hash_password
from app.models import User, UserRole


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
async def test_create_get_update_delete_profile(client, db_session):
    await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    token = await _login(client, "root", "pass1234")

    create_resp = await client.post(
        "/api/poll-profiles",
        json={
            "name": "Профиль1",
            "description": "тест",
            "items": [
                {"obis": "1.1.1.8.0.ff", "label": "Активная энергия, всего", "enabled": True},
                {"obis": "1.1.32.7.0.ff", "label": "Напряжение фаза A", "enabled": False},
            ],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_resp.status_code == 201, create_resp.text
    profile_id = create_resp.json()["id"]

    dup_resp = await client.post(
        "/api/poll-profiles",
        json={"name": "Профиль1", "items": [{"obis": "1.1.1.8.0.ff", "label": "x"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert dup_resp.status_code == 409

    get_resp = await client.get(f"/api/poll-profiles/{profile_id}", headers={"Authorization": f"Bearer {token}"})
    assert get_resp.status_code == 200
    assert len(get_resp.json()["items"]) == 2
    assert get_resp.json()["items"][1]["enabled"] is False

    list_resp = await client.get("/api/poll-profiles", headers={"Authorization": f"Bearer {token}"})
    assert len(list_resp.json()) == 1

    update_resp = await client.put(
        f"/api/poll-profiles/{profile_id}",
        json={"items": [{"obis": "1.1.0.6.3.ff", "label": "Токовый класс", "enabled": True}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert update_resp.status_code == 200
    assert len(update_resp.json()["items"]) == 1

    delete_resp = await client.delete(f"/api/poll-profiles/{profile_id}", headers={"Authorization": f"Bearer {token}"})
    assert delete_resp.status_code == 204

    get_after_delete = await client.get(f"/api/poll-profiles/{profile_id}", headers={"Authorization": f"Bearer {token}"})
    assert get_after_delete.status_code == 404


@pytest.mark.asyncio
async def test_create_profile_rejects_malformed_obis(client, db_session):
    await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    token = await _login(client, "root", "pass1234")

    resp = await client.post(
        "/api/poll-profiles",
        json={"name": "Битый", "items": [{"obis": "not-an-obis", "label": "x"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_observer_can_view_but_not_create_profile(client, db_session):
    await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    await _seed_user(db_session, username="obs", password="pass1234", role=UserRole.OBSERVER)
    obs_token = await _login(client, "obs", "pass1234")

    list_resp = await client.get("/api/poll-profiles", headers={"Authorization": f"Bearer {obs_token}"})
    assert list_resp.status_code == 200

    create_resp = await client.post(
        "/api/poll-profiles",
        json={"name": "x", "items": [{"obis": "1.1.1.8.0.ff", "label": "x"}]},
        headers={"Authorization": f"Bearer {obs_token}"},
    )
    assert create_resp.status_code == 403
