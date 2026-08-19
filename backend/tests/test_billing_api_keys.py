"""ТЗ п.4.2.9, API.docx п.3.4 — управление API-ключами биллинга."""

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
async def test_create_key_returns_secret_once_then_hidden(client, db_session):
    await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    token = await _login(client, "root", "pass1234")

    create_resp = await client.post(
        "/api/billing-keys",
        json={"client_id": "1c-billing", "description": "1С биллинг"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_resp.status_code == 201, create_resp.text
    body = create_resp.json()
    assert "api_key" in body and len(body["api_key"]) > 20
    assert body["status"] == "active"

    list_resp = await client.get("/api/billing-keys", headers={"Authorization": f"Bearer {token}"})
    listed = list_resp.json()
    assert len(listed) == 1
    assert "api_key" not in listed[0]


@pytest.mark.asyncio
async def test_duplicate_client_id_rejected(client, db_session):
    await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    token = await _login(client, "root", "pass1234")

    await client.post(
        "/api/billing-keys", json={"client_id": "dup"}, headers={"Authorization": f"Bearer {token}"}
    )
    dup_resp = await client.post(
        "/api/billing-keys", json={"client_id": "dup"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert dup_resp.status_code == 409


@pytest.mark.asyncio
async def test_revoke_key(client, db_session):
    await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    token = await _login(client, "root", "pass1234")

    create_resp = await client.post(
        "/api/billing-keys", json={"client_id": "revoke-me"}, headers={"Authorization": f"Bearer {token}"}
    )
    key_id = create_resp.json()["id"]

    revoke_resp = await client.post(
        f"/api/billing-keys/{key_id}/revoke", headers={"Authorization": f"Bearer {token}"}
    )
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["status"] == "revoked"

    second_revoke = await client.post(
        f"/api/billing-keys/{key_id}/revoke", headers={"Authorization": f"Bearer {token}"}
    )
    assert second_revoke.status_code == 409


@pytest.mark.asyncio
async def test_engineer_cannot_manage_billing_keys(client, db_session):
    await _seed_user(db_session, username="eng", password="pass1234", role=UserRole.ENGINEER)
    token = await _login(client, "eng", "pass1234")

    resp = await client.post(
        "/api/billing-keys", json={"client_id": "x"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403
