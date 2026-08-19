"""Интеграционные тесты профиля нагрузки (Этап 3, ТЗ п.4.2.3) против
программного эмулятора — SNRM/UA + AARQ/AARE + GET с выборкой по датам,
блочная передача (GET.response-with-datablock + GET.request-Next) и
инкрементальная сборка строк в hdlc_dlms.read_load_profile.

Стандартный DLMS-адрес профиля нагрузки — decimal-нотация 1-0:99.1.0.255
(IEC 62056-6-2, "Load Profile 1", class 7) — рабочая гипотеза, а не
значение из словаря OBIS.xlsx (там для профиля нагрузки нет ни одной
записи) — см. DECISIONS.md, решение согласовано с пользователем
2026-08-19. В проекте OBIS-коды пишутся в HEX-нотации (см. правило
конвертации decimal->hex по полям, DECISIONS.md, «Этап 2, итерация 3»),
поэтому decimal C=99 -> "63", F=255 -> "ff": итоговая строка
"1.0.63.1.0.ff".
"""

from datetime import datetime

import pytest

from mmws_gateway.emulators.common import ConnectionCounter, ErrorInjection, ThreadedEmulatorServer
from mmws_gateway.emulators.hdlc_dlms_emulator import make_hdlc_dlms_handler
from mmws_gateway.protocols import datatypes, dlms, hdlc_dlms
from mmws_gateway.transport import TcpTransport, TransportConfig

SERIAL = "202006003607"
PASSWORD = b"12345678"
LOAD_PROFILE_OBIS = "1.0.63.1.0.ff"
LOAD_PROFILE_CLASS_ID = 7
FROM_DT = datetime(2026, 8, 1)
TO_DT = datetime(2026, 8, 19)


def _row(dt: datetime, value: int) -> list[bytes]:
    timestamp = datatypes.encode_octet_string(datatypes.encode_cosem_date_time(dt))
    return [timestamp, datatypes.encode_double_long_unsigned(value)]


def _start_server(rows: list[list[bytes]], *, block_size: int) -> ThreadedEmulatorServer:
    counter = ConnectionCounter()
    handler = make_hdlc_dlms_handler(
        password=PASSWORD,
        obis_values={},
        error_injection=ErrorInjection(),
        counter=counter,
        load_profile_obis=dlms.parse_obis(LOAD_PROFILE_OBIS),
        load_profile_rows=rows,
        load_profile_block_size=block_size,
    )
    return ThreadedEmulatorServer(handler)


def _read_all(server: ThreadedEmulatorServer) -> list:
    config = TransportConfig(host=server.host, port=server.port, timeout_ms=1000, max_retries=1)
    with TcpTransport(config) as transport:
        rows = list(
            hdlc_dlms.read_load_profile(
                transport,
                serial=SERIAL,
                password=PASSWORD,
                obis=LOAD_PROFILE_OBIS,
                class_id=LOAD_PROFILE_CLASS_ID,
                from_dt=FROM_DT,
                to_dt=TO_DT,
            )
        )
    return rows


def test_single_datablock_round_trip():
    """Все строки умещаются в один датаблок (большой block_size) — всё
    равно проходят через код датаблочной ветки (эмулятор Этапа 3 не
    отдаёт GET.response-Normal для профиля нагрузки, см. docstring
    ``_serve_load_profile``)."""
    rows = [_row(datetime(2026, 8, 1, h), 1000 + h) for h in range(3)]
    with _start_server(rows, block_size=4096) as server:
        decoded = _read_all(server)

    assert len(decoded) == 3
    for i, (timestamp, value) in enumerate(decoded):
        assert datatypes.decode_cosem_date_time(timestamp) == datetime(2026, 8, 1, i)
        assert value == 1000 + i


def test_multi_datablock_round_trip_forces_block_transfer():
    """Маленький block_size гарантированно рвёт закодированный массив
    строк на несколько датаблоков — проверяет склейку через
    GET.request-Next и инкрементальное декодирование строк по мере
    накопления байт (см. hdlc_dlms.read_load_profile)."""
    rows = [_row(datetime(2026, 8, 1, h), 2000 + h) for h in range(10)]
    encoded_len = len(
        datatypes.encode_array([datatypes.encode_structure(r) for r in rows])
    )
    block_size = 12  # заведомо меньше одной строки — несколько датаблоков на строку
    assert encoded_len > block_size * 3  # сверяем, что тест действительно форсирует блочную передачу

    with _start_server(rows, block_size=block_size) as server:
        decoded = _read_all(server)

    assert len(decoded) == 10
    for i, (timestamp, value) in enumerate(decoded):
        assert datatypes.decode_cosem_date_time(timestamp) == datetime(2026, 8, 1, i)
        assert value == 2000 + i


def test_empty_load_profile_returns_no_rows():
    with _start_server([], block_size=4096) as server:
        decoded = _read_all(server)
    assert decoded == []
