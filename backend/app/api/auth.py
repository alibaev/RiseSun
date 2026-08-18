"""ТЗ п. 4.2.1 — вход по логину/паролю, JWT access+refresh."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import create_access_token, create_refresh_token, decode_token, verify_password
from ..db import get_db
from ..models import User
from ..schemas import RefreshRequest, TokenResponse
from ..services.audit import record_audit

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    result = await db.execute(select(User).where(User.username == form_data.username))
    user = result.scalar_one_or_none()
    ip = request.client.host if request.client else None

    if user is None or not user.is_active or not verify_password(form_data.password, user.password_hash):
        await record_audit(
            db, user_id=None, action="login", result="failure",
            ip_address=ip, details={"username": form_data.username},
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный логин или пароль"
        )

    await record_audit(db, user_id=user.id, action="login", result="success", ip_address=ip)
    await db.commit()

    return TokenResponse(
        access_token=create_access_token(subject=user.username, role=user.role.value),
        refresh_token=create_refresh_token(subject=user.username),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    try:
        payload = decode_token(body.refresh_token)
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Недействительный refresh-токен")
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Ожидался refresh-токен")

    result = await db.execute(select(User).where(User.username == payload.get("sub")))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Пользователь недоступен")

    return TokenResponse(
        access_token=create_access_token(subject=user.username, role=user.role.value),
        refresh_token=create_refresh_token(subject=user.username),
    )
