"""Конфигурация Backend API (ТЗ п. 4.4.2). Секреты — только через переменные
окружения/.env (Promt_MMWS.md, раздел 2, «на Этапе 0-1 допустим .env с
правами 600, не в git»), никогда не хардкодятся.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="MMWS_", extra="ignore")

    database_url: str = "postgresql+asyncpg://mmws:mmws@localhost:5432/mmws"

    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_ttl_minutes: int = 30
    jwt_refresh_token_ttl_days: int = 14

    # Ключ шифрования секретов счётчиков (пароль доступа, AES-ключ канала) в
    # покое (Promt_MMWS.md, раздел 3, принцип 4). Fernet-ключ, генерируется
    # `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
    secret_encryption_key: str

    gateway_grpc_target: str = "localhost:50051"

    job_poll_interval_s: float = 1.0
    # 2026-09-07, по решению пользователя — раньше воркер был ровно один
    # (worker_loop — единственная asyncio-задача), и очередь job'ов
    # обрабатывалась строго последовательно: при call-home-чтениях
    # (десятки-сотни секунд на попытку) это быстро становится узким
    # местом при росте парка счётчиков. Сама Gateway к параллельным
    # чтениям уже готова (gRPC-сервер на ThreadPoolExecutor, пул
    # call-home соединений потокобезопасен, см. gateway/grpc_server.py) —
    # не хватало только нескольких экземпляров worker_loop на стороне
    # Backend. См. main.py (запуск N задач) и db.py (пул соединений к
    # БД увеличен пропорционально).
    job_worker_concurrency: int = 8

    # Этап 4 (ТЗ п.4.2.8) — «настраиваемый таймаут» перехода счётчика в
    # offline; та же величина используется и в Meter.is_online (единая
    # точка настройки вместо разъехавшихся хардкодов).
    meter_offline_timeout_s: int = 3600
    notification_check_interval_s: float = 60.0
    scheduler_check_interval_s: float = 30.0

    cors_allow_origins: list[str] = ["http://localhost:5173"]


settings = Settings()
