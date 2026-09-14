"""ORM-модели (ТЗ Приложение Б, Promt_MMWS.md Этап 1 — минимальный состав
таблиц). Секреты счётчика (`password_encrypted`, `aes_key_encrypted`)
хранятся зашифрованными (см. `app/core/security.py:encrypt_secret`) —
никогда не читаются/не логируются в открытом виде вне момента вызова
Gateway (Promt_MMWS.md, раздел 3, принцип 4).
"""

from __future__ import annotations

import enum
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
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

from .config import settings
from .db import Base

_BISHKEK_TZ = ZoneInfo("Asia/Bishkek")


def _is_same_bishkek_day(moment: datetime, now: datetime) -> bool:
    return moment.astimezone(_BISHKEK_TZ).date() == now.astimezone(_BISHKEK_TZ).date()


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


class MeterStatus(str, enum.Enum):
    """Этап 6 — обнаружение новых счётчиков. Счётчик, самостоятельно
    позвонивший на call-home порт Gateway и опознанный по серийному
    номеру (см. Gateway.ListCallHomeSerials), заводится в справочник
    автоматически со статусом INSTALLED — Backend ещё не знает пароль
    доступа и протокольный профиль, работать с таким счётчиком нельзя.
    Администратор переводит его в ACTIVE, одновременно заполняя
    обязательные для реальной работы поля (см. POST /activate).

    Не путать с ``Meter.is_active`` — тот управляет ОРТОГОНАЛЬНЫМ
    смыслом «включён/выключен из обслуживания» для уже полностью
    настроенного (ACTIVE) счётчика, применяется вместо удаления при
    наличии истории (см. delete_meter).

    INVALID (2026-09-08) — счётчик с заведомо некорректными данными
    (обнаружено на практике: серийный номер, повреждённый при call-home
    обнаружении, содержит буквы вместо цифр — addressing.py не может
    вычислить физический адрес, чтение падает с ADDRESSING_ERROR на
    каждой попытке). Переводится сюда вместо ACTIVE/INSTALLED вручную
    администратором, одновременно выставляется is_active=False, чтобы
    планировщик перестал впустую тратить на него попытки воркеров (см.
    scheduler._trigger_one). Вкладка «Некорректные данные» в UI
    позволяет исправить serial_number/ip_address и затем повторно
    активировать тем же POST /activate, что и для INSTALLED."""

    INSTALLED = "installed"
    ACTIVE = "active"
    INVALID = "invalid"


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
    # Этап 6 — фактический порт call-home пула этого экземпляра Gateway,
    # отражается на каждом heartbeat (см. services/heartbeat.py) вне
    # зависимости от того, менялся ли он через панель суперадминистратора
    # или переменной окружения при перезапуске контейнера. NULL — ещё ни
    # разу не получен heartbeat с этим полем (старый Gateway/только что
    # зарегистрирован) либо call-home на этом экземпляре не запущен.
    call_home_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 2026-09-12 (по просьбе пользователя — панель "Шлюзы" показывала
    # только основной порт, хотя реально слушаются 17 портов по РЭСам,
    # см. services/res_mapping.py) — ВСЕ фактически слушаемые порты
    # (основной + extra_bind_ports), отражается на каждом heartbeat так
    # же, как и call_home_port. NULL/пустой список — то же самое, что и
    # у call_home_port (heartbeat ещё не было, либо call-home не
    # запущен).
    call_home_ports: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)
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
    # NULL только для только что автообнаруженных (status=INSTALLED)
    # счётчиков — Gateway опознаёт звонящий call-home по серийному
    # номеру, но НЕ по протокольному профилю; администратор указывает
    # его при активации (см. POST /{id}/activate).
    protocol_profile: Mapped[ProtocolProfile | None] = mapped_column(
        Enum(ProtocolProfile, name="protocol_profile"), nullable=True
    )
    master_address: Mapped[str | None] = mapped_column(String(32), nullable=True)
    physical_address: Mapped[str | None] = mapped_column(String(32), nullable=True)
    logical_address: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # NULL для status=INSTALLED — пароль доступа не может быть узнан из
    # самого факта звонка домой (DL/T645-анонс несёт только адрес, не
    # пароль DLMS), заполняется администратором при активации.
    password_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    aes_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[MeterStatus] = mapped_column(
        Enum(MeterStatus, name="meter_status"), nullable=False, default=MeterStatus.ACTIVE
    )
    gateway_id: Mapped[int] = mapped_column(ForeignKey("gateways.id"), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Отдельная от status категория (2026-09-11, по просьбе пользователя)
    # — счётчик активен и штатно опрашивается, но показывает
    # пренебрежимо малое/нулевое потребление продолжительное время
    # (напр. счётчик 202001002236: 0.03 кВт·ч, без расхода за прошлый
    # месяц) — повод для отдельного внимания (обрыв линии у абонента?
    # незаселённый объект? неисправность самого счётчика?), но НЕ
    # признак неисправности сбора данных, поэтому отдельно от
    # MeterStatus.INVALID. Проставляется вручную через UI/API либо
    # разовой сверкой (см. DECISIONS.md) — автоматического постоянного
    # детектора пока нет (историю показаний хватает не у всех счётчиков
    # для надёжного суждения "нет расхода за месяц").
    is_low_consumption: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Токовый класс счётчика (Maximum Current, атрибут "Imax", OBIS
    # `1.1.0.6.3.ff` — гипотеза, не подтверждена реальным трафиком на
    # момент добавления поля, см. DECISIONS.md 2026-09-07) — статичный
    # паспортный параметр, читается один раз (см. job_type
    # "read_rated_current", services/scheduler.py) и больше не
    # запрашивается повторно, раз уже известен.
    rated_current_amps: Mapped[float | None] = mapped_column(Float, nullable=True)
    # РЭС/объект (2026-09-11, по просьбе пользователя) — определяется по
    # тому, на какой call-home порт Gateway'я приходит соединение
    # счётчика (пользователь физически развёл дозвон разных РЭС по
    # портам, см. services/res_mapping.py, docker-compose.yml), пишется
    # автоматически при каждом опознании (тем же неблокирующим фоновым
    # путём, что и ip_address) — не "один раз узнали и забыли", а всегда
    # свежее значение, т.к. переразводка портов пользователем меняет
    # фактическую принадлежность.
    res_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    gateway: Mapped[Gateway] = relationship(back_populates="meters", lazy="selectin")

    @property
    def is_online(self) -> bool:
        from datetime import timezone

        now = datetime.now(timezone.utc)
        if self.last_seen_at is not None:
            if (now - self.last_seen_at).total_seconds() < settings.meter_offline_timeout_s:
                return True
        # Счётчик, уже успешно передавший показание СЕГОДНЯ (по времени
        # Asia/Bishkek — тот же календарный день, что использует
        # scheduler._already_read_today_meter_ids для «не опрашивать
        # повторно»), считается онлайн весь остаток этих суток — даже
        # если с последнего чтения прошло больше meter_offline_timeout_s
        # (согласовано с пользователем 2026-09-07: получение показания —
        # само по себе доказательство связи, ежесуточный опрос может быть
        # реже часового таймаута).
        if self.last_read_at is not None and _is_same_bishkek_day(self.last_read_at, now):
            return True
        return False


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


class ObisReferenceEntry(Base):
    """Полный справочник объектов (Association View) по каждой из 3
    моделей DTZY217 — импорт из XML-экспорта заводской сервисной
    программы (``C:\\NEW_DLMS\\Suzak\\*.xml`` на референсной боевой
    системе, формат ``ArrayOfGXDLMSObject``, 2026-09-10). В отличие от
    ``app/obis_catalog.py`` (намеренно маленький, только коды с
    подтверждённым в ЭТОМ проекте назначением) — это ПОЛНЫЙ каталог
    объектов от производителя (2941 объект × 3 модели), источник для
    поиска/сверки, а не курируемый список. Несколько записей уже
    сверены с ``obis_catalog.py`` и совпали (1.1.1.8.0.255, 1.1.0.6.3.255,
    1.1.99.1.0.255) — при этом ``1.1.60.50.0.ff`` (используется в
    ``dlms.VALUE_OBIS_OVERRIDES``) НЕ найден ни в одной из 3 моделей,
    см. DECISIONS.md."""

    __tablename__ = "obis_reference_entries"
    __table_args__ = (UniqueConstraint("meter_model", "logical_name", name="uq_obis_reference_entry"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # "100V 5A" | "380V 100A" | "380V 5A" — суффикс, совпадающий с
    # Meter.model (напр. "DTZY217 (380V 100A)").
    meter_model: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    class_name: Mapped[str] = mapped_column(String(64), nullable=False)  # напр. "GXDLMSRegister"
    class_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    logical_name: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # "1.1.1.8.0.255"
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scaler: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit: Mapped[int | None] = mapped_column(Integer, nullable=True)


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
    # Этап 4 (ТЗ п.4.2.6) — если задача создана планировщиком по
    # расписанию (а не вручную оператором), ссылается на конкретный
    # запуск ScheduledJobRun, к которому она относится (один запуск
    # расписания порождает по одной Job на каждый счётчик группы).
    scheduled_job_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("scheduled_job_runs.id"), nullable=True, index=True
    )


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


class ScheduledJobRunStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL_FAILURE = "partial_failure"
    FAILED = "failed"


class NotificationCategory(str, enum.Enum):
    """ТЗ п.4.2.8 — три триггера уведомлений. TAMPER_EVENT заведён как
    инфраструктура на будущее: чтение журнала вмешательств со счётчика
    (ТЗ п.4.2.3) ещё не реализовано ни в одном из этапов (словарь OBIS
    описывает его не как буфер-профиль, а как набор счётчиков/
    длительностей по категориям событий — отдельная по форме
    протокольная задача, см. DECISIONS.md), поэтому эта категория пока
    ничем не порождается — таблица tamper_log всегда пуста."""

    METER_OFFLINE = "meter_offline"
    TAMPER_EVENT = "tamper_event"
    SCHEDULED_JOB_FAILED = "scheduled_job_failed"


class ParameterScheme(Base):
    """Именованная схема параметров (ТЗ п.4.2.5 — аналог Save/Load Scheme
    исходного приложения). Область — параметры из реестра
    ``WRITABLE_INT_PARAMETERS`` (Этап 2); ``write_datetime`` (не имеет
    пользовательского значения — всегда «сейчас») в схемы не входит."""

    __tablename__ = "parameter_schemes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{"parameter": "settlement_no", "value": 3}, ...] — ключи parameter
    # сверяются с WRITABLE_INT_PARAMETERS при сохранении и применении
    # (тот же принцип валидации, что у POST /write-parameter/{parameter}).
    parameters: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )


class PollProfile(Base):
    """Именованный профиль опроса (по просьбе пользователя, 2026-09-08):
    набор OBIS-кодов с пояснением, что каждый из них опрашивает, и
    признаком включён/выключен — используется в ``ScheduledJob``
    (``operation_params.poll_profile_id`` для ``job_type=read_current``)
    вместо одного жёстко заданного OBIS, чтобы одно расписание могло
    опрашивать сразу НЕСКОЛЬКО показателей у каждого счётчика группы за
    один тик (см. ``services.scheduler._resolve_payloads`` — на каждый
    включённый пункт профиля создаётся отдельная ``Job``, как и для
    остальных OBIS, независимо наблюдаемая/переповторяемая)."""

    __tablename__ = "poll_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{"obis": "1.1.1.8.0.ff", "label": "Суммарная активная энергия", "enabled": true}, ...]
    items: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )


class ScheduledJob(Base):
    """Расписание автоматического опроса группы счётчиков (ТЗ п.4.2.6).

    ``next_run_at`` сознательно НЕ хранится как столбец — вычисляется на
    лету из ``cron_expression`` и ``last_run_at`` (croniter) там, где
    нужен (API-сериализация, планировщик); отдельное персистентное поле
    только создавало бы риск рассинхронизации с самим cron-выражением
    при его редактировании."""

    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    cron_expression: Mapped[str] = mapped_column(String(64), nullable=False)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)  # "read_current" | "read_load_profile"
    # read_current -> {"obis": "..."} ИЛИ {"poll_profile_id": N}
    # (2026-09-08 — см. PollProfile: несколько OBIS за один тик,
    # каждый отдельной Job); read_load_profile -> {"window_hours": N}
    # — каждый запуск запрашивает последние N часов (скользящее окно от
    # текущего момента, а не от прошлого запуска — проще и устойчивее к
    # пропущенным/задержанным тикам планировщика).
    operation_params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    meter_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScheduledJobRun(Base):
    """Журнал одного запуска расписания (ТЗ п.4.2.6/4.2.11 — «журнал
    выполнения каждого запуска»). Один запуск порождает по одной ``Job``
    на каждый счётчик группы (см. ``Job.scheduled_job_run_id``);
    ``status`` — сводный итог по завершении всех дочерних Job."""

    __tablename__ = "scheduled_job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheduled_job_id: Mapped[int] = mapped_column(ForeignKey("scheduled_jobs.id"), nullable=False, index=True)
    status: Mapped[ScheduledJobRunStatus] = mapped_column(
        Enum(ScheduledJobRunStatus, name="scheduled_job_run_status"),
        nullable=False,
        default=ScheduledJobRunStatus.RUNNING,
    )
    meters_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meters_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meters_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EudbExportRunStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL_FAILURE = "partial_failure"
    FAILED = "failed"


