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
    return serial[-5:]
