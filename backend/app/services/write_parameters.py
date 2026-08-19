"""Реестр одиночных записываемых параметров (Этап 2, ТЗ п.4.2.4).

Источник — словарь OBIS.xlsx. Лист RW_Tree_параметры (раздел
«Parameter») даёт напрямую в нужной 6-полевой hex-нотации только пять
объектов класса 1 (Data) — Current Settlement No, Current Available
Settlement No, Current Time, Current Date, Current Week. Дата/время
реализованы отдельно (job_worker._run_write_datetime — составная
операция из двух OBIS без пользовательского ввода, синхронизация с
системным временем); Settlement No/Available Settlement No — здесь.

Остальные три категории п.4.2.4 (режимы отображения, тарифное
расписание, параметры профиля нагрузки) НЕ представлены в
RW_Tree_параметры явно, но найдены в листе Read_Tree_полное_дерево
(там короткая ДЕСЯТИЧНАЯ нотация OBIS, не hex, и не в 6-полевом виде).
Правило перевода короткой нотации в нужный протоколу 6-полевой hex
выведено и подтверждено сверкой с уже проверенными объектами:
короткая форма листа Read_Tree — это поля C.D.E в ДЕСЯТИЧНОМ виде,
поля A.B всегда «1.0», поле F всегда wildcard «ff» — то есть
full_hex = "1.0." + hex(C) + "." + hex(D) + "." + hex(E) + ".ff".
Подтверждено на двух независимых примерах: короткая «0.9.1» (Time) ->
«1.0.0.9.1.ff» (уже проверенный код Current Time); и «96.80.0» (децим.)
-> «60.50.0» (hex) -> ровно вендорский OBIS энергии, подтверждённый на
реальном оборудовании 2026-08-18 (см. DECISIONS.md, код `1.1.60.50.0.ff`
там же использует другие A.B, но математика перевода дес.->hex та же).

GPRS-параметры по-прежнему не представлены нигде в словаре OBIS ни в
каком виде (проверено по ключевым словам gprs/apn/сервер/modem/network
по всем 7 листам) — не реализуются, см. DECISIONS.md."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WritableIntParameter:
    obis: str
    class_id: int
    # "unsigned" — 1 байт (0-255), достаточно для всех параметров этого
    # реестра (счётчики/интервалы/номера-селекторы, по аналогии с
    # «Tariff 1..12» в других частях словаря); не подтверждено на
    # реальном оборудовании, как и кодировка даты/времени.
    value_type: str = "unsigned"


WRITABLE_INT_PARAMETERS: dict[str, WritableIntParameter] = {
    "settlement_no": WritableIntParameter(obis="1.0.0.1.0.ff", class_id=1),
    "available_settlement_no": WritableIntParameter(obis="1.0.0.1.1.ff", class_id=1),
    # Read_Tree: «Load Profile Interval (Min)», дес. 0.8.4 -> hex 1.0.0.8.4.ff.
    # Единственная, однозначная запись в дереве (Basic Setting -> Intervals).
    "load_profile_interval": WritableIntParameter(obis="1.0.0.8.4.ff", class_id=1),
    # Read_Tree: «Total Number Of Display Mode», дес. 96.52.1 -> hex 1.0.60.34.1.ff.
    # Единственная, однозначная запись (Display -> Display Mode).
    "display_mode_count": WritableIntParameter(obis="1.0.60.34.1.ff", class_id=1),
    # Read_Tree: «Weekend Rate Type», дес. 96.54.8 -> hex 1.0.60.36.8.ff.
    # В словаре ДВЕ разных записи с этим именем (96.54.8 и 96.72.0) —
    # похоже на дублирование между вариантами моделей в исходной базе
    # (обычная картина для этого словаря, см. дубликаты «Current Season»
    # ниже). Выбрана первая/парная с «Week Structure» 96.54.5 — какая из
    # двух верна для конкретно наших счётчиков, не подтверждено.
    "weekend_rate_type": WritableIntParameter(obis="1.0.60.36.8.ff", class_id=1),
}
