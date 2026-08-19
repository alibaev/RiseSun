"""ТЗ п.4.2.9, API.docx п.3.4 — управление API-ключами биллинговой
интеграции (выдача/ротация/отзыв), доступно роли «Администратор»/
«Супер-администратор» (Permission.MANAGE_BILLING_KEYS)."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import require_permission
from ..core.permissions import Permission
from ..core.security import hash_password
from ..db import get_db
from ..models import BillingApiKey, BillingApiKeyStatus, User
from ..schemas import BillingApiKeyCreate, BillingApiKeyCreated, BillingApiKeyOut
from ..services.audit import record_audit

router = APIRouter(prefix="/api/billing-keys", tags=["billing-keys"])


@router.get("", response_model=list[BillingApiKeyOut])
async def list_billing_keys(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_BILLING_KEYS)),
) -> list[BillingApiKey]:
    result = await db.execute(select(BillingApiKey).order_by(BillingApiKey.created_at.desc()))
    return list(result.scalars().all())


@router.post("", response_model=BillingApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_billing_key(
    body: BillingApiKeyCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_BILLING_KEYS)),
) -> BillingApiKeyCreated:
    """Секрет генерируется на сервере (не выбирается администратором) и
    отдаётся в ответе ОДИН РАЗ — далее хранится только хешированным
    (API.docx п.3.1)."""
    raw_key = secrets.token_urlsafe(32)
    key = BillingApiKey(
        client_id=body.client_id,
        key_hash=hash_password(raw_key),
        description=body.description,
        rate_limit_per_minute=body.rate_limit_per_minute,
        created_by_id=user.id,
    )
    db.add(key)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="client_id уже используется")

    await record_audit(
        db, user_id=user.id, action="billing_key.create", object_type="billing_api_key",
        object_id=str(key.id), source="web",
        ip_address=request.client.host if request.client else None,
        details={"client_id": body.client_id},
    )
    await db.commit()
    await db.refresh(key)
    return BillingApiKeyCreated(**BillingApiKeyOut.model_validate(key).model_dump(), api_key=raw_key)


@router.post("/{key_id}/revoke", response_model=BillingApiKeyOut)
async def revoke_billing_key(
    key_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.MANAGE_BILLING_KEYS)),
) -> BillingApiKey:
    """Отзыв (API.docx п.3.4 — «при компрометации ключа он подлежит
    немедленному отзыву... факт... фиксируется в журнале аудита»). Не
    удаляет запись — история пакетов ссылается на ключ по FK."""
    key = await db.get(BillingApiKey, key_id)
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ключ не найден")
    if key.status == BillingApiKeyStatus.REVOKED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ключ уже отозван")

    key.status = BillingApiKeyStatus.REVOKED
    key.revoked_at = datetime.now(timezone.utc)
    await record_audit(
        db, user_id=user.id, action="billing_key.revoke", object_type="billing_api_key",
        object_id=str(key.id), source="web",
        ip_address=request.client.host if request.client else None,
        details={"client_id": key.client_id},
    )
    await db.commit()
    await db.refresh(key)
    return key
