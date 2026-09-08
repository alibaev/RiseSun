"""Правило определения физического адреса счётчика (ТЗ п. 4.3.2).

Для протокольных профилей IEC 62056-21 режим E и HDLC-DLMS физический
адрес — последние 5 цифр серийного номера счётчика; для режима C —
серийный номер целиком.
"""

from __future__ import annotations

MODE_C = "mode_c"
MODE_E = "mode_e"
HDLC_DLMS = "hdlc_dlms"

PROFILES = (MODE_C, MODE_E, HDLC_DLMS)

# Завод сообщил (2026-09-08, пользователь) отдельное правило для партии
# счётчиков с серийными номерами 201901230001..201901230186 (те самые
# 22, что валили чтение AddressingError'ом — см. DECISIONS.md,
# «физический адрес вне 14 бит»): у ЭТОЙ партии физический адрес —
# последние 4 цифры серийного + 199, а не последние 5 цифр целиком.
# Пример от завода: 201901230001 -> 00001 + 199 = 00200. Устраняет
# переполнение 14-битного поля (последние 5 цифр давали значения вроде
# 30001, у этой формулы результат — 200..385, комфортно в диапазоне.
_FACTORY_BATCH_LOW = 201901230001
_FACTORY_BATCH_HIGH = 201901230186
_FACTORY_BATCH_OFFSET = 199


def physical_address(serial: str, profile: str) -> str:
    """Возвращает физический адрес счётчика для заданного протокольного профиля."""
    if profile not in PROFILES:
        raise ValueError(
            f"Неизвестный протокольный профиль: {profile!r}, ожидается один из {PROFILES}"
        )
    if not serial:
        raise ValueError("Серийный номер счётчика не может быть пустым")
    if profile == MODE_C:
        return serial
    if serial.isdigit() and _FACTORY_BATCH_LOW <= int(serial) <= _FACTORY_BATCH_HIGH:
        return f"{int(serial[-4:]) + _FACTORY_BATCH_OFFSET:05d}"
    return serial[-5:]
