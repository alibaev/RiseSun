"""gRPC-клиент к Protocol Gateway (ТЗ п. 4.1 — внутренний RPC).

Backend не реализует протокольную логику — только вызывает Gateway по
gRPC и интерпретирует результат (Promt_MMWS.md, раздел 3, принцип 1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import grpc

from mmws_gateway.grpc_generated import gateway_pb2, gateway_pb2_grpc


@dataclass
class ReadResult:
    ok: bool
    value: object | None = None
    error_code: str | None = None
    error_message: str | None = None
    is_partial: bool = False
    raw_frame_hex: str = ""


async def read_register(
    *,
    grpc_target: str,
    profile: str,
    host: str,
    port: int,
    serial: str,
    password: str,
    obis: str,
    timeout_ms: int = 0,
    retries: int = 0,
    call_timeout_s: float = 60.0,
) -> ReadResult:
    async with grpc.aio.insecure_channel(grpc_target) as channel:
        stub = gateway_pb2_grpc.GatewayServiceStub(channel)
        request = gateway_pb2.ReadRegisterRequest(
            profile=profile,
            host=host,
            port=port,
            serial=serial,
            password=password,
            obis=obis,
            timeout_ms=timeout_ms,
            retries=retries,
        )
        response = await stub.ReadRegister(request, timeout=call_timeout_s)

    which = response.WhichOneof("result")
    if which == "value":
        return ReadResult(ok=True, value=json.loads(response.value.value_json))
    error = response.error
    return ReadResult(
        ok=False,
        error_code=error.code,
        error_message=error.message,
        is_partial=error.is_partial,
        raw_frame_hex=error.raw_frame_hex,
    )
