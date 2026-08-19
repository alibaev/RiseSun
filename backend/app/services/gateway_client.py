"""gRPC-клиент к Protocol Gateway (ТЗ п. 4.1 — внутренний RPC).

Backend не реализует протокольную логику — только вызывает Gateway по
gRPC и интерпретирует результат (Promt_MMWS.md, раздел 3, принцип 1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import AsyncIterator

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
    serial: str,
    password: str,
    obis: str,
    host: str = "",
    port: int = 0,
    call_home: bool = False,
    class_id: int = 0,
    timeout_ms: int = 0,
    retries: int = 0,
    call_timeout_s: float = 60.0,
) -> ReadResult:
    """``call_home=True`` — счётчик сам звонит Gateway (подтверждённое
    расхождение с ТЗ Table 1, см. ../DECISIONS.md); ``host``/``port``
    в этом случае игнорируются на стороне Gateway (не набираются), а
    ``call_timeout_s`` стоит держать не меньше таймаута ожидания внутри
    Gateway (``callhome.read_via_call_home``'s ``max_wait_s``, по
    умолчанию тоже 60с — согласовано специально). ``class_id=0`` —
    дефолт Gateway (Register, класс 3); параметры Этапа 2 (класс 1,
    Data) передают явно."""
    async with grpc.aio.insecure_channel(grpc_target) as channel:
        stub = gateway_pb2_grpc.GatewayServiceStub(channel)
        request = gateway_pb2.ReadRegisterRequest(
            profile=profile,
            host=host,
            port=port,
            serial=serial,
            password=password,
            obis=obis,
            class_id=class_id,
            timeout_ms=timeout_ms,
            retries=retries,
            call_home=call_home,
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


@dataclass
class WriteResult:
    ok: bool
    error_code: str | None = None
    error_message: str | None = None


async def write_register(
    *,
    grpc_target: str,
    profile: str,
    serial: str,
    password: str,
    obis: str,
    value_bytes: bytes,
    class_id: int = 0,
    value_type: str = "octet_string",
    host: str = "",
    port: int = 0,
    call_home: bool = False,
    timeout_ms: int = 0,
    retries: int = 0,
    call_timeout_s: float = 60.0,
) -> WriteResult:
    """Запись одного атрибута COSEM-объекта (Этап 2, ТЗ п.4.2.4).
    ``value_bytes``/``value_type`` — сырое значение и дискриминатор
    DLMS-кодировки; Gateway кодирует в Common-Data-Type сам (Backend не
    реализует протокольную логику — Promt_MMWS.md, раздел 3, принцип 1)."""
    async with grpc.aio.insecure_channel(grpc_target) as channel:
        stub = gateway_pb2_grpc.GatewayServiceStub(channel)
        request = gateway_pb2.WriteRegisterRequest(
            profile=profile,
            host=host,
            port=port,
            serial=serial,
            password=password,
            obis=obis,
            class_id=class_id,
            value_type=value_type,
            value_bytes=value_bytes,
            timeout_ms=timeout_ms,
            retries=retries,
            call_home=call_home,
        )
        response = await stub.WriteRegister(request, timeout=call_timeout_s)

    which = response.WhichOneof("result")
    if which == "success":
        return WriteResult(ok=True)
    error = response.error
    return WriteResult(ok=False, error_code=error.code, error_message=error.message)


@dataclass
class LoadProfileRow:
    timestamp_iso: str
    values: list


class LoadProfileError(Exception):
    """Строки, уже отданные генератором ДО этого исключения, не теряются —
    вызывающий код (job_worker) успевает сохранить их в БД до того, как
    исключение прервёт итерацию (ТЗ п.4.2.3 — докачка при обрыве)."""

    def __init__(self, code: str, message: str, is_partial: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.is_partial = is_partial


async def read_load_profile(
    *,
    grpc_target: str,
    profile: str,
    serial: str,
    password: str,
    obis: str,
    from_iso: str,
    to_iso: str,
    host: str = "",
    port: int = 0,
    call_home: bool = False,
    class_id: int = 0,
    timeout_ms: int = 0,
    retries: int = 0,
    call_timeout_s: float = 180.0,
) -> AsyncIterator[LoadProfileRow]:
    """Профиль нагрузки (Этап 3, ТЗ п.4.2.3) — server-streaming RPC, генератор
    отдаёт каждую строку сразу по получении (не ждёт всего ответа целиком).
    ``call_timeout_s`` выше, чем у read_register — буфер профиля нагрузки
    может передаваться несколькими датаблоками дольше, чем чтение одного
    регистра."""
    async with grpc.aio.insecure_channel(grpc_target) as channel:
        stub = gateway_pb2_grpc.GatewayServiceStub(channel)
        request = gateway_pb2.ReadLoadProfileRequest(
            profile=profile,
            host=host,
            port=port,
            serial=serial,
            password=password,
            obis=obis,
            class_id=class_id,
            from_iso=from_iso,
            to_iso=to_iso,
            timeout_ms=timeout_ms,
            retries=retries,
            call_home=call_home,
        )
        async for response in stub.ReadLoadProfile(request, timeout=call_timeout_s):
            which = response.WhichOneof("result")
            if which == "row":
                yield LoadProfileRow(
                    timestamp_iso=response.row.timestamp_iso,
                    values=json.loads(response.row.values_json),
                )
            else:
                error = response.error
                raise LoadProfileError(error.code, error.message, error.is_partial)
