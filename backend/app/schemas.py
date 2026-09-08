"""Pydantic-схемы запросов/ответов REST API (ТЗ Приложение А)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import (
    BillingApiKeyStatus,
    DisconnectBatchItemStatus,
    DisconnectBatchStatus,
    GatewayStatus,
    JobStatus,
    MeterStatus,
    NotificationCategory,
    ParameterWriteResult,
    ProtocolProfile,
    ScheduledJobRunStatus,
    UserRole,
)


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


class UserUpdate(BaseModel):
    role: UserRole | None = None
    is_active: bool | None = None


class UserResetPassword(BaseModel):
    new_password: str = Field(min_length=8)


class GatewayCreate(BaseModel):
    name: str
    manufacturer: str = "Risesun"
    driver_version: str | None = None
    grpc_target: str
    supported_operations: dict = Field(default_factory=dict)


class GatewayCallHomePortRequest(BaseModel):
    port: int = Field(ge=1, le=65535)


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
    call_home_port: int | None
    is_online: bool
    created_at: datetime


class MeterCreate(BaseModel):
    serial_number: str = Field(min_length=1, max_length=32)
    # ip_address/port обязательны только для обычной модели (Gateway —
    # инициатор). Для call-home счётчиков (звонят сами — подтверждённое
    # расхождение с ТЗ Table 1, см. ../DECISIONS.md) Gateway опознаёт
    # звонящий счётчик по serial_number через свой call-home пул, адрес
    # назначения ему не нужен.
    ip_address: str | None = None
    port: int | None = Field(default=None, gt=0, le=65535)
    is_call_home: bool = False
    protocol_profile: ProtocolProfile
    password: str = Field(description="Пароль доступа (LLS) — будет зашифрован перед сохранением")
    aes_key_hex: str | None = Field(default=None, description="AES-ключ канала, hex-строка")
    location: str | None = None
    model: str | None = None
    gateway_id: int

    @model_validator(mode="after")
    def _require_address_unless_call_home(self) -> "MeterCreate":
        if not self.is_call_home and (self.ip_address is None or self.port is None):
            raise ValueError(
                "ip_address и port обязательны для счётчиков без is_call_home "
                "(Gateway сам инициирует подключение — ТЗ Table 1)"
            )
        return self


class MeterUpdate(BaseModel):
    ip_address: str | None = None
    port: int | None = Field(default=None, gt=0, le=65535)
    is_call_home: bool | None = None
    password: str | None = None
    aes_key_hex: str | None = None
    location: str | None = None
    model: str | None = None
    is_active: bool | None = None


class MeterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    serial_number: str
    physical_address: str | None
    ip_address: str | None
    port: int | None
    is_call_home: bool
    # NULL для status=MeterStatus.INSTALLED — счётчик обнаружен
    # автоматически по call-home, протокол ещё не указан администратором.
    protocol_profile: ProtocolProfile | None
    location: str | None
    model: str | None
    is_active: bool
    status: MeterStatus
    is_online: bool
    gateway_id: int
    last_seen_at: datetime | None
    last_read_at: datetime | None
    rated_current_amps: float | None
    created_at: datetime
    # Значение последнего показания (meter_readings.value_json на момент
    # last_read_at) — подмешивается отдельным запросом в list_meters, не
    # ORM-связь (см. app/api/meters.py); нужно списку счётчиков, чтобы не
    # заставлять фронтенд делать по отдельному запросу на каждый счётчик.
    last_reading_value: object | None = None


class ActivateMeterRequest(BaseModel):
    """Этап 6 — перевод счётчика, обнаруженного по call-home
    (status=INSTALLED), в рабочее состояние (status=ACTIVE). Пароль и
    протокольный профиль обязательны — они принципиально не могут быть
    узнаны из самого факта звонка домой (DL/T645-анонс несёт только
    адрес)."""

    password: str = Field(description="Пароль доступа (LLS) — будет зашифрован перед сохранением")
    protocol_profile: ProtocolProfile
    ip_address: str | None = None
    port: int | None = Field(default=None, gt=0, le=65535)
    location: str | None = None
    model: str | None = None


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


class WriteParameterRequest(BaseModel):
    value: int = Field(ge=0, le=255, description="Новое значение параметра (1 байт, 0-255)")


class ReadLoadProfileTriggerRequest(BaseModel):
    from_iso: str = Field(description="Начало диапазона, ISO 8601, напр. \"2026-08-01T00:00:00\"")
    to_iso: str = Field(description="Конец диапазона, ISO 8601")
    obis: str | None = Field(
        default=None,
        description=(
            "Override адреса буфера профиля нагрузки (6-байтная hex-нотация). "
            "По умолчанию — рабочая гипотеза DEFAULT_LOAD_PROFILE_OBIS "
            "(см. app/services/load_profile.py, DECISIONS.md)."
        ),
    )


class LoadProfileRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    meter_id: int
    obis_code: str
    timestamp: datetime
    values_json: list


class ParameterWriteHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None
    meter_id: int
    parameter: str
    obis_code: str
    old_value: object | None
    new_value: object | None
    result: ParameterWriteResult
    error_message: str | None
    created_at: datetime


class ParameterSchemeParam(BaseModel):
    parameter: str
    value: int = Field(ge=0, le=255, description="1 байт (0-255) — та же область значений, что у WriteParameterRequest")


class ParameterSchemeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    parameters: list[ParameterSchemeParam] = Field(min_length=1)


class ParameterSchemeUpdate(BaseModel):
    description: str | None = None
    parameters: list[ParameterSchemeParam] | None = None


class ParameterSchemeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    parameters: list[dict]
    created_at: datetime
    updated_at: datetime | None


class ApplySchemeRequest(BaseModel):
    meter_ids: list[int] = Field(min_length=1)


_OBIS_PATTERN = r"^[0-9a-fA-F]{1,2}(\.[0-9a-fA-F]{1,2}){5}$"


class PollProfileItem(BaseModel):
    obis: str = Field(min_length=1, max_length=32)
    label: str = Field(min_length=1, max_length=255, description="Пояснение — что именно опрашивает этот OBIS")
    enabled: bool = True

    @field_validator("obis")
    @classmethod
    def _validate_obis(cls, value: str) -> str:
        if not re.match(_OBIS_PATTERN, value):
            raise ValueError(f"OBIS-код должен быть вида A.B.C.D.E.F (hex-поля), получено: {value!r}")
        return value


class PollProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    items: list[PollProfileItem] = Field(min_length=1)


class PollProfileUpdate(BaseModel):
    description: str | None = None
    items: list[PollProfileItem] | None = None


class PollProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    items: list[dict]
    created_at: datetime
    updated_at: datetime | None


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


ScheduledJobType = Literal["read_current", "read_load_profile", "read_rated_current"]


class ScheduledJobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    cron_expression: str
    job_type: ScheduledJobType
    # read_current -> {"obis": "..."} (по умолчанию — активная энергия,
    # приём, всего); read_load_profile -> {"window_hours": N, "obis": "..."}
    # — каждый запуск запрашивает последние N часов от текущего момента;
    # read_rated_current -> {} (OBIS фиксирован, см. job_worker.py).
    operation_params: dict = Field(default_factory=dict)
    meter_ids: list[int] = Field(min_length=1)
    is_enabled: bool = True

    @field_validator("cron_expression")
    @classmethod
    def _validate_cron(cls, value: str) -> str:
        if not croniter.is_valid(value):
            raise ValueError(f"Некорректное cron-выражение: {value!r}")
        return value


class ScheduledJobUpdate(BaseModel):
    name: str | None = None
    cron_expression: str | None = None
    operation_params: dict | None = None
    meter_ids: list[int] | None = Field(default=None, min_length=1)
    is_enabled: bool | None = None

    @field_validator("cron_expression")
    @classmethod
    def _validate_cron(cls, value: str | None) -> str | None:
        if value is not None and not croniter.is_valid(value):
            raise ValueError(f"Некорректное cron-выражение: {value!r}")
        return value


class ScheduledJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    cron_expression: str
    job_type: str
    operation_params: dict
    meter_ids: list[int]
    is_enabled: bool
    created_at: datetime
    last_run_at: datetime | None
    next_run_at: datetime | None = None  # вычисляется, не хранится — см. services/scheduler.py


class ScheduledJobRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    scheduled_job_id: int
    status: ScheduledJobRunStatus
    meters_total: int
    meters_succeeded: int
    meters_failed: int
    started_at: datetime
    finished_at: datetime | None


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: NotificationCategory
    message: str
    meter_id: int | None
    scheduled_job_id: int | None
    details: dict | None
    is_read: bool
    created_at: datetime


# --- Этап 5: биллинг (ТЗ п.4.2.9/4.2.10, API.docx) ---


class BillingApiKeyCreate(BaseModel):
    client_id: str = Field(min_length=1, max_length=64)
    description: str | None = None
    rate_limit_per_minute: int = Field(default=60, ge=1, le=1000)


class BillingApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: str
    description: str | None
    rate_limit_per_minute: int
    status: BillingApiKeyStatus
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None


class BillingApiKeyCreated(BillingApiKeyOut):
    # Секрет отдаётся ОДИН РАЗ, только в ответ на создание — далее
    # хранится исключительно в хешированном виде (API.docx п.3.1).
    api_key: str


class MeterReadingBillingOut(BaseModel):
    meter_serial: str
    obis_code: str
    value: object
    unit: str | None
    read_at: datetime


class MeterTariffBillingOut(BaseModel):
    meter_serial: str
    tariff_schedule: list[dict]
    updated_at: datetime


class DisconnectBatchRequest(BaseModel):
    reason: str | None = None
    # Верхний лимit размера пакета (API.docx раздел 6, 500 счётчиков)
    # намеренно НЕ объявлен здесь через Field(max_length=...) — pydantic
    # отклонил бы превышение как обычную ошибку валидации ДО того, как
    # управление дойдёт до billing.py, и клиент получил бы общий
    # VALIDATION_ERROR вместо специфичного BATCH_TOO_LARGE (API.docx
    # Приложение А.2). Проверяется явно в billing._create_batch.
    meters: list[str] = Field(min_length=1, description="Серийные номера счётчиков")


class DisconnectBatchAccepted(BaseModel):
    batch_id: str
    accepted_count: int
    status: DisconnectBatchStatus
    status_url: str


class DisconnectBatchItemOut(BaseModel):
    meter_serial: str
    status: DisconnectBatchItemStatus
    error_code: str | None = None


class DisconnectBatchStatusOut(BaseModel):
    batch_id: str
    operation: str
    status: DisconnectBatchStatus
    created_at: datetime
    finished_at: datetime | None
    items: list[DisconnectBatchItemOut]
