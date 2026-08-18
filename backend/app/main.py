"""Точка входа Backend API (AMI/HES Core, ТЗ п. 4.1)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import audit, auth, gateways, jobs, meters, users
from .config import settings
from .services.heartbeat import heartbeat_loop
from .services.job_worker import worker_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(worker_loop(stop_event))
    heartbeat_task = asyncio.create_task(heartbeat_loop(stop_event))
    try:
        yield
    finally:
        stop_event.set()
        await worker_task
        await heartbeat_task


app = FastAPI(title="MMWS Backend API", version="0.1.0-etap1", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(gateways.router)
app.include_router(meters.router)
app.include_router(jobs.router)
app.include_router(users.router)
app.include_router(audit.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
