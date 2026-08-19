"""ТЗ п.4.2.5 — именованные схемы параметров: сохранение, просмотр,
редактирование, удаление, применение к одному или нескольким счётчикам
за одну операцию."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import require_permission
from ..core.permissions import Permission
from ..db import get_db
from ..models import Job, Meter, ParameterScheme, User
from ..schemas import (
    ApplySchemeRequest,
    JobOut,
    ParameterSchemeCreate,
    ParameterSchemeOut,
    ParameterSchemeUpdate,
)
from ..services.write_parameters import WRITABLE_INT_PARAMETERS

router = APIRouter(prefix="/api/parameter-schemes", tags=["parameter-schemes"])


def _validate_parameters(parameters: list) -> None:
    unknown = [p.parameter for p in parameters if p.parameter not in WRITABLE_INT_PARAMETERS]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Неизвестные параметры: {unknown}. Доступные: {sorted(WRITABLE_INT_PARAMETERS)}",
        )


@router.get("", response_model=list[ParameterSchemeOut])
async def list_schemes(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.VIEW_METERS)),
) -> list[ParameterScheme]:
    result = await db.execute(select(ParameterScheme).order_by(ParameterScheme.name))
    return list(result.scalars().all())


@router.get("/{scheme_id}", response_model=ParameterSchemeOut)
async def get_scheme(
    scheme_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.VIEW_METERS)),
) -> ParameterScheme:
    scheme = await db.get(ParameterScheme, scheme_id)
    if scheme is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Схема не найдена")
    return scheme


@router.post("", response_model=ParameterSchemeOut, status_code=status.HTTP_201_CREATED)
async def create_scheme(
    body: ParameterSchemeCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_PARAMETER_SCHEMES)),
) -> ParameterScheme:
    _validate_parameters(body.parameters)
    scheme = ParameterScheme(
        name=body.name,
        description=body.description,
        parameters=[p.model_dump() for p in body.parameters],
        created_by_id=user.id,
    )
    db.add(scheme)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Схема с таким именем уже существует")
    await db.commit()
    await db.refresh(scheme)
    return scheme


@router.put("/{scheme_id}", response_model=ParameterSchemeOut)
async def update_scheme(
    scheme_id: int,
    body: ParameterSchemeUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_PARAMETER_SCHEMES)),
) -> ParameterScheme:
    scheme = await db.get(ParameterScheme, scheme_id)
    if scheme is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Схема не найдена")

    if body.parameters is not None:
        _validate_parameters(body.parameters)
        scheme.parameters = [p.model_dump() for p in body.parameters]
    if body.description is not None:
        scheme.description = body.description

    await db.commit()
    await db.refresh(scheme)
    return scheme


@router.delete("/{scheme_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scheme(
    scheme_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_PARAMETER_SCHEMES)),
) -> None:
    scheme = await db.get(ParameterScheme, scheme_id)
    if scheme is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Схема не найдена")
    await db.delete(scheme)
    await db.commit()


@router.post("/{scheme_id}/apply", response_model=list[JobOut], status_code=status.HTTP_202_ACCEPTED)
async def apply_scheme(
    scheme_id: int,
    body: ApplySchemeRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.WRITE_PARAMETER)),
) -> list[Job]:
    """Массовое применение (ТЗ п.4.2.5 — расширение по сравнению с
    исходным приложением, где схема применялась к одному счётчику за
    раз): по одной задаче write_parameter на каждую пару
    параметр×счётчик, каждая наблюдаема и аудируема независимо (тот же
    механизм, что и у одиночной записи параметра)."""
    scheme = await db.get(ParameterScheme, scheme_id)
    if scheme is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Схема не найдена")

    meters = (await db.execute(select(Meter).where(Meter.id.in_(body.meter_ids)))).scalars().all()
    found_ids = {m.id for m in meters}
    missing_ids = [mid for mid in body.meter_ids if mid not in found_ids]
    if missing_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Счётчики не найдены: {missing_ids}"
        )

    jobs = [
        Job(
            job_type="write_parameter",
            meter_id=meter_id,
            payload={
                "parameter": entry["parameter"],
                "value": entry["value"],
                "scheme_id": scheme.id,
                "scheme_name": scheme.name,
            },
            created_by_id=user.id,
        )
        for meter_id in body.meter_ids
        for entry in scheme.parameters
    ]
    db.add_all(jobs)
    await db.commit()
    for job in jobs:
        await db.refresh(job)
    return jobs
