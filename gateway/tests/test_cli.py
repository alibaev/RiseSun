"""Дымовой тест CLI ``gateway read`` через emulator hdlc_dlms."""

import pytest

from mmws_gateway.cli import main
from mmws_gateway.emulators.common import ConnectionCounter, ErrorInjection, ThreadedEmulatorServer
from mmws_gateway.emulators.hdlc_dlms_emulator import make_hdlc_dlms_handler
from mmws_gateway.protocols import dlms

SERIAL = "202006003607"
PASSWORD = "12345678"
# Родовой OBIS для теста — намеренно НЕ "1.1.1.8.0.ff" (суммарная
# активная энергия), у которого с 2026-08-20 есть вендорский override
# value-OBIS (dlms.VALUE_OBIS_OVERRIDES, см. test_integration_hdlc_dlms.py).
OBIS = "1.1.1.7.0.ff"
OBIS_VALUES = {dlms.parse_obis(OBIS): 1234567}


def test_cli_read_hdlc_dlms_happy_path(capsys, monkeypatch):
    counter = ConnectionCounter()
    handler = make_hdlc_dlms_handler(
        password=PASSWORD.encode("ascii"),
        obis_values=OBIS_VALUES,
        error_injection=ErrorInjection(),
        counter=counter,
    )
    with ThreadedEmulatorServer(handler) as server:
        monkeypatch.delenv("MMWS_METER_PASSWORD", raising=False)
        exit_code = main(
            [
                "read",
                "--profile",
                "hdlc_dlms",
                "--host",
                server.host,
                "--port",
                str(server.port),
                "--serial",
                SERIAL,
                "--obis",
                OBIS,
                "--password",
                PASSWORD,
                "--timeout-ms",
                "1000",
                "--retries",
                "1",
            ]
        )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "1234567" in out


def test_cli_read_requires_password_for_hdlc_dlms(monkeypatch):
    monkeypatch.delenv("MMWS_METER_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        main(
            [
                "read",
                "--profile",
                "hdlc_dlms",
                "--host",
                "127.0.0.1",
                "--port",
                "1",
                "--serial",
                SERIAL,
                "--obis",
                OBIS,
            ]
        )
