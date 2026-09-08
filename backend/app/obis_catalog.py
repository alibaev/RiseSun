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

``1.1.60.50.0.ff`` (``dlms.VALUE_OBIS_OVERRIDES``) раньше был
сознательно исключён как "внутренняя протокольная деталь, не для
прямого опроса" — пересмотрено 2026-09-08: пользователь предоставил
``obis1.xlsx`` (18 пунктов с короткой десятичной нотацией OBIS,
экспорт из заводского словаря), где «Total Active Energy\\Total»
(96.80.0) идёт ОТДЕЛЬНЫМ пунктом верхнего уровня, наравне с «Import
Active Energy\\Total» (1.8.0, = уже подтверждённый ``1.1.1.8.0.ff``)
— то есть счётчик поддерживает оба объекта как самостоятельные, оба
имеет смысл опрашивать напрямую. Правило перевода короткой формы
``C.D.E`` (десятичное) в полный OBIS — ``1.1.<hex(C)>.<hex(D)>.<hex(E)>.ff``
(то же правило, что и для ``1.0.*`` параметров записи, см.
``write_parameters.py``, только префикс ``1.1`` вместо ``1.0`` — это
класс Register/Data для показаний, а не параметров) — подтверждено на
2 из 18 строк файла точным совпадением с уже проверенными реальным
трафиком кодами (1.8.0 -> 1.1.1.8.0.ff и 96.80.0 -> 1.1.60.50.0.ff),
остальные 16 из того же файла тем же правилом НЕ проверены живым
трафиком по отдельности.
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
    # 010-026: из obis1.xlsx (пользователь, 2026-09-08) — см. docstring
    # модуля про правило перевода короткой формы C.D.E (десятичное) в
    # 1.1.<hex(C)>.<hex(D)>.<hex(E)>.ff.
    ObisEntry(
        number="010",
        obis="1.1.60.50.0.ff",
        label="Суммарная активная энергия, всего",
        description=(
            "Total Active Energy\\Total (96.80.0 в obis1.xlsx) — отдельный от 1.1.1.8.0.ff объект "
            "с тем же физическим смыслом (см. VALUE_OBIS_OVERRIDES в dlms.py). Подтверждено "
            "реальным трафиком 2026-08-18 как value-цель для 1.1.1.8.0.ff на этой модели счётчика."
        ),
        source="obis1.xlsx (пользователь) + dlms.py:VALUE_OBIS_OVERRIDES",
    ),
    ObisEntry(
        number="011",
        obis="1.1.60.50.1.ff",
        label="Суммарная активная энергия, тариф 1",
        description="Total Active Energy\\T1 (96.80.1 в obis1.xlsx) — не проверено живым трафиком отдельно.",
        source="obis1.xlsx (пользователь, 2026-09-08)",
    ),
    ObisEntry(
        number="012",
        obis="1.1.60.50.2.ff",
        label="Суммарная активная энергия, тариф 2",
        description="Total Active Energy\\T2 (96.80.2 в obis1.xlsx) — не проверено живым трафиком отдельно.",
        source="obis1.xlsx (пользователь, 2026-09-08)",
    ),
    ObisEntry(
        number="013",
        obis="1.1.60.50.3.ff",
        label="Суммарная активная энергия, тариф 3",
        description="Total Active Energy\\T3 (96.80.3 в obis1.xlsx) — не проверено живым трафиком отдельно.",
        source="obis1.xlsx (пользователь, 2026-09-08)",
    ),
    ObisEntry(
        number="014",
        obis="1.1.20.7.0.ff",
        label="Напряжение, фаза A",
        description="Instantaneous Voltage\\Phase A (32.7.0 в obis1.xlsx) — не проверено живым трафиком.",
        source="obis1.xlsx (пользователь, 2026-09-08)",
    ),
    ObisEntry(
        number="015",
        obis="1.1.34.7.0.ff",
        label="Напряжение, фаза B",
        description="Instantaneous Voltage\\Phase B (52.7.0 в obis1.xlsx) — не проверено живым трафиком.",
        source="obis1.xlsx (пользователь, 2026-09-08)",
    ),
    ObisEntry(
        number="016",
        obis="1.1.48.7.0.ff",
        label="Напряжение, фаза C",
        description="Instantaneous Voltage\\Phase C (72.7.0 в obis1.xlsx) — не проверено живым трафиком.",
        source="obis1.xlsx (пользователь, 2026-09-08)",
    ),
    ObisEntry(
        number="017",
        obis="1.1.1f.7.0.ff",
        label="Ток, фаза A",
        description="Instantaneous Current\\Phase A (31.7.0 в obis1.xlsx) — не проверено живым трафиком.",
        source="obis1.xlsx (пользователь, 2026-09-08)",
    ),
    ObisEntry(
        number="018",
        obis="1.1.33.7.0.ff",
        label="Ток, фаза B",
        description="Instantaneous Current\\Phase B (51.7.0 в obis1.xlsx) — не проверено живым трафиком.",
        source="obis1.xlsx (пользователь, 2026-09-08)",
    ),
    ObisEntry(
        number="019",
        obis="1.1.47.7.0.ff",
        label="Ток, фаза C",
        description="Instantaneous Current\\Phase C (71.7.0 в obis1.xlsx) — не проверено живым трафиком.",
        source="obis1.xlsx (пользователь, 2026-09-08)",
    ),
    ObisEntry(
        number="020",
        obis="1.1.1.8.1.ff",
        label="Активная энергия, приём, тариф 1",
        description="Import Active Energy\\T1 (1.8.1 в obis1.xlsx) — не проверено живым трафиком отдельно.",
        source="obis1.xlsx (пользователь, 2026-09-08)",
    ),
    ObisEntry(
        number="021",
        obis="1.1.1.8.2.ff",
        label="Активная энергия, приём, тариф 2",
        description="Import Active Energy\\T2 (1.8.2 в obis1.xlsx) — не проверено живым трафиком отдельно.",
        source="obis1.xlsx (пользователь, 2026-09-08)",
    ),
    ObisEntry(
        number="022",
        obis="1.1.1.8.3.ff",
        label="Активная энергия, приём, тариф 3",
        description="Import Active Energy\\T3 (1.8.3 в obis1.xlsx) — не проверено живым трафиком отдельно.",
        source="obis1.xlsx (пользователь, 2026-09-08)",
    ),
    ObisEntry(
        number="023",
        obis="1.1.2.8.0.ff",
        label="Активная энергия, отдача, всего",
        description="Export Active Energy\\Total (2.8.0 в obis1.xlsx) — не проверено живым трафиком.",
        source="obis1.xlsx (пользователь, 2026-09-08)",
    ),
    ObisEntry(
        number="024",
        obis="1.1.2.8.1.ff",
        label="Активная энергия, отдача, тариф 1",
        description="Export Active Energy\\T1 (2.8.1 в obis1.xlsx) — не проверено живым трафиком.",
        source="obis1.xlsx (пользователь, 2026-09-08)",
    ),
    ObisEntry(
        number="025",
        obis="1.1.2.8.2.ff",
        label="Активная энергия, отдача, тариф 2",
        description="Export Active Energy\\T2 (2.8.2 в obis1.xlsx) — не проверено живым трафиком.",
        source="obis1.xlsx (пользователь, 2026-09-08)",
    ),
    ObisEntry(
        number="026",
        obis="1.1.2.8.3.ff",
        label="Активная энергия, отдача, тариф 3",
        description="Export Active Energy\\T3 (2.8.3 в obis1.xlsx) — не проверено живым трафиком.",
        source="obis1.xlsx (пользователь, 2026-09-08)",
    ),
]
