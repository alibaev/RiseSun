"""Тесты серверного (call-home) транспорта: адресация, фильтрация
DL/T645-шума, скользящее окно с вытеснением, чтение через пул с
повторными попытками SNRM (модель поведения, подтверждённая на
реальном оборудовании 2026-08-18 — см. DECISIONS.md).
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from mmws_gateway.callhome import (
    CallHomePool,
    DlT645FilteringSocket,
    read_via_call_home,
    serial_from_dlt645_address,
)
from mmws_gateway.protocols import dlms
from mmws_gateway.protocols.hdlc import CONTROL_SNRM, CONTROL_UA, HdlcFrame


def test_serial_from_dlt645_address_matches_real_captures():
    # Подтверждено на обоих реальных счётчиках Risesun, 2026-08-18.
    assert serial_from_dlt645_address(bytes.fromhex("522300012020")) == "202001002352"
    assert serial_from_dlt645_address(bytes.fromhex("134100062320")) == "202306004113"


def test_serial_from_dlt645_address_rejects_wrong_length():
    with pytest.raises(ValueError):
        serial_from_dlt645_address(bytes.fromhex("5223000120"))


def _build_dummy_dlt645_frame(addr6: bytes, data_len: int = 14) -> bytes:
    # Контрольная сумма/содержимое данных для целей теста не важны —
    # DlT645FilteringSocket/_identify их не проверяют, только длину.
    return bytes([0x68]) + addr6 + bytes([0x68, 0x15, data_len]) + bytes(data_len) + bytes([0x00, 0x16])


def test_filtering_socket_skips_dlt645_frame_and_zero_bytes():
    server_sock, client_sock = socket.socketpair()
    try:
        noise = _build_dummy_dlt645_frame(bytes.fromhex("522300012020"))
        server_sock.sendall(b"\x00\x00\x00" + noise + b"\x00" + b"\x7eHELLO\x7e")
        server_sock.close()

        filtering = DlT645FilteringSocket(client_sock)
        filtering.settimeout(2)
        collected = bytearray()
        while len(collected) < len(b"\x7eHELLO\x7e"):
            chunk = filtering.recv(64)
            if not chunk:
                break
            collected += chunk
        assert bytes(collected) == b"\x7eHELLO\x7e"
    finally:
        client_sock.close()


def test_pool_sliding_window_evicts_oldest():
    pool = CallHomePool(bind_host="127.0.0.1", bind_port=0, window_size=2)
    pool.start()
    try:
        c1 = socket.create_connection(("127.0.0.1", pool.bind_port), timeout=3)
        time.sleep(0.1)
        c2 = socket.create_connection(("127.0.0.1", pool.bind_port), timeout=3)
        time.sleep(0.1)
        assert pool.pending_count() == 2

        c1.settimeout(2)
        c3 = socket.create_connection(("127.0.0.1", pool.bind_port), timeout=3)
        time.sleep(0.2)

        # окно = 2 -> c1 (самое старое) должно быть вытеснено при подключении c3
        assert pool.pending_count() == 2
        with pytest.raises((ConnectionResetError, OSError, socket.timeout)):
            data = c1.recv(1)
            if data == b"":
                raise ConnectionResetError("closed")
        for s in (c1, c2, c3):
            try:
                s.close()
            except OSError:
                pass
    finally:
        pool.stop()


def test_list_seen_serials_persists_after_sliding_window_eviction():
    """Этап 6 (обнаружение новых счётчиков) — список опознанных
    серийников не должен теряться при вытеснении held-соединения из
    скользящего окна (в отличие от самого соединения)."""
    pool = CallHomePool(bind_host="127.0.0.1", bind_port=0, window_size=1)
    pool.start()
    try:
        assert pool.list_seen_serials() == {}

        addr6 = bytes.fromhex("522300012020")  # -> 202001002352 (реальный, подтверждённый адрес)
        c1 = socket.create_connection(("127.0.0.1", pool.bind_port), timeout=3)
        c1.sendall(_build_dummy_dlt645_frame(addr6))
        time.sleep(0.3)  # даём _identify() время обработать анонс-кадр

        c2 = socket.create_connection(("127.0.0.1", pool.bind_port), timeout=3)  # окно=1 -> вытесняет c1
        time.sleep(0.2)
        assert pool.pending_count() == 1  # c1 вытеснено

        seen = pool.list_seen_serials()
        assert "202001002352" in seen
        assert abs(seen["202001002352"] - time.time()) < 5

        for s in (c1, c2):
            try:
                s.close()
            except OSError:
                pass
    finally:
        pool.stop()


def _run_fake_meter(conn: socket.socket, *, addr6: bytes, password: bytes, obis_values: dict, ignore_first_n_snrm: int) -> None:
    """Имитирует реальное поведение счётчика: сначала шлёт DL/T645-анонс,
    затем игнорирует первые ``ignore_first_n_snrm`` попыток SNRM (не
    отвечает вовсе — так вело себя реальное оборудование), и только на
    следующей попытке отвечает полноценным HDLC/DLMS-обменом. Читает
    кадры строго по одному (через ``_read_frame`` — то же чтение по
    длине, что и в постоянном коде), а не разрезанием произвольного
    bulk-recv() буфера — так исключены баги смещения границ кадров в
    самом тесте, а не в проверяемом коде."""
    from mmws_gateway.emulators.hdlc_dlms_emulator import _read_frame

    conn.sendall(_build_dummy_dlt645_frame(addr6))

    seen = 0
    while True:
        frame = HdlcFrame.decode(_read_frame(conn))
        if frame.control != CONTROL_SNRM:
            return
        seen += 1
        if seen <= ignore_first_n_snrm:
            continue  # намеренно не отвечаем на эту попытку
        ua = HdlcFrame(destination=frame.source, source=frame.destination, control=CONTROL_UA)
        conn.sendall(ua.encode())
        _serve_rest_of_session(conn, password=password, obis_values=obis_values)
        return


def _serve_rest_of_session(conn: socket.socket, *, password: bytes, obis_values: dict) -> None:
    # serve_hdlc_dlms_session сама читает SNRM первым действием — тут он
    # уже обработан выше, поэтому реализуем "хвост" (AARQ/AARE/GET)
    # напрямую, как в самой функции.
    from mmws_gateway.emulators.hdlc_dlms_emulator import _read_frame, OBJECT_UNDEFINED
    from mmws_gateway.protocols.hdlc import control_information_frame

    aarq_frame = HdlcFrame.decode(_read_frame(conn))
    parsed_aarq = dlms.parse_aarq(dlms.unwrap_llc(aarq_frame.information))
    accepted = parsed_aarq.password == password
    aare = dlms.build_aare(accepted=accepted)
    aare_frame = HdlcFrame(
        destination=aarq_frame.source, source=aarq_frame.destination,
        control=control_information_frame(0, 1), information=dlms.wrap_llc_response(aare),
    )
    conn.sendall(aare_frame.encode())
    if not accepted:
        return

    get_frame = HdlcFrame.decode(_read_frame(conn))
    get_request = dlms.parse_get_request(dlms.unwrap_llc(get_frame.information))
    value = obis_values.get(get_request.obis)
    from mmws_gateway.protocols import datatypes
    if value is None:
        info = dlms.build_get_response_error(get_request.invoke_id, OBJECT_UNDEFINED)
    else:
        info = dlms.build_get_response_data(get_request.invoke_id, datatypes.encode_double_long_unsigned(value))
    response_frame = HdlcFrame(
        destination=get_frame.source, source=get_frame.destination,
        control=control_information_frame(1, 2), information=dlms.wrap_llc_response(info),
    )
    conn.sendall(response_frame.encode())


def test_read_via_call_home_succeeds_after_ignored_snrm_attempts():
    """Регрессия модели реального поведения: первая попытка SNRM ответа
    не получает — только повторная. read_via_call_home должен пережить
    это за счёт ретраев внутри пула."""
    serial = "202001002352"
    addr6 = bytes.fromhex("522300012020")
    password = b"12345678"
    obis = "1.1.1.8.0.ff"
    obis_values = {dlms.parse_obis(obis): 1234567}

    pool = CallHomePool(bind_host="127.0.0.1", bind_port=0, window_size=10)
    pool.start()
    try:
        # Без timeout на сокете "счётчика" — иначе он гоняется наперегонки
        # с per_attempt_timeout_ms сервера и может закрыться раньше, чем
        # придёт повторная попытка SNRM (ложный сбой теста, не бага).
        client_conn = socket.create_connection(("127.0.0.1", pool.bind_port))
        meter_thread = threading.Thread(
            target=_run_fake_meter,
            kwargs=dict(conn=client_conn, addr6=addr6, password=password, obis_values=obis_values, ignore_first_n_snrm=2),
            daemon=True,
        )
        meter_thread.start()

        # Таймауты не слишком агрессивные — иначе гонка с обработкой в
        # потоке fake-метра может привести к тому, что сервер отправит
        # СЛЕДУЮЩУЮ попытку SNRM раньше, чем метр успеет ответить на
        # предыдущую (в реальности между попытками были секунды, не
        # миллисекунды).
        # max_attempts_per_connection передан явно: боевой дефолт теперь
        # 1 (см. callhome.py — повтор SNRM на том же соединении при
        # большом per-attempt таймауте создавал рассинхронизацию с
        # реальным оборудованием), но этот тест намеренно проверяет
        # именно поведение "повтор на одном и том же соединении".
        value = read_via_call_home(
            pool, serial=serial, password=password, obis=obis,
            retry_interval_s=0.5, max_wait_s=15, per_attempt_timeout_ms=1500,
            max_attempts_per_connection=5,
        )
        assert value == 1234567
        meter_thread.join(timeout=3)
    finally:
        pool.stop()


def test_read_via_call_home_raises_if_meter_never_connected():
    pool = CallHomePool(bind_host="127.0.0.1", bind_port=0, window_size=10)
    pool.start()
    try:
        with pytest.raises(Exception):
            read_via_call_home(
                pool, serial="000000000000", password=b"12345678", obis="1.1.1.8.0.ff",
                retry_interval_s=0.2, max_wait_s=1,
            )
    finally:
        pool.stop()
