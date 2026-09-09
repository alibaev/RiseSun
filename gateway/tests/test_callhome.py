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

from datetime import datetime, timedelta

from mmws_gateway.callhome import (
    CallHomePool,
    DlT645FilteringSocket,
    read_load_profile_via_call_home,
    read_via_call_home,
    serial_from_dlt645_address,
)
from mmws_gateway.protocols import datatypes, dlms
from mmws_gateway.protocols.hdlc import CONTROL_SNRM, CONTROL_UA, HdlcFrame, read_frame_from_transport
from mmws_gateway.transport import TcpServerTransport


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


def test_filtering_socket_deadline_is_absolute_not_reset_by_noise():
    """Регрессия найденного 2026-09-08 бага (см. DECISIONS.md,
    «эксперимент с ожиданием AARE 150с»): ``socket.settimeout()``
    ограничивает КАЖДЫЙ ``recv()`` по отдельности, а не операцию
    целиком — шумовой байт (например, keepalive-заглушка ``0x00``),
    прилетевший до истечения таймаута, незаметно обнулял отсчёт и
    продлевал реальное ожидание. ``set_deadline()`` должен держать
    единый абсолютный дедлайн независимо от того, сколько
    отфильтровываемых байт прилетает по пути."""
    server_sock, client_sock = socket.socketpair()
    try:
        def trickle() -> None:
            # 5 нулевых байт с паузами по 0.3с (t=0.3..1.5) — каждый
            # укладывается в старый per-recv таймаут по отдельности, но
            # суммарно должен быть отсечён единым дедлайном в 1.0с.
            for _ in range(5):
                time.sleep(0.3)
                try:
                    server_sock.sendall(b"\x00")
                except OSError:
                    return

        threading.Thread(target=trickle, daemon=True).start()

        filtering = DlT645FilteringSocket(client_sock)
        filtering.set_deadline(time.time() + 1.0)
        start = time.time()
        with pytest.raises(socket.timeout):
            filtering.recv(64)
        elapsed = time.time() - start
        # До фикса шум продлевал бы ожидание минимум до последнего
        # прилетевшего байта (t=1.5) плюс ещё один полный таймаут сверху
        # (~2.5с); с фиксом должно уложиться в дедлайн (~1.0с).
        assert elapsed < 1.5
    finally:
        client_sock.close()
        server_sock.close()


def test_read_frame_from_transport_filters_dlt645_noise_between_frames():
    """Найденный баг (2026-09-07, сообщение пользователя — при чтении
    профиля нагрузки счётчик "передаёт что-то нечитаемое, без чёткого
    начала и конца"): раньше фильтрация DL/T645-шума включалась заново
    только вручную перед SNRM, поэтому шум между ВТОРЫМ и последующими
    кадрами уже установленной сессии не отфильтровывался. Этот тест
    читает через один и тот же ``TcpServerTransport`` ДВА кадра подряд
    без единого явного вызова ``reset_seeking()`` — фильтрация должна
    сработать автоматически перед вторым кадром так же, как перед
    первым (через ``transport.reset_frame_seeking()``, см. hdlc.py)."""
    server_sock, client_sock = socket.socketpair()
    try:
        frame1 = HdlcFrame(destination=1, source=16, control=0x93).encode()
        frame2 = HdlcFrame(destination=1, source=16, control=0x53).encode()
        noise = _build_dummy_dlt645_frame(bytes.fromhex("522300012020"))
        server_sock.sendall(frame1 + b"\x00" + noise + frame2)
        server_sock.close()

        filtering = DlT645FilteringSocket(client_sock)
        transport = TcpServerTransport.from_accepted_socket(
            filtering, peer_host="127.0.0.1", peer_port=1, timeout_ms=2000
        )
        got1 = HdlcFrame.decode(read_frame_from_transport(transport))
        got2 = HdlcFrame.decode(read_frame_from_transport(transport))
        assert got1.control == 0x93
        assert got2.control == 0x53
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


