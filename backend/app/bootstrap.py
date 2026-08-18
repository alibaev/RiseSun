"""Разовый бутстрап первого пользователя с ролью «Супер-администратор».

Без этого регистрация Gateway/управление пользователями невозможны —
курица-и-яйцо (обе операции требуют уже существующего Супер-администратора).
Идемпотентно: если хоть один super_admin уже существует, ничего не делает.

Запуск: python -m app.bootstrap
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import select

from .core.security import hash_password
from .db import SessionLocal
from .models import User, UserRole


async def create_initial_superadmin() -> None:
    username = os.environ.get("MMWS_INITIAL_SUPERADMIN_USERNAME")
    password = os.environ.get("MMWS_INITIAL_SUPERADMIN_PASSWORD")
    if not username or not password:
        print(
            "MMWS_INITIAL_SUPERADMIN_USERNAME/PASSWORD не заданы — пропускаю бутстрап.",
            file=sys.stderr,
        )
        return

    async with SessionLocal() as db:
        existing = await db.execute(select(User).where(User.role == UserRole.SUPER_ADMIN))
        if existing.scalars().first() is not None:
            print("Супер-администратор уже существует — пропускаю.")
            return

        db.add(
            User(
                username=username,
                password_hash=hash_password(password),
                role=UserRole.SUPER_ADMIN,
            )
        )
        await db.commit()
        print(f"Создан Супер-администратор: {username}")


if __name__ == "__main__":
    asyncio.run(create_initial_superadmin())
