"""gRPC-сервер Gateway (ТЗ п. 4.1 — внутренний RPC Backend <-> Gateway, Этап 1).

Оборачивает существующий протокольный слой Этапа 0 (`cli._do_read`) в
gRPC-сервис. Gateway остаётся stateless относительно пользователей и
задач (Promt_MMWS.md, раздел 3, принцип 1) — этот модуль не содержит
бизнес-логики, только транспорт вызова протокольных операций.
"""

from __future__ import annotations

import json
import logging
from concurrent import futures

import grpc

from .errors import GatewayError
from .grpc_generated import gateway_pb2, gateway_pb2_grpc
from .protocols import hdlc_dlms, mode_c, mode_e
from .session import run_with_retries
from .transport import DEFAULT_RETRIES, DEFAULT_TIMEOUT_MS, TcpTransport, TransportConfig

logger = logging.getLogger("mmws_gateway.grpc_server")

DRIVER_VERSION = "0.1.0-etap1"


def _do_read(request: gateway_pb2.ReadRegisterRequest) -> object:
    password = request.password.encode("ascii")
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
                    transport, serial=request.serial, password=password, obis=request.obis
                )
            raise GatewayError(f"Неизвестный протокольный профиль: {request.profile!r}")

    return run_with_retries(operation, max_retries=retries)


class GatewayServiceServicer(gateway_pb2_grpc.GatewayServiceServicer):
    def ReadRegister(self, request, context):
        try:
            value = _do_read(request)
        except GatewayError as exc:
            logger.warning("Ошибка чтения serial=%s obis=%s: [%s] %s", request.serial, request.obis, exc.code, exc.message)
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

    def HealthCheck(self, request, context):
        return gateway_pb2.HealthCheckResponse(ok=True, driver_version=DRIVER_VERSION)


def _jsonable(value: object) -> object:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def serve(*, host: str = "0.0.0.0", port: int = 50051, max_workers: int = 10) -> grpc.Server:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    gateway_pb2_grpc.add_GatewayServiceServicer_to_server(GatewayServiceServicer(), server)
    server.add_insecure_port(f"{host}:{port}")
    server.start()
    logger.info("Gateway gRPC-сервер запущен на %s:%s", host, port)
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = serve()
    server.wait_for_termination()


if __name__ == "__main__":
    main()
