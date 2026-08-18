"""Pydantic-схемы запросов/ответов REST API (ТЗ Приложение А)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .models import GatewayStatus, JobStatus, ProtocolProfile, UserRole


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: UserRole
    is_active: bool
    created_at: datetime


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8)
    role: UserRole


class GatewayCreate(BaseModel):
    name: str
    manufacturer: str = "Risesun"
    driver_version: str | None = None
    grpc_target: str
    supported_operations: dict = Field(default_factory=dict)


class GatewayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    manufacturer: str
    driver_version: str | None
    grpc_target: str
    supported_operations: dict
    status: GatewayStatus
    last_heartbeat_at: datetime | None
    is_online: bool
    created_at: datetime


class MeterCreate(BaseModel):
    serial_number: str = Field(min_length=1, max_length=32)
    ip_address: str
    port: int = Field(gt=0, le=65535)
    protocol_profile: ProtocolProfile
    password: str = Field(description="Пароль доступа (LLS) — будет зашифрован перед сохранением")
    aes_key_hex: str | None = Field(default=None, description="AES-ключ канала, hex-строка")
    location: str | None = None
    model: str | None = None
    gateway_id: int


class MeterUpdate(BaseModel):
    ip_address: str | None = None
    port: int | None = Field(default=None, gt=0, le=65535)
    password: str | None = None
    aes_key_hex: str | None = None
    location: str | None = None
    model: str | None = None
    is_active: bool | None = None


class MeterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    serial_number: str
    ip_address: str
    port: int
    protocol_profile: ProtocolProfile
    location: str | None
    model: str | None
    is_active: bool
    is_online: bool
    gateway_id: int
    last_seen_at: datetime | None
    last_read_at: datetime | None
    created_at: datetime


class MeterReadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    meter_id: int
    obis_code: str
    value_json: object
    unit: str | None
    read_at: datetime


class ReadTriggerRequest(BaseModel):
    obis: str = Field(description="6-байтная hex-нотация A.B.C.D.E.F")


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_type: str
    meter_id: int
    status: JobStatus
    result: dict | None
    error: dict | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
