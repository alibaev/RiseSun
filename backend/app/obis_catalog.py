"""Карта OBIS-кодов (по просьбе пользователя, 2026-09-08) — справочный
список ВСЕХ OBIS-кодов, реально используемых где-либо в проекте (не
полный словарь ``OBIS.xlsx``, 9166 строк на английском без перевода, а
только те коды, для которых в этом проекте уже есть подтверждённое или
задокументированное назначение). Источник для каждого кода — тот же
файл/строка, где этот код используется как константа; список сделан
намеренно НЕБОЛЬШИМ и точным, а не исчерпывающим — угадывать описания
для непроверенных кодов из ``OBIS.xlsx`` означало бы гадать протокол
без реального трафика, чего этот проект весь день принципиально
избегает (см. DECISIONS.md, разделы про адресацию).

Единственный сознательно ИСКЛЮЧЁННЫЙ код — ``1.1.60.50.0.ff``
(``dlms.VALUE_OBIS_OVERRIDES``): это внутренняя протокольная деталь
(GET value для энергии реально уходит по другому OBIS, чем указанный
пользователем), не то, что имеет смысл опрашивать НАПРЯМУЮ — Gateway
подставляет его сам, прозрачно для профиля опроса.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ObisEntry:
    number: str  # "000", "001", ... — сквозной номер для справочника, не часть OBIS
    obis: str
    label: str
    description: str
    source: str


OBIS_CATALOG: list[ObisEntry] = [
    ObisEntry(
        number="000",
        obis="1.1.1.8.0.ff",
        label="Активная энергия, приём, всего",
        description=(
            "Суммарная активная энергия (накопительный счётчик, приём) — основной показатель "
            "read_current по умолчанию. Значение читается по одному GET, отдельный GET scaler_unit "
            "перед value обязателен на реальном оборудовании (см. DECISIONS.md)."
        ),
        source="job_worker.py:_run_read_current (обис по умолчанию)",
    ),
    ObisEntry(
        number="001",
        obis="1.1.0.6.3.ff",
        label="Максимальный (номинальный) ток — токовый класс",
        description=(
            "Паспортный параметр счётчика (100А/5А и т.п.) — читается ОДИН РАЗ на счётчик "
            "(read_rated_current), результат сохраняется на сам счётчик, а не как показание. "
            "Гипотеза адреса не подтверждена реальным трафиком (см. DECISIONS.md, 2026-09-07)."
        ),
        source="job_worker.py:RATED_CURRENT_OBIS",
    ),
    ObisEntry(
        number="002",
        obis="1.1.63.1.0.ff",
        label="Профиль нагрузки (буфер, канал 1)",
        description=(
            "Буферизованные записи через равные интервалы (Load profile, recording period 1, "
            "канал 1) — используется read_load_profile. Подтверждено экспортом реальной объектной "
            "модели счётчика DTZY217 (заводская сервисная программа, 2026-08-19)."
        ),
        source="load_profile.py:DEFAULT_LOAD_PROFILE_OBIS",
    ),
    ObisEntry(
        number="003",
        obis="1.0.0.9.1.ff",
        label="Текущее время (запись)",
        description=(
            "Объект класса Data (не DLMS Clock) — часть составной операции синхронизации "
            "времени счётчика с системным (write_datetime), отдельно от Даты. Кодировка байт "
            "НЕ подтверждена на реальном оборудовании."
        ),
        source="job_worker.py:_DATETIME_TIME_OBIS",
    ),
    ObisEntry(
        number="004",
        obis="1.0.0.9.2.ff",
        label="Текущая дата (запись)",
        description=(
            "Объект класса Data (не DLMS Clock) — вторая часть составной операции синхронизации "
            "времени счётчика (write_datetime), отдельно от Времени. Кодировка байт НЕ "
            "подтверждена на реальном оборудовании."
        ),
        source="job_worker.py:_DATETIME_DATE_OBIS",
    ),
    ObisEntry(
        number="005",
        obis="1.0.0.1.0.ff",
        label="Текущий номер расчётного периода",
        description="Записываемый параметр (0-255) — из реестра WRITABLE_INT_PARAMETERS, используется в схемах параметров.",
        source="write_parameters.py:settlement_no",
    ),
    ObisEntry(
        number="006",
        obis="1.0.0.1.1.ff",
        label="Доступный номер расчётного периода",
        description="Записываемый параметр (0-255) — из реестра WRITABLE_INT_PARAMETERS, используется в схемах параметров.",
        source="write_parameters.py:available_settlement_no",
    ),
    ObisEntry(
        number="007",
        obis="1.0.0.8.4.ff",
        label="Интервал профиля нагрузки (мин)",
        description="Записываемый параметр (0-255) — из реестра WRITABLE_INT_PARAMETERS, используется в схемах параметров.",
        source="write_parameters.py:load_profile_interval",
    ),
    ObisEntry(
        number="008",
        obis="1.0.60.34.1.ff",
        label="Количество режимов отображения",
        description="Записываемый параметр (0-255) — из реестра WRITABLE_INT_PARAMETERS, используется в схемах параметров.",
        source="write_parameters.py:display_mode_count",
    ),
    ObisEntry(
        number="009",
        obis="1.0.60.36.8.ff",
        label="Тип тарифа выходного дня",
        description=(
            "Записываемый параметр (0-255) — из реестра WRITABLE_INT_PARAMETERS. В словаре "
            "OBIS.xlsx есть два похожих кандидата (96.54.8 и 96.72.0) — выбран первый, какой из "
            "двух верен для наших счётчиков, не подтверждено (см. write_parameters.py)."
        ),
        source="write_parameters.py:weekend_rate_type",
    ),
]
