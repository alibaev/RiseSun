"""Профили опроса (по просьбе пользователя, 2026-09-08) — именованные
наборы OBIS-кодов с пояснением и признаком включён/выключен, CRUD.
Используются в расписаниях (``ScheduledJob.operation_params.poll_profile_id``)
вместо одного жёстко заданного OBIS — см. ``services.scheduler``."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import require_permission
from ..core.permissions import Permission
from ..db import get_db
from ..models import PollProfile, User
from ..schemas import PollProfileCreate, PollProfileOut, PollProfileUpdate

router = APIRouter(prefix="/api/poll-profiles", tags=["poll-profiles"])


@router.get("", response_model=list[PollProfileOut])
async def list_poll_profiles(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.VIEW_METERS)),
) -> list[PollProfile]:
    result = await db.execute(select(PollProfile).order_by(PollProfile.name))
    return list(result.scalars().all())


@router.get("/{profile_id}", response_model=PollProfileOut)
async def get_poll_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.VIEW_METERS)),
) -> PollProfile:
    profile = await db.get(PollProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Профиль опроса не найден")
    return profile


@router.post("", response_model=PollProfileOut, status_code=status.HTTP_201_CREATED)
async def create_poll_profile(
    body: PollProfileCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_SCHEDULED_JOBS)),
) -> PollProfile:
    profile = PollProfile(
        name=body.name,
        description=body.description,
        items=[item.model_dump() for item in body.items],
        created_by_id=user.id,
    )
    db.add(profile)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Профиль с таким именем уже существует")
    await db.commit()
    await db.refresh(profile)
    return profile


@router.put("/{profile_id}", response_model=PollProfileOut)
async def update_poll_profile(
    profile_id: int,
    body: PollProfileUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_SCHEDULED_JOBS)),
) -> PollProfile:
    profile = await db.get(PollProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Профиль опроса не найден")

    if body.items is not None:
        profile.items = [item.model_dump() for item in body.items]
    if body.description is not None:
        profile.description = body.description

    await db.commit()
    await db.refresh(profile)
    return profile


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_poll_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_SCHEDULED_JOBS)),
) -> None:
    profile = await db.get(PollProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Профиль опроса не найден")
    await db.delete(profile)
    await db.commit()
