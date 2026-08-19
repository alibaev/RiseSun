"""ТЗ п.4.2.8 — REST API уведомлений (список, отметка прочитанным)."""

from __future__ import annotations

import pytest

from app.core.security import hash_password
from app.models import Notification, NotificationCategory, User, UserRole


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
async def test_list_and_filter_unread(client, db_session):
    await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    token = await _login(client, "root", "pass1234")

    db_session.add(Notification(category=NotificationCategory.METER_OFFLINE, message="a", is_read=True))
    db_session.add(Notification(category=NotificationCategory.METER_OFFLINE, message="b", is_read=False))
    await db_session.commit()

    all_resp = await client.get("/api/notifications", headers={"Authorization": f"Bearer {token}"})
    assert len(all_resp.json()) == 2

    unread_resp = await client.get("/api/notifications?unread_only=true", headers={"Authorization": f"Bearer {token}"})
    assert len(unread_resp.json()) == 1
    assert unread_resp.json()[0]["message"] == "b"


@pytest.mark.asyncio
async def test_mark_read(client, db_session):
    await _seed_user(db_session, username="root", password="pass1234", role=UserRole.SUPER_ADMIN)
    token = await _login(client, "root", "pass1234")

    notification = Notification(category=NotificationCategory.SCHEDULED_JOB_FAILED, message="x", is_read=False)
    db_session.add(notification)
    await db_session.commit()
    await db_session.refresh(notification)

    resp = await client.post(
        f"/api/notifications/{notification.id}/read", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["is_read"] is True

    missing_resp = await client.post("/api/notifications/9999/read", headers={"Authorization": f"Bearer {token}"})
    assert missing_resp.status_code == 404


@pytest.mark.asyncio
async def test_observer_can_view_notifications(client, db_session):
    await _seed_user(db_session, username="obs", password="pass1234", role=UserRole.OBSERVER)
    token = await _login(client, "obs", "pass1234")

    resp = await client.get("/api/notifications", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
