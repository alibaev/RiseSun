"""gRPC-сервер Gateway (ТЗ п. 4.1 — внутренний RPC Backend <-> Gateway, Этап 1).

Оборачивает существующий протокольный слой Этапа 0 (`cli._do_read`) в
gRPC-сервис. Gateway остаётся stateless относительно пользователей и
задач (Promt_MMWS.md, раздел 3, принцип 1) — этот модуль не содержит
бизнес-логики, только транспорт вызова протокольных операций.

Поддерживает оба транспортных сценария: обычный (Gateway — инициатор
TCP-соединения, ТЗ Table 1) и call-home (счётчик сам инициирует
соединение — подтверждённое на практике расхождение с ТЗ, см.
``callhome.py`` и DECISIONS.md).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from concurrent import futures
from datetime import datetime

import grpc

from .callhome import CallHomePool, read_via_call_home
from .errors import GatewayError
from .grpc_generated import gateway_pb2, gateway_pb2_grpc
from .protocols import datatypes, dlms, hdlc_dlms, mode_c, mode_e
from .session import run_with_retries
from .transport import DEFAULT_RETRIES, DEFAULT_TIMEOUT_MS, TcpTransport, TransportConfig

logger = logging.getLogger("mmws_gateway.grpc_server")

DRIVER_VERSION = "0.1.0-etap1"
DEFAULT_CALL_HOME_PORT = int(os.environ.get("MMWS_CALL_HOME_PORT", "2009"))


def _do_read(request: gateway_pb2.ReadRegisterRequest, call_home_pool: CallHomePool | None) -> object:
    password = request.password.encode("ascii")

    if request.call_home:
        if call_home_pool is None:
            raise GatewayError("Call-home пул не запущен на этом экземпляре Gateway")
        # max_wait_s согласован с call_timeout_s в backend/app/services/
        # job_worker.py (160с для звонящих домой счётчиков) — реальная
        # задержка ответа устройства сильно "дрожит" (см. callhome.py),
        # больший общий бюджет времени даёт больше шансов на удачное
        # совпадение по таймингу за счёт большего числа held-соединений,
        # которые успеют смениться за время ожидания.
        return read_via_call_home(
            call_home_pool, serial=request.serial, password=password, obis=request.obis, max_wait_s=150.0
        )

    timeout_ms = request.timeout_ms or DEFAULT_TIMEOUT_MS
    retries = request.retries or DEFAULT_RETRIES
    config = TransportConfig(host=request.host, port=request.port, timeout_ms=timeout_ms, max_retries=1)

    def operation() -> object:
        with TcpTransport(config) as transport:
            if request.profile == "mode_c":
                short_code = mode_c.obis6_to_short_code(request.obis)
                return mode_c.read_value(transport, serial=request.serial, obis_5=short_code)
            if request.profile == "mode_e":
                return mode_e.read_register(
                    transport, serial=request.serial, password=password, obis=request.obis
                )
            if request.profile == "hdlc_dlms":
                return hdlc_dlms.read_register(
                    transport, serial=request.serial, password=password, obis=request.obis,
                    class_id=request.class_id or dlms.REGISTER_CLASS_ID,
                )
            raise GatewayError(f"Неизвестный протокольный профиль: {request.profile!r}")

    return run_with_retries(operation, max_retries=retries)


# Кодировщики значения записи (Этап 2, ТЗ п.4.2.4) по дискриминатору
# ``value_type`` из WriteRegisterRequest — Gateway кодирует в DLMS
# Common-Data-Type, Backend передаёт только "сырые" данные и тип, не
# зная деталей DLMS-кодировки (Promt_MMWS.md, раздел 3, принцип 1).
# octet_string — «Установить дату/время»; unsigned (1 байт значения в
# value_bytes[0]) — «Текущий/доступный номер расчётного периода»
# (единственные ещё не реализованные записываемые объекты словаря OBIS,
# см. DECISIONS.md). Остальные типы добавляются по мере появления в
# словаре OBIS кодов для оставшихся категорий (режимы отображения,
# тарифное расписание, профиль нагрузки, GPRS).
_VALUE_ENCODERS = {
    "octet_string": lambda request: datatypes.encode_octet_string(bytes(request.value_bytes)),
    "unsigned": lambda request: datatypes.encode_unsigned(request.value_bytes[0]),
}


def _do_write(request: gateway_pb2.WriteRegisterRequest, call_home_pool: CallHomePool | None) -> None:
    password = request.password.encode("ascii")

    encoder = _VALUE_ENCODERS.get(request.value_type)
    if encoder is None:
        raise GatewayError(f"Неподдержанный тип значения для записи: {request.value_type!r}")
    encoded_value = encoder(request)

    if request.call_home:
        # Живая проверка call-home чтения 2026-08-18 выявила крайне
        # нестабильную задержку ответа реального счётчика даже для
        # ПОДТВЕРЖДЁННО доставленных запросов (см. DECISIONS.md) —
        # решено оставить call-home транспорт "как есть" без записи
        # параметров через него до отдельной доработки/диагностики.
        raise GatewayError("Запись параметров через call-home пока не реализована")

    timeout_ms = request.timeout_ms or DEFAULT_TIMEOUT_MS
    retries = request.retries or DEFAULT_RETRIES
    config = TransportConfig(host=request.host, port=request.port, timeout_ms=timeout_ms, max_retries=1)

    def operation() -> None:
        with TcpTransport(config) as transport:
            if request.profile == "hdlc_dlms":
                hdlc_dlms.write_register(
                    transport, serial=request.serial, password=password, obis=request.obis,
                    encoded_value=encoded_value,
                    class_id=request.class_id or dlms.REGISTER_CLASS_ID,
                )
                return
            raise GatewayError(
                f"Запись параметров для протокольного профиля {request.profile!r} пока не реализована"
            )

    run_with_retries(operation, max_retries=retries)


def _do_read_load_profile(request: gateway_pb2.ReadLoadProfileRequest, call_home_pool: CallHomePool | None):
    """Генератор строк профиля нагрузки (Этап 3, ТЗ п.4.2.3). Без
    ``run_with_retries`` — повтор ПОСЕРЕДИНЕ уже начатой потоковой
    передачи неоднозначен (какие строки повторно запрашивать), поэтому
    решение «докачивать ли остаток после обрыва» (is_partial) отдано
    на откуп Backend'у (см. ReadLoadProfile в gateway.proto), а не
    реализовано здесь неявным повтором всей операции заново."""
    password = request.password.encode("ascii")

    if request.call_home:
        raise GatewayError("Профиль нагрузки через call-home пока не реализован")
    if request.profile != "hdlc_dlms":
        raise GatewayError(f"Профиль нагрузки для протокольного профиля {request.profile!r} пока не реализован")

    timeout_ms = request.timeout_ms or DEFAULT_TIMEOUT_MS
    config = TransportConfig(host=request.host, port=request.port, timeout_ms=timeout_ms, max_retries=1)
    from_dt = datetime.fromisoformat(request.from_iso)
    to_dt = datetime.fromisoformat(request.to_iso)

    with TcpTransport(config) as transport:
        yield from hdlc_dlms.read_load_profile(
            transport, serial=request.serial, password=password, obis=request.obis,
            class_id=request.class_id or dlms.PROFILE_GENERIC_CLASS_ID,
            from_dt=from_dt, to_dt=to_dt,
        )


def _load_profile_row_to_proto(row: object) -> gateway_pb2.LoadProfileRow:
    """Первая колонка строки буфера по конвенции — метка времени
    (octet_string, 12 сырых байт cosem-date-time); Gateway декодирует
    её сам (Backend не реализует протокольную логику — Promt_MMWS.md,
    раздел 3, принцип 1). Если строка не в этой форме — гипотеза о
    структуре буфера профиля нагрузки (см. DECISIONS.md) не
    подтвердилась, это должно упасть явной ошибкой, а не молча отдать
    мусор."""
    if not isinstance(row, list) or not row or not isinstance(row[0], bytes) or len(row[0]) != 12:
        raise GatewayError(
            "Первая колонка строки профиля нагрузки не похожа на cosem-date-time "
            "(12 сырых байт) — гипотеза о структуре буфера не подтвердилась"
        )
    timestamp = datatypes.decode_cosem_date_time(row[0])
    return gateway_pb2.LoadProfileRow(
        timestamp_iso=timestamp.isoformat(), values_json=json.dumps(_jsonable(row[1:]))
    )


_DISCONNECT_METHOD_IDS = {
    "disconnect": dlms.METHOD_REMOTE_DISCONNECT,
    "reconnect": dlms.METHOD_REMOTE_RECONNECT,
}


def _do_disconnect(request: gateway_pb2.DisconnectMeterRequest, call_home_pool: CallHomePool | None) -> None:
    """Удалённое отключение/подключение (Этап 5, ТЗ п.4.2.10) — ACTION
    на объект Disconnect Control (см. protocols.dlms). ``request.operation``
    — дискриминатор "disconnect"/"reconnect", Backend не знает про
    method_id DLMS (Promt_MMWS.md, раздел 3, принцип 1)."""
    password = request.password.encode("ascii")

    method_id = _DISCONNECT_METHOD_IDS.get(request.operation)
    if method_id is None:
        raise GatewayError(f"Неизвестная операция отключения: {request.operation!r}")

    if request.call_home:
        raise GatewayError("Отключение/подключение через call-home пока не реализовано")
    if request.profile != "hdlc_dlms":
        raise GatewayError(
            f"Отключение/подключение для протокольного профиля {request.profile!r} пока не поддержано",
        )

    timeout_ms = request.timeout_ms or DEFAULT_TIMEOUT_MS
    retries = request.retries or DEFAULT_RETRIES
    config = TransportConfig(host=request.host, port=request.port, timeout_ms=timeout_ms, max_retries=1)

    def operation() -> None:
        with TcpTransport(config) as transport:
            hdlc_dlms.execute_action(
                transport, serial=request.serial, password=password,
                obis=dlms.DISCONNECT_CONTROL_OBIS, method_id=method_id,
                class_id=dlms.DISCONNECT_CONTROL_CLASS_ID,
            )

    run_with_retries(operation, max_retries=retries)


class GatewayServiceServicer(gateway_pb2_grpc.GatewayServiceServicer):
    def __init__(self, call_home_pool: CallHomePool | None = None, call_home_bind_host: str = "0.0.0.0") -> None:
        self._call_home_pool = call_home_pool
        self._call_home_bind_host = call_home_bind_host
        # Защищает SetCallHomePort от одновременных вызовов из нескольких
        # RPC-потоков (grpc.server использует пул потоков) — без этого
        # два параллельных вызова могли бы оба запустить новый пул и
        # потерять ссылку на один из них (утечка потока/сокета).
        self._call_home_lock = threading.Lock()

    def ReadRegister(self, request, context):
        try:
            value = _do_read(request, self._call_home_pool)
        except GatewayError as exc:
            logger.warning(
                "Ошибка чтения serial=%s obis=%s call_home=%s: [%s] %s",
                request.serial, request.obis, request.call_home, exc.code, exc.message,
            )
            return gateway_pb2.ReadRegisterResponse(
                error=gateway_pb2.ReadError(
                    code=exc.code,
                    message=exc.message,
                    is_partial=exc.is_partial,
                    raw_frame_hex=exc.raw_frame.hex() if exc.raw_frame else "",
                )
            )
        return gateway_pb2.ReadRegisterResponse(
            value=gateway_pb2.ReadValue(value_json=json.dumps(_jsonable(value)))
        )

    def WriteRegister(self, request, context):
        try:
            _do_write(request, self._call_home_pool)
        except GatewayError as exc:
            logger.warning(
                "Ошибка записи serial=%s obis=%s class_id=%s: [%s] %s",
                request.serial, request.obis, request.class_id, exc.code, exc.message,
            )
            return gateway_pb2.WriteRegisterResponse(
                error=gateway_pb2.ReadError(
                    code=exc.code,
                    message=exc.message,
                    is_partial=exc.is_partial,
                    raw_frame_hex=exc.raw_frame.hex() if exc.raw_frame else "",
                )
            )
        return gateway_pb2.WriteRegisterResponse(success=gateway_pb2.WriteSuccess(ok=True))

    def HealthCheck(self, request, context):
        call_home_port = self._call_home_pool.bind_port if self._call_home_pool is not None else 0
        return gateway_pb2.HealthCheckResponse(ok=True, driver_version=DRIVER_VERSION, call_home_port=call_home_port)

    def ListCallHomeSerials(self, request, context):
        seen = self._call_home_pool.list_seen_serials() if self._call_home_pool is not None else {}
        return gateway_pb2.ListCallHomeSerialsResponse(
            serials=[
                gateway_pb2.SeenSerial(serial=serial, first_seen_unix=first_seen)
                for serial, first_seen in seen.items()
            ]
        )

    def SetCallHomePort(self, request, context):
        try:
            with self._call_home_lock:
                old_pool = self._call_home_pool
                new_pool = CallHomePool(bind_host=self._call_home_bind_host, bind_port=request.port)
                new_pool.start()
                self._call_home_pool = new_pool
            if old_pool is not None:
                old_pool.stop()
            logger.info("Call-home пул переслушан на порту %d (без перезапуска процесса)", new_pool.bind_port)
        except OSError as exc:
            logger.warning("Не удалось переслушать call-home пул на порту %d: %s", request.port, exc)
            return gateway_pb2.SetCallHomePortResponse(
                error=gateway_pb2.ReadError(code="GATEWAY_ERROR", message=str(exc))
            )
        return gateway_pb2.SetCallHomePortResponse(
            success=gateway_pb2.SetCallHomePortSuccess(port=new_pool.bind_port)
        )

    def DisconnectMeter(self, request, context):
        try:
            _do_disconnect(request, self._call_home_pool)
        except GatewayError as exc:
            logger.warning(
                "Ошибка операции %s serial=%s: [%s] %s",
                request.operation, request.serial, exc.code, exc.message,
            )
            return gateway_pb2.DisconnectMeterResponse(
                error=gateway_pb2.ReadError(
                    code=exc.code,
                    message=exc.message,
                    is_partial=exc.is_partial,
                    raw_frame_hex=exc.raw_frame.hex() if exc.raw_frame else "",
                )
            )
        return gateway_pb2.DisconnectMeterResponse(success=gateway_pb2.WriteSuccess(ok=True))

    def ReadLoadProfile(self, request, context):
        try:
            for row in _do_read_load_profile(request, self._call_home_pool):
                yield gateway_pb2.ReadLoadProfileResponse(row=_load_profile_row_to_proto(row))
        except GatewayError as exc:
            logger.warning(
                "Ошибка чтения профиля нагрузки serial=%s obis=%s: [%s] %s",
                request.serial, request.obis, exc.code, exc.message,
            )
            yield gateway_pb2.ReadLoadProfileResponse(
                error=gateway_pb2.ReadError(
                    code=exc.code,
                    message=exc.message,
                    is_partial=exc.is_partial,
                    raw_frame_hex=exc.raw_frame.hex() if exc.raw_frame else "",
                )
            )


def _jsonable(value: object) -> object:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def serve(
    *,
    host: str = "0.0.0.0",
    port: int = 50051,
    max_workers: int = 10,
    call_home_port: int | None = DEFAULT_CALL_HOME_PORT,
) -> tuple[grpc.Server, CallHomePool | None]:
    """``call_home_port=None`` отключает call-home пул (например, для
    тестов, где он не нужен и просто занимал бы порт)."""
    call_home_pool = None
    if call_home_port is not None:
        call_home_pool = CallHomePool(bind_host=host, bind_port=call_home_port)
        call_home_pool.start()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    gateway_pb2_grpc.add_GatewayServiceServicer_to_server(
        GatewayServiceServicer(call_home_pool=call_home_pool, call_home_bind_host=host), server
    )
    server.add_insecure_port(f"{host}:{port}")
    server.start()
    logger.info("Gateway gRPC-сервер запущен на %s:%s", host, port)
    return server, call_home_pool


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server, call_home_pool = serve()
    try:
        server.wait_for_termination()
    finally:
        if call_home_pool is not None:
            call_home_pool.stop()


if __name__ == "__main__":
    main()
