"""Тесты разбора сообщений IEC 62056-21 режим C (без сети)."""

import pytest

from mmws_gateway.errors import CrcError, GatewayError
from mmws_gateway.protocols.mode_c import (
    ETX,
    STX,
    build_identification_request,
    build_option_select,
    compute_bcc,
    obis6_to_short_code,
    parse_data_readout,
    parse_identification,
)


def test_build_identification_request():
    assert build_identification_request("202006003607") == b"/?202006003607!\r\n"


def test_parse_identification():
    ident = parse_identification(b"/RSN5202006003607\r\n")
    assert ident.manufacturer == "RSN"
    assert ident.baud_char == "5"
    assert ident.identification == "202006003607"


def test_build_option_select():
    assert build_option_select("5", mode="0") == b"\x06050\r\n"
    assert build_option_select("5", mode="2") == b"\x06250\r\n"


def test_parse_data_readout_round_trip():
    lines = "1.8.0(001234.567*kWh)\r\n32.7.0(230.1*V)\r\n!\r\n"
    body = lines.encode("ascii") + bytes([ETX])
    bcc = compute_bcc(body)
    readout = parse_data_readout(bytes([STX]), body, bytes([bcc]))
    assert readout.values["1.8.0"] == ("001234.567", "kWh")
    assert readout.values["32.7.0"] == ("230.1", "V")


def test_parse_data_readout_rejects_bad_bcc():
    lines = "1.8.0(001234.567*kWh)\r\n!\r\n"
    body = lines.encode("ascii") + bytes([ETX])
    with pytest.raises(CrcError):
        parse_data_readout(bytes([STX]), body, bytes([0x00]))


def test_parse_data_readout_requires_stx():
    body = b"1.8.0(1*kWh)\r\n!\r\n" + bytes([ETX])
    with pytest.raises(GatewayError):
        parse_data_readout(b"\x01", body, bytes([compute_bcc(body)]))


def test_obis6_to_short_code():
    assert obis6_to_short_code("1.1.1.8.0.ff") == "1.8.0"


def test_obis6_to_short_code_hex_field():
    # C-поле "1f" (hex) = 31 (десятичное) — короткий код режима C десятичный.
    assert obis6_to_short_code("1.1.1f.7.ff.ff") == "31.7.255"


def test_obis6_to_short_code_rejects_wrong_arity():
    with pytest.raises(GatewayError):
        obis6_to_short_code("1.8.0")
