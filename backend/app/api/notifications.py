"""ТЗ п.4.2.8 — уведомления: список, отметка прочитанным, потоковая
доставка новых уведомлений по WebSocket в реальном времени."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import InvalidCredentials, load_user_from_token, require_permission
from ..core.permissions import Permission
from ..db import SessionLocal, get_db
from ..models import Notification, User
from ..schemas import NotificationOut

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

_POLL_INTERVAL_S = 2.0


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    unread_only: bool = False,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.VIEW_METERS)),
) -> list[Notification]:
    query = select(Notification)
    if unread_only:
        query = query.where(Notification.is_read.is_(False))
    result = await db.execute(query.order_by(Notification.created_at.desc()).limit(limit))
    return list(result.scalars().all())


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_notification_read(
    notification_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.VIEW_METERS)),
) -> Notification:
    """Уведомления общесистемные (см. models.Notification) — отметка
    прочитанным доступна любому пользователю с правом просмотра, не
    только тому, кто его создал/увидел первым."""
    notification = await db.get(Notification, notification_id)
    if notification is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Уведомление не найдено")
    notification.is_read = True
    await db.commit()
    await db.refresh(notification)
    return notification


@router.websocket("/stream")
async def stream_notifications(websocket: WebSocket) -> None:
    """Токен передаётся query-параметром ``?token=`` — тот же паттерн, что
    и у ``/api/jobs/{id}/stream`` (WebSocket не поддерживает заголовок
    Authorization при установлении соединения из браузера). Отдаёт
    только НОВЫЕ уведомления (созданные после открытия соединения) —
    начальный список уже прочитанных/непрочитанных загружается через
    GET /api/notifications при открытии страницы, повторно его через WS
    не дублируем (тот же принцип, что и у потока статуса задачи)."""
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4401)
        return
    try:
        async with SessionLocal() as db:
            await load_user_from_token(token, db)
    except InvalidCredentials:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    async with SessionLocal() as db:
        newest = (
            await db.execute(select(Notification.id).order_by(Notification.id.desc()).limit(1))
        ).scalar_one_or_none()
    last_seen_id = newest or 0

    try:
        while True:
            async with SessionLocal() as db:
                result = await db.execute(
                    select(Notification).where(Notification.id > last_seen_id).order_by(Notification.id)
                )
                new_notifications = list(result.scalars().all())
            for notification in new_notifications:
                await websocket.send_json(NotificationOut.model_validate(notification).model_dump(mode="json"))
                last_seen_id = notification.id
            await asyncio.sleep(_POLL_INTERVAL_S)
    except WebSocketDisconnect:
        pass