def test_pool_per_serial_limit_evicts_oldest_same_serial():
    """2026-09-08 (по просьбе пользователя) — лимит на ОДИН счётчик,
    независимый от глобального окна: если один и тот же серийник
    занимает больше ``max_per_serial`` held-соединений, вытесняется
    самое старое ИЗ ЕГО ЖЕ соединений (а не глобально самое старое,
    которое могло бы принадлежать другому счётчику)."""
    pool = CallHomePool(bind_host="127.0.0.1", bind_port=0, window_size=10, max_per_serial=2)
    pool.start()
    try:
        addr6 = bytes.fromhex("522300012020")  # -> 202001002352
        c1 = socket.create_connection(("127.0.0.1", pool.bind_port), timeout=3)
        c1.sendall(_build_dummy_dlt645_frame(addr6))
        time.sleep(0.2)
        c2 = socket.create_connection(("127.0.0.1", pool.bind_port), timeout=3)
        c2.sendall(_build_dummy_dlt645_frame(addr6))
        time.sleep(0.2)
        assert pool.pending_count("202001002352") == 2

        c1.settimeout(2)
        c3 = socket.create_connection(("127.0.0.1", pool.bind_port), timeout=3)
        c3.sendall(_build_dummy_dlt645_frame(addr6))
        time.sleep(0.2)

        # лимит на счётчик = 2 -> c1 (самое старое ИЗ ЭТОГО серийника) вытеснено
        assert pool.pending_count("202001002352") == 2
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


def test_pool_per_serial_limit_does_not_affect_other_serials():
    """Лимит считается ОТДЕЛЬНО по каждому серийнику — held-соединения
    ДРУГОГО счётчика не вытесняются, даже если первый уже упёрся в свой
    лимит."""
    pool = CallHomePool(bind_host="127.0.0.1", bind_port=0, window_size=10, max_per_serial=1)
    pool.start()
    try:
        addr6_a = bytes.fromhex("522300012020")  # -> 202001002352
        addr6_b = bytes.fromhex("134100062320")  # -> 202306004113
        c1 = socket.create_connection(("127.0.0.1", pool.bind_port), timeout=3)
        c1.sendall(_build_dummy_dlt645_frame(addr6_a))
        time.sleep(0.2)
        c2 = socket.create_connection(("127.0.0.1", pool.bind_port), timeout=3)
        c2.sendall(_build_dummy_dlt645_frame(addr6_b))
        time.sleep(0.2)

        assert pool.pending_count("202001002352") == 1
        assert pool.pending_count("202306004113") == 1
        for s in (c1, c2):
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

    from mmws_gateway.protocols import datatypes

    # read_register_via_established_link (2026-09-07: порядок приведён в
    # соответствие с реальным трафиком легитимного заводского клиента)
    # сначала запрашивает scaler_unit (атрибут 3), затем value (атрибут 2)
    # — отвечаем scaler=0 (не меняет ожидаемое сырое значение в этом тесте).
    scaler_frame = HdlcFrame.decode(_read_frame(conn))
    scaler_request = dlms.parse_get_request(dlms.unwrap_llc(scaler_frame.information))
    scaler_value = datatypes.encode_structure(
        [datatypes.encode_integer(0), datatypes.encode_unsigned(0)]
    )
    scaler_info = dlms.build_get_response_data(scaler_request.invoke_id, scaler_value)
    scaler_response_frame = HdlcFrame(
        destination=scaler_frame.source, source=scaler_frame.destination,
        control=control_information_frame(1, 2), information=dlms.wrap_llc_response(scaler_info),
    )
    conn.sendall(scaler_response_frame.encode())

    get_frame = HdlcFrame.decode(_read_frame(conn))
    get_request = dlms.parse_get_request(dlms.unwrap_llc(get_frame.information))
    value = obis_values.get(get_request.obis)
    if value is None:
        info = dlms.build_get_response_error(get_request.invoke_id, OBJECT_UNDEFINED)
    else:
        info = dlms.build_get_response_data(get_request.invoke_id, datatypes.encode_double_long_unsigned(value))
    response_frame = HdlcFrame(
        destination=get_frame.source, source=get_frame.destination,
        control=control_information_frame(2, 3), information=dlms.wrap_llc_response(info),
    )
    conn.sendall(response_frame.encode())


