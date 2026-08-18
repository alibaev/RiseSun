"""FastAPI-зависимости: текущий пользователь из JWT, проверка прав по роли."""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.permissions import Permission, role_has_permission
from ..core.security import decode_token
from ..db import get_db
from ..models import User

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


class InvalidCredentials(Exception):
    """Токен недействителен/истёк, либо пользователь неактивен/удалён.

    Отдельное от HTTPException исключение — переиспользуется и HTTP-, и
    WebSocket-обработчиками (у них разные способы сообщить об отказе).
    """


async def load_user_from_token(token: str, db: AsyncSession) -> User:
    try:
        payload = decode_token(token)
    except JWTError:
        raise InvalidCredentials
    if payload.get("type") != "access":
        raise InvalidCredentials
    username = payload.get("sub")
    if not username:
        raise InvalidCredentials
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise InvalidCredentials
    return user


async def get_current_user(
    token: str = Depends(_oauth2_scheme), db: AsyncSession = Depends(get_db)
) -> User:
    try:
        return await load_user_from_token(token, db)
    except InvalidCredentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Недействительный или истёкший токен",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_permission(permission: Permission):
    async def checker(user: User = Depends(get_current_user)) -> User:
        if not role_has_permission(user.role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Роль {user.role.value} не имеет права {permission.value}",
            )
        return user

    return checker
