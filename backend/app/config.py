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
    #
    # 2026-09-08 — поднято с 1ч до 24ч. Причина: профиль опроса по
    # умолчанию (18 OBIS/счётчик) увеличил число пар «счётчик×код» с 147
    # до 2646, а суммарная скорость воркеров не изменилась (~8 воркеров,
    # ~3 попытки/мин) — полный круг по очереди стал занимать ~14ч вместо
    # ~45 минут. При прежнем часовом окне это выглядело как массовый уход
    # счётчиков в offline (наблюдение: "Online" на дашборде уходило на
    # убывание), хотя связь с устройствами не терялась — просто до них
    # физически не успевали дойти в очереди за час. 24ч согласуется с тем,
    # что сам профиль опроса по умолчанию задуман как ежедневный.
    meter_offline_timeout_s: int = 86400
    notification_check_interval_s: float = 60.0
    scheduler_check_interval_s: float = 30.0

    cors_allow_origins: list[str] = ["http://localhost:5173"]

    # 2026-09-09 — событийное чтение call-home счётчиков сразу при
    # подключении (см. DECISIONS.md, план /root/.claude/plans/
    # ticklish-popping-bear.md): Gateway на своём внутреннем HTTP-канале
    # спрашивает Backend "что сейчас нужно прочитать у этого счётчика" и
    # репортит результаты — общий секрет вместо пользовательской
    # аутентификации, т.к. это трафик исключительно между контейнерами
    # внутри docker-сети, не от внешнего клиента. Без дефолта, как
    # jwt_secret_key/secret_encryption_key — приложение не должно молча
    # стартовать с пустым/предсказуемым секретом на этом канале.
    gateway_internal_secret: str
    # Верхний предел OBIS, забираемых одним claim-запросом на один
    # счётчик — профиль опроса по умолчанию сейчас ~18-22 OBIS, запас
    # с кратным потолком (тот же принцип, что и у DEFAULT_MAX_PER_SERIAL
    # в gateway/callhome.py).
    gateway_internal_claim_batch_max: int = 50
    # Глобальный выключатель нового пути — по умолчанию ВЫКЛ (раскатка
    # только через явное включение + allowlist серийников, см. план).
    immediate_read_enabled: bool = False
    # Пустой список при immediate_read_enabled=True означает "без
    # ограничения" (весь активный call-home парк); непустой — только
    # перечисленные серийники, для поэтапного раскатывания.
    immediate_read_allowed_serials: list[str] = []
    # Порог, после которого RUNNING-задача считается зависшей и
    # возвращается в QUEUED периодическим stale_job_reaper_loop —
    # должен быть заметно больше самого длинного легитимного
    # call_timeout_s в проекте (220с у чтения профиля нагрузки), иначе
    # можно вернуть в очередь задачу, которую старый путь ещё реально
    # выполняет.
    stale_running_job_reap_after_s: float = 300.0

    # 2026-09-14, по прямому указанию пользователя — ежедневный экспорт
    # профиля нагрузки (Profile1) во внешнюю БД ЕЭБД (Единая база данных
    # энергосбыта, отдельный сервер/схема — НЕ наш Postgres). Пусто по
    # умолчанию — экспорт неактивен, пока явно не настроен через .env
    # (см. services/eudb_export.py). Отдельные host/port вместо готовой
    # DSN-строки — единообразно с MMWS_DATABASE_URL нельзя, т.к. пароль
    # ЕЭБД может содержать символы, требующие url-квотирования, а
    # asyncpg.connect() принимает их раздельными kwargs без этой возни.
    eudb_host: str = ""
    eudb_port: int = 5432
    eudb_database: str = ""
    eudb_user: str = ""
    eudb_password: str = ""
    # UUID справочника производителей ЕЭБД для Risesun (cd.ctl_meter_
    # models.ref_producer) — используется в SELECT поиска mtr_guid по
    # серийнику, см. eudb_export.py.
    eudb_risesun_producer_guid: str = "972ccc41-0489-47cc-8056-f4bf1c2ede65"
    # Локальное время (Asia/Bishkek — тот же пояс, что и у показаний
    # счётчиков), в которое ежедневно запускается экспорт — по просьбе
    # пользователя "в конце суток, 23:50".
    eudb_export_hour_bishkek: int = 23
    eudb_export_minute_bishkek: int = 50


settings = Settings()