def test_read_via_call_home_succeeds_after_ignored_snrm_attempts():
    """Регрессия модели реального поведения: первая попытка SNRM ответа
    не получает — только повторная. read_via_call_home должен пережить
    это за счёт ретраев внутри пула."""
    serial = "202001002352"
    addr6 = bytes.fromhex("522300012020")
    password = b"12345678"
    # Родовой OBIS для теста — намеренно НЕ "1.1.1.8.0.ff" (суммарная
    # активная энергия), у которого с 2026-08-20 есть вендорский override
    # value-OBIS (dlms.VALUE_OBIS_OVERRIDES, см. test_integration_hdlc_dlms.py).
    obis = "1.1.1.7.0.ff"
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


def test_read_via_call_home_fails_fast_on_unaddressable_serial():
    """Регрессия найденного 2026-09-08 бага (см. DECISIONS.md, «22
    счётчика»): серийники, чьи последние 5 цифр >= 16384 (5 десятичных
    цифр физически доходят до 99999, а не влезают в 14-битное поле HDLC-
    адреса), раньше падали необработанным ``ValueError`` где-то в
    середине цикла ожидания — теперь адрес проверяется ДО входа в цикл,
    и ``AddressingError`` (см. ``errors.py``) поднимается сразу, не
    дожидаясь ``max_wait_s``."""
    from mmws_gateway.errors import AddressingError

    pool = CallHomePool(bind_host="127.0.0.1", bind_port=0, window_size=10)
    pool.start()
    try:
        start = time.time()
        with pytest.raises(AddressingError):
            read_via_call_home(
                pool, serial="999999999999", password=b"12345678", obis="1.1.1.8.0.ff",
                retry_interval_s=0.2, max_wait_s=30,
            )
        assert time.time() - start < 1.0
    finally:
        pool.stop()


def _run_fake_meter_load_profile(
    conn: socket.socket, *, addr6: bytes, password: bytes,
    load_profile_obis: bytes, rows: list, capture_period_seconds: int, block_size: int,
) -> None:
    """Тот же приём, что и ``_run_fake_meter``/``_serve_rest_of_session``
    (2026-08-19) — воспроизводит SNRM->UA->AARQ->AARE, затем GET
    capture_period (обычный GET, без access-selection) и GET с диапазоном
    дат (``_serve_load_profile`` из эмулятора, форсирующего датаблочную
    передачу)."""
    from mmws_gateway.emulators.hdlc_dlms_emulator import _read_frame, _serve_load_profile
    from mmws_gateway.protocols.hdlc import control_information_frame

    conn.sendall(_build_dummy_dlt645_frame(addr6))

    frame = HdlcFrame.decode(_read_frame(conn))
    assert frame.control == CONTROL_SNRM
    ua = HdlcFrame(destination=frame.source, source=frame.destination, control=CONTROL_UA)
    conn.sendall(ua.encode())

    aarq_frame = HdlcFrame.decode(_read_frame(conn))
    parsed_aarq = dlms.parse_aarq(dlms.unwrap_llc(aarq_frame.information))
    assert parsed_aarq.password == password
    aare = dlms.build_aare(accepted=True)
    aare_frame = HdlcFrame(
        destination=aarq_frame.source, source=aarq_frame.destination,
        control=control_information_frame(0, 1), information=dlms.wrap_llc_response(aare),
    )
    conn.sendall(aare_frame.encode())

    period_frame = HdlcFrame.decode(_read_frame(conn))
    period_request = dlms.parse_get_request(dlms.unwrap_llc(period_frame.information))
    assert period_request.obis == load_profile_obis
    info = dlms.build_get_response_data(
        period_request.invoke_id, datatypes.encode_double_long_unsigned(capture_period_seconds)
    )
    response_frame = HdlcFrame(
        destination=period_frame.source, source=period_frame.destination,
        control=control_information_frame(1, 2), information=dlms.wrap_llc_response(info),
    )
    conn.sendall(response_frame.encode())

    range_frame = HdlcFrame.decode(_read_frame(conn))
    payload = dlms.unwrap_llc(range_frame.information)
    _serve_load_profile(
        conn, range_frame, invoke_id=payload[2], rows=rows, block_size=block_size,
        send_seq=2, recv_seq=3,
    )


