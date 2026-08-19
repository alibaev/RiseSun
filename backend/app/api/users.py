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
from ..schemas import UserCreate, UserOut, UserResetPassword, UserUpdate
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


@router.put("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    body: UserUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_USERS)),
) -> User:
    """ТЗ п.4.2.11 — «редактирование роли, ... блокировка учётной записи»."""
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(target, field, value)

    await record_audit(
        db, user_id=user.id, action="user.update", object_type="user",
        object_id=str(target.id), ip_address=request.client.host if request.client else None,
        details={"changed_fields": {k: (v.value if hasattr(v, "value") else v) for k, v in changes.items()}},
    )
    await db.commit()
    await db.refresh(target)
    return target


@router.post("/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_user_password(
    user_id: int,
    body: UserResetPassword,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_USERS)),
) -> None:
    """ТЗ п.4.2.11 — «сброс пароля». Администратор задаёт новый пароль
    напрямую (без почтового потока сброса — вне минимального состава
    экранов ТЗ); действие безусловно фиксируется в аудите, сам новый
    пароль в details не попадает (Promt_MMWS.md, раздел 3, принцип 4)."""
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    target.password_hash = hash_password(body.new_password)
    await record_audit(
        db, user_id=user.id, action="user.reset_password", object_type="user",
        object_id=str(target.id), ip_address=request.client.host if request.client else None,
    )
    await db.commit()
