"""ORM-модели (ТЗ Приложение Б, Promt_MMWS.md Этап 1 — минимальный состав
таблиц). Секреты счётчика (`password_encrypted`, `aes_key_encrypted`)
хранятся зашифрованными (см. `app/core/security.py:encrypt_secret`) —
никогда не читаются/не логируются в открытом виде вне момента вызова
Gateway (Promt_MMWS.md, раздел 3, принцип 4).
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class UserRole(str, enum.Enum):
    """ТЗ п. 4.2.1 / Приложение Б, TABLE 0 — ровно пять ролей."""

    OPERATOR = "operator"
    ENGINEER = "engineer"
    OBSERVER = "observer"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


class ProtocolProfile(str, enum.Enum):
    """ТЗ п. 4.3.2 — три протокольных профиля Risesun."""

    MODE_C = "mode_c"
    MODE_E = "mode_e"
    HDLC_DLMS = "hdlc_dlms"


class GatewayStatus(str, enum.Enum):
    """ТЗ п. 4.1.1 — регистрация нового Gateway требует подтверждения
    ролью «Супер-администратор», без авторегистрации."""

    PENDING = "pending"
    APPROVED = "approved"
    DISABLED = "disabled"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ParameterWriteResult(str, enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Gateway(Base):
    """Реестр экземпляров Protocol Gateway (ТЗ п. 4.1.1)."""

    __tablename__ = "gateways"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    manufacturer: Mapped[str] = mapped_column(String(64), nullable=False, default="Risesun")
    driver_version: Mapped[str] = mapped_column(String(32), nullable=True)
    grpc_target: Mapped[str] = mapped_column(String(255), nullable=False)  # host:port внутреннего RPC
    supported_operations: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[GatewayStatus] = mapped_column(
        Enum(GatewayStatus, name="gateway_status"), nullable=False, default=GatewayStatus.PENDING
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    registered_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    meters: Mapped[list["Meter"]] = relationship(back_populates="gateway", lazy="selectin")

    @property
    def is_online(self) -> bool:
        if self.last_heartbeat_at is None:
            return False
        from datetime import timezone

        return (datetime.now(timezone.utc) - self.last_heartbeat_at).total_seconds() < 120


class Meter(Base):
    """Справочник счётчиков (ТЗ п. 4.2.2)."""

    __tablename__ = "meters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    serial_number: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    # ip_address/port обязательны только для обычной модели (Gateway —
    # инициатор, ТЗ Table 1). Для call-home счётчиков (звонят сами —
    # подтверждённое расхождение с ТЗ, см. DECISIONS.md) они необязательны:
    # Gateway опознаёт звонящий счётчик по serial_number через свой
    # call-home пул, а не по адресу назначения.
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_call_home: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    protocol_profile: Mapped[ProtocolProfile] = mapped_column(
        Enum(ProtocolProfile, name="protocol_profile"), nullable=False
    )
    master_address: Mapped[str | None] = mapped_column(String(32), nullable=True)
    physical_address: Mapped[str | None] = mapped_column(String(32), nullable=True)
    logical_address: Mapped[str | None] = mapped_column(String(32), nullable=True)
    password_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    aes_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    gateway_id: Mapped[int] = mapped_column(ForeignKey("gateways.id"), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    gateway: Mapped[Gateway] = relationship(back_populates="meters", lazy="selectin")

    @property
    def is_online(self) -> bool:
        if self.last_seen_at is None:
            return False
        from datetime import timezone

        return (datetime.now(timezone.utc) - self.last_seen_at).total_seconds() < 3600


class MeterReading(Base):
    """Снятые показания (ТЗ Приложение Б)."""

    __tablename__ = "meter_readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), nullable=False, index=True)
    obis_code: Mapped[str] = mapped_column(String(32), nullable=False)
    value_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)


class LoadProfileData(Base):
    """Строки буфера профиля нагрузки (Этап 3, ТЗ п.4.2.3).

    Уникальность (meter_id, obis_code, timestamp) делает повторное чтение
    пересекающегося диапазона дат идемпотентным — Backend просто
    вставляет строки с ``ON CONFLICT DO NOTHING`` (см. job_worker), без
    отдельного отслеживания "точки докачки": обрыв связи посреди
    передачи (Job помечается is_partial в error) не оставляет дыр —
    достаточно поставить новый job с тем же (или чуть более широким)
    диапазоном дат, уже сохранённые строки просто не продублируются."""

    __tablename__ = "load_profile_data"
    __table_args__ = (UniqueConstraint("meter_id", "obis_code", "timestamp", name="uq_load_profile_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), nullable=False, index=True)
    obis_code: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    # Остальные колонки буфера (первая — timestamp, уже вынесена в
    # отдельное поле) — JSON-совместимый вид, тот же принцип, что и
    # meter_readings.value_json.
    values_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EventLog(Base):
    """Журнал событий счётчика (ТЗ п. 4.2.3, Приложение Г.4)."""

    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TamperLog(Base):
    """Журнал вмешательств счётчика (ТЗ п. 4.2.3, Приложение Г.4)."""

    __tablename__ = "tamper_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MeterStatusSnapshot(Base):
    """Снимки статусного слова и кодов ошибок счётчика (ТЗ Приложение Б)."""

    __tablename__ = "meter_status_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), nullable=False, index=True)
    status_word_hex: Mapped[str] = mapped_column(String(64), nullable=False)
    error_codes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Job(Base):
    """Асинхронные операции (Promt_MMWS.md, раздел 3, принцип 3 — длительные
    операции не блокируют HTTP-ответ). Этап 1: только job_type='read_current'."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), nullable=False, index=True)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status"), nullable=False, default=JobStatus.QUEUED, index=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ParameterWriteHistory(Base):
    """История операций записи параметров на счётчики (Этап 2, ТЗ п.4.2.4).

    Обязательный состав полей по ТЗ: пользователь, время, счётчик,
    изменяемый параметр, предыдущее и новое значение, результат —
    заполняется безусловно при КАЖДОЙ попытке записи (успешной или нет),
    независимо от общего audit_log (тот же принцип раздельного
    журналирования, что и у event_log/tamper_log — предметный журнал
    отдельно от общего аудита действий)."""

    __tablename__ = "parameter_write_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), nullable=False, index=True)
    # Машиночитаемое имя параметра (напр. "datetime.time", "datetime.date") —
    # не OBIS-код напрямую, т.к. одна логическая операция записи (Этап 2,
    # итерация 1: "дата и время") может затрагивать несколько OBIS-объектов.
    parameter: Mapped[str] = mapped_column(String(64), nullable=False)
    obis_code: Mapped[str] = mapped_column(String(32), nullable=False)
    # old_value может быть NULL — не для каждой операции записи
    # предварительное чтение осмысленно (напр. синхронизация часов "на
    # текущее время" не требует знания предыдущего показания часов
    # счётчика для аудита операции как таковой); когда прочитано —
    # значение то же представление, что и meter_readings.value_json.
    old_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[ParameterWriteResult] = mapped_column(
        Enum(ParameterWriteResult, name="parameter_write_result"), nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AuditLog(Base):
    """Неизменяемый журнал аудита (ТЗ п. 4.2.7). Append-only — никаких
    UPDATE/DELETE в прикладном коде поверх этой таблицы."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="web")  # web|billing|system
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False)  # success|failure
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
