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
from concurrent import futures

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


class GatewayServiceServicer(gateway_pb2_grpc.GatewayServiceServicer):
    def __init__(self, call_home_pool: CallHomePool | None = None) -> None:
        self._call_home_pool = call_home_pool

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
        return gateway_pb2.HealthCheckResponse(ok=True, driver_version=DRIVER_VERSION)


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
        GatewayServiceServicer(call_home_pool=call_home_pool), server
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
