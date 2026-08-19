"""Точка входа Backend API (AMI/HES Core, ТЗ п. 4.1)."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import (
    audit,
    auth,
    billing,
    billing_api_keys,
    dashboard,
    gateways,
    jobs,
    meters,
    notifications,
    parameter_schemes,
    scheduled_jobs,
    users,
)
from .config import settings
from .services.billing_errors import BillingApiError
from .services.heartbeat import heartbeat_loop
from .services.job_worker import worker_loop
from .services.meter_discovery import meter_discovery_loop
from .services.offline_detector import offline_detector_loop
from .services.scheduler import scheduler_loop

_BILLING_PATH_PREFIX = "/api/v1/billing/"


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(worker_loop(stop_event))
    heartbeat_task = asyncio.create_task(heartbeat_loop(stop_event))
    scheduler_task = asyncio.create_task(scheduler_loop(stop_event))
    offline_detector_task = asyncio.create_task(offline_detector_loop(stop_event))
    meter_discovery_task = asyncio.create_task(meter_discovery_loop(stop_event))
    try:
        yield
    finally:
        stop_event.set()
        await worker_task
        await heartbeat_task
        await scheduler_task
        await offline_detector_task
        await meter_discovery_task


app = FastAPI(title="MMWS Backend API", version="0.1.0-etap1", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def billing_request_id_middleware(request: Request, call_next):
    """API.docx раздел 5: ``request_id`` присутствует в КАЖДОМ ответе
    сегмента /api/v1/billing/* (в т.ч. успешном) — как заголовок
    ``X-Request-Id``; в теле ответа (``error.request_id``) — только для
    ошибок, см. ``billing_api_error_handler`` ниже."""
    if not request.url.path.startswith(_BILLING_PATH_PREFIX):
        return await call_next(request)
    request_id = f"r-{uuid.uuid4().hex[:12]}"
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


@app.exception_handler(BillingApiError)
async def billing_api_error_handler(request: Request, exc: BillingApiError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or f"r-{uuid.uuid4().hex[:12]}"
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else {}
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "request_id": request_id}},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def billing_validation_error_handler(request: Request, exc: RequestValidationError):
    """API.docx раздел 5: «Все ошибки возвращаются в едином формате,
    независимо от метода» — включая ошибки валидации параметров запроса
    (тип/формат), которые FastAPI иначе вернул бы в собственном формате
    ``{"detail": [...]}`` со статусом 422 раньше, чем управление дойдёт
    до тела обработчика роута. Вне /api/v1/billing/* поведение не
    меняется — используется штатный обработчик FastAPI."""
    if not request.url.path.startswith(_BILLING_PATH_PREFIX):
        return await request_validation_exception_handler(request, exc)
    request_id = getattr(request.state, "request_id", None) or f"r-{uuid.uuid4().hex[:12]}"
    return JSONResponse(
        status_code=400,
        content={"error": {"code": "VALIDATION_ERROR", "message": str(exc), "request_id": request_id}},
    )


app.include_router(auth.router)
app.include_router(gateways.router)
app.include_router(meters.router)
app.include_router(jobs.router)
app.include_router(users.router)
app.include_router(audit.router)
app.include_router(parameter_schemes.router)
app.include_router(scheduled_jobs.router)
app.include_router(notifications.router)
app.include_router(billing.router)
app.include_router(billing_api_keys.router)
app.include_router(dashboard.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