class EudbExportRun(Base):
    """Журнал одного запуска ежедневного экспорта профиля нагрузки
    (Profile1) во внешнюю БД ЕЭБД (2026-09-14, по прямому указанию
    пользователя — см. services/eudb_export.py). Тот же принцип
    журналирования, что и у ``ScheduledJobRun``, но отдельная таблица:
    это не job планировщика (нет привязки к конкретному счётчику через
    ``Job``, весь запуск — один процесс, читающий из ``load_profile_
    data`` и пишущий в ЧУЖУЮ БД, не через обычный gateway/job_worker
    путь)."""

    __tablename__ = "eudb_export_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[EudbExportRunStatus] = mapped_column(
        Enum(EudbExportRunStatus, name="eudb_export_run_status"),
        nullable=False,
        default=EudbExportRunStatus.RUNNING,
    )
    meters_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meters_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meters_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # "Только запросу" — было ли это по расписанию (23:50) или по кнопке
    # "Запустить сейчас" в интерфейсе.
    triggered_manually: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class EudbExportItem(Base):
    """Результат экспорта ОДНОГО счётчика в рамках запуска (см.
    ``EudbExportRun``) — нужен для интерфейса: какие именно счётчики не
    экспортировались и почему (не найден UUID в ЕЭБД, профиль не в
    ожидаемом формате колонок, свежих данных профиля вообще нет и т.п.)."""

    __tablename__ = "eudb_export_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("eudb_export_runs.id"), nullable=False, index=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), nullable=False, index=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rows_exported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    """ТЗ п.4.2.8. Уведомления общесистемные (не персональный inbox на
    пользователя — ТЗ не описывает разный набор уведомлений по ролям),
    ``is_read`` — общий флаг «кто-то из пользователей уже видел»."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[NotificationCategory] = mapped_column(
        Enum(NotificationCategory, name="notification_category"), nullable=False, index=True
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    meter_id: Mapped[int | None] = mapped_column(ForeignKey("meters.id"), nullable=True)
    scheduled_job_id: Mapped[int | None] = mapped_column(ForeignKey("scheduled_jobs.id"), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class BillingApiKeyStatus(str, enum.Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class BillingApiKey(Base):
    """Техническая учётная запись биллинговой системы (ТЗ п.4.2.9,
    API.docx раздел 3) — отдельная от пользовательских учётных записей
    веб-интерфейса, без JWT-сессии. Секрет хранится хешированным (тот
    же bcrypt, что и пароли пользователей, — секрет генерируется
    случайно на сервере, а не выбирается человеком, поэтому общий
    механизм хеширования подходит без изменений) и показывается
    администратору только один раз, в момент выдачи."""

    __tablename__ = "billing_api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # ТЗ раздел 6 API.docx: «не более 60 запросов в минуту... конфигурируется
    # индивидуально по согласованию с Заказчиком» — не общесистемная
    # константа, а атрибут конкретного ключа.
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    status: Mapped[BillingApiKeyStatus] = mapped_column(
        Enum(BillingApiKeyStatus, name="billing_api_key_status"), nullable=False, default=BillingApiKeyStatus.ACTIVE
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DisconnectBatchOperation(str, enum.Enum):
    DISCONNECT = "disconnect"
    RECONNECT = "reconnect"


class DisconnectBatchStatus(str, enum.Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


class DisconnectBatchItemStatus(str, enum.Enum):
    QUEUED = "queued"
    DONE = "done"
    FAILED = "failed"


class DisconnectBatch(Base):
    """Пакетная операция отключения/подключения от биллинга (ТЗ п.4.2.10,
    API.docx п.4.3-4.5) — тот же архитектурный принцип, что и
    ScheduledJobRun (Этап 4): пакет порождает по одной обычной Job на
    каждый счётчик, сам не читает/не пишет на счётчики напрямую.

    ``idempotency_key`` уникален В ПРЕДЕЛАХ ОДНОГО API-ключа (составной
    UNIQUE) — повторная отправка того же Idempotency-Key тем же клиентом
    возвращает уже принятый пакет (API.docx п.2.3), не создавая новый."""

    __tablename__ = "disconnect_batches"
    __table_args__ = (UniqueConstraint("api_key_id", "idempotency_key", name="uq_disconnect_batch_idempotency"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    operation: Mapped[DisconnectBatchOperation] = mapped_column(
        Enum(DisconnectBatchOperation, name="disconnect_batch_operation"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key_id: Mapped[int] = mapped_column(ForeignKey("billing_api_keys.id"), nullable=False, index=True)
    status: Mapped[DisconnectBatchStatus] = mapped_column(
        Enum(DisconnectBatchStatus, name="disconnect_batch_status"),
        nullable=False,
        default=DisconnectBatchStatus.QUEUED,
    )
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DisconnectBatchItem(Base):
    """Один счётчик в составе пакета — ``meter_serial`` хранится ВСЕГДА
    (как прислал биллинг), ``meter_id`` может быть NULL, если серийный
    номер не найден в справочнике (статус сразу ``failed``,
    ``error_code="METER_NOT_FOUND"`` — сам факт присутствия элемента в
    ответе обязателен по API.docx, п.4.5, даже для ненайденных счётчиков)."""

    __tablename__ = "disconnect_batch_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("disconnect_batches.id"), nullable=False, index=True)
    meter_serial: Mapped[str] = mapped_column(String(32), nullable=False)
    meter_id: Mapped[int | None] = mapped_column(ForeignKey("meters.id"), nullable=True)
    status: Mapped[DisconnectBatchItemStatus] = mapped_column(
        Enum(DisconnectBatchItemStatus, name="disconnect_batch_item_status"),
        nullable=False,
        default=DisconnectBatchItemStatus.QUEUED,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)


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