def test_read_load_profile_via_call_home_streams_rows():
    """Профиль нагрузки через call-home (2026-08-19) — тот же сценарий
    нестабильного реального счётчика (первая попытка SNRM без ответа),
    но для ВСЕГО обмена чтения буфера, не одного GET."""
    serial = "202001002352"
    addr6 = bytes.fromhex("522300012020")
    password = b"12345678"
    obis = "1.1.63.1.0.ff"
    class_id = dlms.PROFILE_GENERIC_CLASS_ID
    capture_period_seconds = 900
    from_dt = datetime(2026, 8, 1)
    to_dt = datetime(2026, 8, 19)
    rows = [[datatypes.encode_double_long_unsigned(5000 + i)] for i in range(4)]

    pool = CallHomePool(bind_host="127.0.0.1", bind_port=0, window_size=10)
    pool.start()
    try:
        client_conn = socket.create_connection(("127.0.0.1", pool.bind_port))
        meter_thread = threading.Thread(
            target=_run_fake_meter_load_profile,
            kwargs=dict(
                conn=client_conn, addr6=addr6, password=password,
                load_profile_obis=dlms.parse_obis(obis), rows=rows,
                capture_period_seconds=capture_period_seconds, block_size=6,
            ),
            daemon=True,
        )
        meter_thread.start()

        decoded = list(
            read_load_profile_via_call_home(
                pool, serial=serial, password=password, obis=obis, class_id=class_id,
                from_dt=from_dt, to_dt=to_dt,
                retry_interval_s=0.5, max_wait_s=15, per_attempt_timeout_ms=1500,
                max_attempts_per_connection=5,
            )
        )
        meter_thread.join(timeout=3)
    finally:
        pool.stop()

    assert len(decoded) == 4
    for i, (timestamp, values) in enumerate(decoded):
        assert timestamp == from_dt + timedelta(seconds=capture_period_seconds * i)
        assert values == [5000 + i]


# --- Событийное чтение сразу при подключении (2026-09-09, см.
# DECISIONS.md и план ticklish-popping-bear.md) —
# CallHomePool._maybe_trigger_immediate_read, вызывается из _identify.
# backend_client.claim_due_jobs/report_job_results и
# read_batch_via_fresh_connection замоканы — это тесты на поведение
# ПУЛА (кто остаётся held, кто закрывается), не на сам DLMS-обмен
# (тот уже покрыт test_integration_hdlc_dlms.py). ---


def test_maybe_trigger_immediate_read_backend_unreachable_leaves_pc_in_pool(monkeypatch):
    from mmws_gateway import backend_client

    monkeypatch.setattr(backend_client, "claim_due_jobs", lambda serial, **kw: None)

    pool = CallHomePool(bind_host="127.0.0.1", bind_port=0, window_size=10)
    pool.start()
    c1 = None
    try:
        addr6 = bytes.fromhex("522300012020")
        c1 = socket.create_connection(("127.0.0.1", pool.bind_port), timeout=3)
        c1.sendall(_build_dummy_dlt645_frame(addr6))
        time.sleep(0.3)
        # Backend "недоступен" (мок вернул None) — соединение остаётся
        # в пуле нетронутым для старого пути, как если бы этого
        # события вовсе не было.
        assert pool.pending_count() == 1
    finally:
        pool.stop()
        if c1 is not None:
            try:
                c1.close()
            except OSError:
                pass


