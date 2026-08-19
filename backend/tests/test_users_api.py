"""ТЗ п.4.2.11 — управление пользователями: создание, список,
редактирование роли/блокировка, сброс пароля."""

from __future__ import annotations

import pytest

from app.core.security import hash_password, verify_password
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
async def test_create_and_list_users(client, db_session):
    await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    token = await _login(client, "root", "pass1234")

    create_resp = await client.post(
        "/api/users", json={"username": "newop", "password": "pass12345", "role": "operator"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_resp.status_code == 201, create_resp.text

    dup_resp = await client.post(
        "/api/users", json={"username": "newop", "password": "pass12345", "role": "operator"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert dup_resp.status_code == 409

    list_resp = await client.get("/api/users", headers={"Authorization": f"Bearer {token}"})
    assert len(list_resp.json()) == 2


@pytest.mark.asyncio
async def test_update_role_and_block_user(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    target = await _seed_user(db_session, username="op", password="pass1234", role=UserRole.OPERATOR)
    token = await _login(client, "root", "pass1234")

    resp = await client.put(
        f"/api/users/{target.id}", json={"role": "engineer"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "engineer"

    block_resp = await client.put(
        f"/api/users/{target.id}", json={"is_active": False}, headers={"Authorization": f"Bearer {token}"}
    )
    assert block_resp.status_code == 200
    assert block_resp.json()["is_active"] is False


@pytest.mark.asyncio
async def test_reset_password(client, db_session):
    root = await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    target = await _seed_user(db_session, username="op", password="oldpassword", role=UserRole.OPERATOR)
    token = await _login(client, "root", "pass1234")

    resp = await client.post(
        f"/api/users/{target.id}/reset-password",
        json={"new_password": "brandnewpass"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204

    await db_session.refresh(target)
    assert verify_password("brandnewpass", target.password_hash)
    assert not verify_password("oldpassword", target.password_hash)


@pytest.mark.asyncio
async def test_engineer_cannot_manage_users(client, db_session):
    await _seed_user(db_session, username="eng", password="pass1234", role=UserRole.ENGINEER)
    token = await _login(client, "eng", "pass1234")

    resp = await client.get("/api/users", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_unknown_user_returns_404(client, db_session):
    await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    token = await _login(client, "root", "pass1234")

    resp = await client.put(
        "/api/users/9999", json={"is_active": False}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 404
