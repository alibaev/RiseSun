"""Тесты правила определения физического адреса (ТЗ п. 4.3.2)."""

import pytest

from mmws_gateway.addressing import HDLC_DLMS, MODE_C, MODE_E, physical_address


def test_mode_c_uses_full_serial():
    assert physical_address("202006003607", MODE_C) == "202006003607"


def test_mode_e_uses_last_5_digits():
    assert physical_address("202006003607", MODE_E) == "03607"


def test_hdlc_dlms_uses_last_5_digits():
    assert physical_address("202006003607", HDLC_DLMS) == "03607"


def test_short_serial_returns_whole_serial_for_last5():
    assert physical_address("42", MODE_E) == "42"


def test_factory_batch_uses_last_4_digits_plus_199():
    # Подтверждено заводом 2026-09-08 (см. DECISIONS.md, «физический
    # адрес вне 14 бит») для партии 201901230001..201901230186 —
    # последние 5 цифр серийного (30001) переполняли 14-битное поле,
    # у этой партии реальный физический адрес другой.
    assert physical_address("201901230001", HDLC_DLMS) == "00200"
    assert physical_address("201901230001", MODE_E) == "00200"


def test_factory_batch_upper_bound():
    assert physical_address("201901230186", HDLC_DLMS) == "00385"


def test_factory_batch_range_boundaries_excluded():
    # Соседи диапазона — уже обычное правило "последние 5 цифр".
    assert physical_address("201901230000", HDLC_DLMS) == "30000"
    assert physical_address("201901230187", HDLC_DLMS) == "30187"


def test_factory_batch_rule_not_applied_to_mode_c():
    # Режим C — серийный номер целиком, спецправило партии его не касается.
    assert physical_address("201901230001", MODE_C) == "201901230001"


def test_unknown_profile_rejected():
    with pytest.raises(ValueError):
        physical_address("202006003607", "unknown_profile")


def test_empty_serial_rejected():
    with pytest.raises(ValueError):
        physical_address("", MODE_C)