def test_maybe_trigger_immediate_read_with_jobs_removes_pc_and_reports_results(monkeypatch):
    from mmws_gateway import backend_client
    from mmws_gateway.protocols.hdlc_dlms import RegisterReadOutcome

    claimed = backend_client.ClaimDueJobsResult(
        meter_found=True, meter_id=1, protocol_profile="hdlc_dlms", password="12345678",
        jobs=[backend_client.DueJob(job_id=7, job_type="read_current", obis="1.1.1.8.0.ff", class_id=0)],
    )
    monkeypatch.setattr(backend_client, "claim_due_jobs", lambda serial, **kw: claimed)

    reported: dict = {}

    def fake_report(serial, results, **kw):
        reported["serial"] = serial
        reported["results"] = results
        return True

    monkeypatch.setattr(backend_client, "report_job_results", fake_report)
    monkeypatch.setattr(
        "mmws_gateway.callhome.read_batch_via_fresh_connection",
        lambda pc, **kw: [RegisterReadOutcome(obis="1.1.1.8.0.ff", ok=True, value=999)],
    )

    pool = CallHomePool(bind_host="127.0.0.1", bind_port=0, window_size=10)
    pool.start()
    c1 = None
    try:
        addr6 = bytes.fromhex("522300012020")  # -> 202001002352
        c1 = socket.create_connection(("127.0.0.1", pool.bind_port), timeout=3)
        c1.sendall(_build_dummy_dlt645_frame(addr6))
        time.sleep(0.3)

        # Job'ы найдены — pc забран событийным путём, в пуле его больше нет.
        assert pool.pending_count() == 0
        assert reported["serial"] == "202001002352"
        assert len(reported["results"]) == 1
        assert reported["results"][0].job_id == 7
        assert reported["results"][0].ok is True
        assert reported["results"][0].value == 999
    finally:
        pool.stop()
        if c1 is not None:
            try:
                c1.close()
            except OSError:
                pass


def test_maybe_trigger_immediate_read_closes_sibling_same_serial_connections(monkeypatch):
    """По просьбе пользователя (2026-09-09): как только одно соединение
    счётчика стало активным, остальные held-соединения ЭТОГО ЖЕ
    счётчика должны быть закрыты, а не просто оставлены висеть до
    истечения окна/лимита."""
    from mmws_gateway import backend_client
    from mmws_gateway.protocols.hdlc_dlms import RegisterReadOutcome

    claimed = backend_client.ClaimDueJobsResult(
        meter_found=True, meter_id=1, protocol_profile="hdlc_dlms", password="12345678",
        jobs=[backend_client.DueJob(job_id=7, job_type="read_current", obis="1.1.1.8.0.ff", class_id=0)],
    )
    call_count = {"n": 0}

    def fake_claim(serial, **kw):
        call_count["n"] += 1
        return claimed if call_count["n"] >= 3 else None  # первые 2 подключения — "нечего читать"

    monkeypatch.setattr(backend_client, "claim_due_jobs", fake_claim)
    monkeypatch.setattr(backend_client, "report_job_results", lambda serial, results, **kw: True)
    monkeypatch.setattr(
        "mmws_gateway.callhome.read_batch_via_fresh_connection",
        lambda pc, **kw: [RegisterReadOutcome(obis="1.1.1.8.0.ff", ok=True, value=1)],
    )

    pool = CallHomePool(bind_host="127.0.0.1", bind_port=0, window_size=10, max_per_serial=10)
    pool.start()
    c1 = c2 = c3 = None
    try:
        addr6 = bytes.fromhex("522300012020")
        c1 = socket.create_connection(("127.0.0.1", pool.bind_port), timeout=3)
        c1.sendall(_build_dummy_dlt645_frame(addr6))
        time.sleep(0.3)
        c2 = socket.create_connection(("127.0.0.1", pool.bind_port), timeout=3)
        c2.sendall(_build_dummy_dlt645_frame(addr6))
        time.sleep(0.3)
        assert pool.pending_count() == 2  # c1, c2 остались held (нечего было читать)

        c3 = socket.create_connection(("127.0.0.1", pool.bind_port), timeout=3)
        c3.sendall(_build_dummy_dlt645_frame(addr6))
        time.sleep(0.3)

        # c3 забран событийным путём; c1/c2 закрыты как "соседи того же
        # счётчика", а не просто продолжают висеть в пуле.
        assert pool.pending_count() == 0
        c1.settimeout(1)
        assert c1.recv(1) == b""
        c2.settimeout(1)
        assert c2.recv(1) == b""
    finally:
        pool.stop()
        for c in (c1, c2, c3):
            if c is not None:
                try:
                    c.close()
                except OSError:
                    pass
