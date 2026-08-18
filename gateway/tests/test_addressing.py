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


def test_unknown_profile_rejected():
    with pytest.raises(ValueError):
        physical_address("202006003607", "unknown_profile")


def test_empty_serial_rejected():
    with pytest.raises(ValueError):
        physical_address("", MODE_C)
