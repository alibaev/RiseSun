"""Реестр одиночных записываемых параметров (Этап 2, ТЗ п.4.2.4).

Источник — словарь OBIS.xlsx, лист RW_Tree_параметры: раздел
«Parameter» (запись) содержит ровно пять объектов класса 1 (Data) —
Current Settlement No, Current Available Settlement No, Current Time,
Current Date, Current Week. Дата/время реализованы отдельно
(job_worker._run_write_datetime — составная операция из двух OBIS без
пользовательского ввода, синхронизация с системным временем). Здесь —
одиночные параметры с пользовательским значением; остальные категории
ТЗ п.4.2.4 (режимы отображения, тарифное расписание, профиль нагрузки,
GPRS) в словаре OBIS не представлены — см. DECISIONS.md, не
реализуются до появления точных OBIS-кодов/структур."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WritableIntParameter:
    obis: str
    class_id: int
    # "unsigned" — 1 байт (0-255), достаточно для номера расчётного
    # периода (по аналогии с «Tariff 1..12» в других частях словаря —
    # диапазон заведомо укладывается в один байт); не подтверждено на
    # реальном оборудовании, как и кодировка даты/времени.
    value_type: str = "unsigned"


WRITABLE_INT_PARAMETERS: dict[str, WritableIntParameter] = {
    "settlement_no": WritableIntParameter(obis="1.0.0.1.0.ff", class_id=1),
    "available_settlement_no": WritableIntParameter(obis="1.0.0.1.1.ff", class_id=1),
}
