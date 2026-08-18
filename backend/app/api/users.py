"""ТЗ п. 4.2.11 — управление пользователями (роль «Администратор»)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import require_permission
from ..core.permissions import Permission
from ..core.security import hash_password
from ..db import get_db
from ..models import User
from ..schemas import UserCreate, UserOut
from ..services.audit import record_audit

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=list[UserOut])
async def list_users(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_USERS)),
) -> list[User]:
    result = await db.execute(select(User).order_by(User.username))
    return list(result.scalars().all())


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_USERS)),
) -> User:
    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Логин уже занят")

    new_user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(new_user)
    await db.flush()
    await record_audit(
        db, user_id=user.id, action="user.create", object_type="user",
        object_id=str(new_user.id), ip_address=request.client.host if request.client else None,
        details={"username": body.username, "role": body.role.value},
    )
    await db.commit()
    await db.refresh(new_user)
    return new_user
