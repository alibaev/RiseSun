"""HTTP-клиент к внутреннему каналу Backend'а (событийное чтение
call-home счётчиков сразу при подключении, см. DECISIONS.md и план
/root/.claude/plans/ticklish-popping-bear.md). Namespace-совпадение с
``gateway_client.py`` на стороне Backend по духу, не по коду — тот
ходит в Gateway через gRPC, этот ходит в Backend через обычный HTTP
(на стандартной библиотеке — у Gateway сейчас нет зависимостей, кроме
grpcio/protobuf, заводить новую ради одного маленького клиента не
нужно).

Оба публичных вызова — ``claim_due_jobs``/``report_job_results`` —
FAIL-CLOSED: любая сетевая ошибка, таймаут, не-2xx ответ или отсутствие
настроенного секрета трактуются как "ничего не делать" (``None``/
``False``), а НЕ как исключение — вызывающий код (``callhome.py``)
обязан в этом случае оставить соединение как есть, не трогая его,
чтобы старый (пуловый) путь чтения остался рабочим независимо от
доступности этого канала."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger("mmws_gateway.backend_client")

DEFAULT_BACKEND_INTERNAL_URL = os.environ.get("MMWS_BACKEND_INTERNAL_URL", "http://backend:8000")
_INTERNAL_SECRET = os.environ.get("MMWS_GATEWAY_INTERNAL_SECRET", "")
# Быстрый внутрикластерный HTTP-вызов (не обращённый к счётчику через
# GPRS/сотовую сеть) — короткий таймаут оправдан именно потому, что это
# запрос к соседнему контейнеру в той же docker-сети, а не к устройству
# в поле; ср. с 45-160с таймаутами на стороне протокола счётчика.
DEFAULT_CLAIM_TIMEOUT_S = float(os.environ.get("MMWS_BACKEND_CLAIM_TIMEOUT_S", "3.0"))
DEFAULT_REPORT_TIMEOUT_S = float(os.environ.get("MMWS_BACKEND_REPORT_TIMEOUT_S", "10.0"))


@dataclass
class DueJob:
    job_id: int
    job_type: str
    obis: str
    class_id: int


@dataclass
class DueLoadProfileJob:
    # 2026-09-11 — перенос read_load_profile на событийный путь (см.
    # DECISIONS.md).
    job_id: int
    obis: str
    class_id: int
    from_iso: str
    to_iso: str


@dataclass
class ClaimDueJobsResult:
    meter_found: bool
    meter_id: int | None = None
    protocol_profile: str | None = None
    password: str | None = None
    jobs: list[DueJob] = field(default_factory=list)
    load_profile_jobs: list[DueLoadProfileJob] = field(default_factory=list)


@dataclass
class JobResultReport:
    job_id: int
    obis: str
    ok: bool
    value: object | None = None
    error_code: str | None = None
    error_message: str | None = None
    is_partial: bool = False


def _post_json(path: str, payload: dict, *, timeout_s: float) -> dict | None:
    if not _INTERNAL_SECRET:
        logger.warning(
            "MMWS_GATEWAY_INTERNAL_SECRET не задан — событийное чтение через Backend отключено"
        )
        return None
    url = f"{DEFAULT_BACKEND_INTERNAL_URL}{path}"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Internal-Secret": _INTERNAL_SECRET},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
        logger.warning("Backend internal call %s не удался: %s", path, exc)
        return None


def claim_due_jobs(
    serial: str,
    *,
    peer_ip: str | None = None,
    local_port: int | None = None,
    timeout_s: float = DEFAULT_CLAIM_TIMEOUT_S,
) -> ClaimDueJobsResult | None:
    """``None`` — не удалось связаться с Backend (сеть/таймаут/секрет не
    настроен) ИЛИ Backend явно ответил "ничего не должен" (фича
    выключена, счётчик не в allowlist, нет due job'ов) — вызывающий код
    в обоих случаях просто ничего не делает, соединение остаётся в
    пуле для старого пути.

    ``peer_ip`` (2026-09-11) — IP-адрес звонящего соединения, на котором
    опознан этот серийник; Backend сохраняет его в Meter.ip_address при
    КАЖДОМ вызове (даже когда due job'ов нет и эта функция вернёт
    ``None``) — call-home-счётчики сами инициируют соединение, иначе их
    ip_address никогда и нигде не сохраняется.

    ``local_port`` (2026-09-11) — локальный порт Gateway'я, на который
    пришло это соединение (см. CallHomePool.extra_bind_ports); Backend
    превращает его в РЭС/объект (services/res_mapping.py) и сохраняет
    в Meter.res_name — пользователь физически разводит дозвон разных
    РЭС по разным портам."""
    payload: dict[str, object] = {}
    if peer_ip:
        payload["peer_ip"] = peer_ip
    if local_port is not None:
        payload["local_port"] = local_port
    body = _post_json(f"/api/internal/gateway/meters/{serial}/claim-jobs", payload, timeout_s=timeout_s)
    if body is None:
        return None
    if not body.get("meter_found") or not (body.get("jobs") or body.get("load_profile_jobs")):
        return None
    jobs = [
        DueJob(job_id=j["job_id"], job_type=j["job_type"], obis=j["obis"], class_id=j["class_id"])
        for j in body["jobs"]
    ]
    load_profile_jobs = [
        DueLoadProfileJob(
            job_id=j["job_id"], obis=j["obis"], class_id=j["class_id"],
            from_iso=j["from_iso"], to_iso=j["to_iso"],
        )
        for j in body.get("load_profile_jobs") or []
    ]
    return ClaimDueJobsResult(
        meter_found=True,
        meter_id=body.get("meter_id"),
        protocol_profile=body.get("protocol_profile"),
        password=body.get("password"),
        jobs=jobs,
        load_profile_jobs=load_profile_jobs,
    )


def report_job_results(
    serial: str, results: list[JobResultReport], *, timeout_s: float = DEFAULT_REPORT_TIMEOUT_S
) -> bool:
    """``False`` — отчёт не доставлен; job'ы останутся RUNNING на
    стороне Backend и будут возвращены в очередь периодическим
    ``stale_job_reaper_loop`` (см. backend/app/services/job_worker.py),
    после чего их подхватит старый (пуловый) путь чтения."""
    payload = {
        "serial": serial,
        "results": [
            {
                "job_id": r.job_id, "obis": r.obis, "ok": r.ok, "value": r.value,
                "error_code": r.error_code, "error_message": r.error_message, "is_partial": r.is_partial,
            }
            for r in results
        ],
    }
    body = _post_json("/api/internal/gateway/job-results", payload, timeout_s=timeout_s)
    return body is not None


@dataclass
class LoadProfileRowReport:
    timestamp_iso: str
    values: object


def report_load_profile_results(
    serial: str,
    *,
    job_id: int,
    obis: str,
    rows: list[LoadProfileRowReport],
    ok: bool,
    error_code: str | None = None,
    error_message: str | None = None,
    is_partial: bool = False,
    timeout_s: float = DEFAULT_REPORT_TIMEOUT_S,
) -> bool:
    """2026-09-11 — перенос read_load_profile на событийный путь (см.
    DECISIONS.md). ``rows`` — ВСЕ строки, собранные Gateway'ем к моменту
    отчёта (даже при ``ok=False``/``is_partial=True`` — обрыв связи
    посреди передачи датаблоков не должен терять уже полученные строки).
    ``False`` — отчёт не доставлен, job останется RUNNING на стороне
    Backend и будет возвращён в очередь периодическим
    ``stale_job_reaper_loop``, после чего его подхватит старый путь."""
    payload = {
        "serial": serial,
        "job_id": job_id,
        "obis": obis,
        "ok": ok,
        "error_code": error_code,
        "error_message": error_message,
        "is_partial": is_partial,
        "rows": [{"timestamp_iso": r.timestamp_iso, "values": r.values} for r in rows],
    }
    body = _post_json("/api/internal/gateway/load-profile-results", payload, timeout_s=timeout_s)
    return body is not None
