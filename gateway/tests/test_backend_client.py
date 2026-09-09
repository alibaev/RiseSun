"""Тесты mmws_gateway.backend_client — HTTP-клиент к внутреннему каналу
Backend'а (событийное чтение call-home сразу при подключении, см.
DECISIONS.md и план ticklish-popping-bear.md). Использует настоящий
локальный HTTP-сервер (``http.server``), не мок сети — те же ошибки
(таймаут, не-2xx, недоступный порт), что были бы и в проде."""

from __future__ import annotations

import http.server
import importlib
import json
import threading

import pytest

from mmws_gateway import backend_client


class _StubHandler(http.server.BaseHTTPRequestHandler):
    response_body: dict = {}
    response_status: int = 200
    last_request: dict | None = None
    last_headers: dict | None = None

    def do_POST(self):  # noqa: N802 — имя метода диктуется http.server
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _StubHandler.last_request = json.loads(body) if body else {}
        _StubHandler.last_headers = dict(self.headers)
        self.send_response(_StubHandler.response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(_StubHandler.response_body).encode("utf-8"))

    def log_message(self, format, *args):  # noqa: A002 — сигнатура базового класса
        pass  # тише в тестовом выводе


@pytest.fixture
def stub_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _StubHandler.response_body = {}
    _StubHandler.response_status = 200
    _StubHandler.last_request = None
    _StubHandler.last_headers = None
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=3)


@pytest.fixture(autouse=True)
def _configure_backend_client(stub_server, monkeypatch):
    """Внутренний секрет и URL читаются как модульные константы при
    импорте (``os.environ.get`` на верхнем уровне ``backend_client.py``)
    — переопределяем их напрямую на модуле, а не через os.environ (уже
    поздно, модуль импортирован), плюс перезагружаем модуль не нужно."""
    monkeypatch.setattr(backend_client, "_INTERNAL_SECRET", "test-secret")
    monkeypatch.setattr(
        backend_client, "DEFAULT_BACKEND_INTERNAL_URL", f"http://127.0.0.1:{stub_server.server_port}"
    )


def test_claim_due_jobs_happy_path_round_trip():
    _StubHandler.response_body = {
        "meter_found": True,
        "meter_id": 42,
        "protocol_profile": "hdlc_dlms",
        "password": "12345678",
        "jobs": [{"job_id": 1, "job_type": "read_current", "obis": "1.1.1.8.0.ff", "class_id": 0}],
    }
    result = backend_client.claim_due_jobs("202306004113")
    assert result is not None
    assert result.meter_found is True
    assert result.meter_id == 42
    assert result.password == "12345678"
    assert len(result.jobs) == 1
    assert result.jobs[0].job_id == 1
    assert result.jobs[0].obis == "1.1.1.8.0.ff"

    assert _StubHandler.last_headers["X-Internal-Secret"] == "test-secret"


def test_claim_due_jobs_returns_none_when_meter_not_found():
    _StubHandler.response_body = {
        "meter_found": False, "meter_id": None, "protocol_profile": None, "password": None, "jobs": [],
    }
    assert backend_client.claim_due_jobs("202306004113") is None


def test_claim_due_jobs_returns_none_when_no_jobs():
    _StubHandler.response_body = {
        "meter_found": True, "meter_id": 42, "protocol_profile": "hdlc_dlms", "password": "x", "jobs": [],
    }
    assert backend_client.claim_due_jobs("202306004113") is None


def test_claim_due_jobs_returns_none_on_http_error_status():
    _StubHandler.response_status = 401
    _StubHandler.response_body = {"detail": "Недействительный внутренний секрет"}
    assert backend_client.claim_due_jobs("202306004113") is None


def test_claim_due_jobs_returns_none_on_unreachable_backend(monkeypatch):
    monkeypatch.setattr(backend_client, "DEFAULT_BACKEND_INTERNAL_URL", "http://127.0.0.1:1")  # закрытый порт
    assert backend_client.claim_due_jobs("202306004113", timeout_s=1.0) is None


def test_claim_due_jobs_fail_closed_without_secret(monkeypatch):
    """Отсутствующий секрет — не пытаемся отправить пустую строку,
    сразу отступаем (см. docstring backend_client.py про fail-closed)."""
    monkeypatch.setattr(backend_client, "_INTERNAL_SECRET", "")
    assert backend_client.claim_due_jobs("202306004113") is None
    assert _StubHandler.last_request is None  # запрос вообще не ушёл


def test_report_job_results_happy_path_sends_correct_payload():
    _StubHandler.response_body = {"accepted": 1, "skipped": 0}
    ok = backend_client.report_job_results(
        "202306004113",
        [
            backend_client.JobResultReport(
                job_id=1, obis="1.1.1.8.0.ff", ok=True, value=1234.5,
            )
        ],
    )
    assert ok is True
    assert _StubHandler.last_request["serial"] == "202306004113"
    assert _StubHandler.last_request["results"] == [
        {
            "job_id": 1, "obis": "1.1.1.8.0.ff", "ok": True, "value": 1234.5,
            "error_code": None, "error_message": None, "is_partial": False,
        }
    ]


def test_report_job_results_returns_false_on_unreachable_backend(monkeypatch):
    monkeypatch.setattr(backend_client, "DEFAULT_BACKEND_INTERNAL_URL", "http://127.0.0.1:1")
    ok = backend_client.report_job_results(
        "202306004113", [backend_client.JobResultReport(job_id=1, obis="1.1.1.8.0.ff", ok=False)], timeout_s=1.0
    )
    assert ok is False
